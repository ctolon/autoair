"""The validator service: DagBag import checks and test runs in their own process (and
container).

``autoair validator-serve`` runs only this app: a warm DagBag pool whose workers import
the untrusted DAG code under the limits of :mod:`autoair.validation.sandbox` (profile
``check``), and one-shot test-run processes (profile ``run``). It reads no
AutoAir state (no database, no storage, no catalog), needs no outbound network and holds
one credential: the HMAC key, read from the environment variable of an Airflow connection
(``AIRFLOW_CONN_<validator.secret_conn_id>``), which its workers never see.

Endpoints:

* ``POST /v1/check`` - ``{"source", "filename", "timeout"}`` signed by the main server
  (:func:`autoair.validation.remote.sign`); answers an ``ImportCheckResult`` signed with the
  same key and bound to the request's nonce (``x-autoair-validator-response-signature``,
  :func:`autoair.validation.remote.response_signature`). 401 for a
  missing, wrong, expired or replayed signature, 413 for a body over
  ``validator.max_request_bytes``, 503 when ``pool_size + max_queue`` checks are running;
* ``POST /v1/run`` - a task or DAG test run (:class:`RunRequest`, in memory: the service
  has no metadata DB) with the same signing, body limit and response signature; one process
  per run (:func:`autoair.runner.run_task_test` / ``run_dag_test`` with ``remote=False``),
  at most ``max_concurrent_runs`` at a time (503 beyond), network allowed unless
  ``run_network`` is off;
* ``GET /health`` (process alive), ``GET /ready`` (a self-test check passed; 503 before),
  ``GET /metrics`` (Prometheus, ``validator.metrics_enabled``).

Signed requests are single-use per replica: the nonce cache is process memory because the
service has no shared state. A request replayed to another replica within the five-minute
signature window runs the same side-effect-free check once more. A replayed ``/v1/run`` would
run the test once more, and test runs can have side effects (a task calling a service):
reach the validator over TLS (an ``https`` ``validator.url``) or a network only the main
server can reach, so a request cannot be captured on the way.
"""

from __future__ import annotations

import contextlib
import logging
import shutil
import tempfile
import threading
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Literal

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.concurrency import run_in_threadpool

from autoair import __version__
from autoair._subprocess import user_code_env
from autoair.sections import ValidatorSettings
from autoair.spec.models import KEY_PATTERN
from autoair.validation.dagbag_check import ImportCheckResult
from autoair.validation.pool import DagBagPool
from autoair.validation.remote import (
    IDENTITY_HEADER,
    RESPONSE_SIGNATURE_HEADER,
    SIGNATURE_HEADER,
    SIGNED_ROLE,
    KeyCache,
    KeyUnavailable,
    connection_env_name,
    request_nonce,
    response_signature,
)
from autoair.validation.sandbox import WorkerSandbox, make_non_dumpable

log = logging.getLogger("autoair.validation.service")

SELFTEST_DAG = """from airflow.sdk import DAG

with DAG("autoair_validator_selftest", schedule=None):
    pass
"""
# Seconds between self-test attempts while the service is not ready.
SELFTEST_RETRY_SECONDS = 10.0
CHECK_BUCKETS = (0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120)


class CheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str
    filename: str = Field(default="dag.py", pattern=r"^[A-Za-z0-9_.-]{1,250}\.py$")
    timeout: float = Field(default=60, gt=0)


class RunRequest(BaseModel):
    """A test run (``POST /v1/run``); the fields of the main server's runner request."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["task", "dag"]
    source: str
    # ids become file names of the run's folder: the spec's key pattern, no path parts
    dag_id: str = Field(pattern=KEY_PATTERN, min_length=1, max_length=250)
    task_id: str | None = Field(default=None, pattern=KEY_PATTERN, max_length=250)
    conf: dict[str, Any] = Field(default_factory=dict)
    mock_xcoms: dict[str, Any] = Field(default_factory=dict)
    mode: Literal["memory"] = "memory"
    logical_date: str | None = Field(default=None, max_length=64)
    timeout: float = Field(default=120, gt=0)
    max_log_chars: int = Field(default=64_000, ge=1_000, le=10_000_000)
    max_value_chars: int = Field(default=4_000, ge=100, le=1_000_000)


class _Metrics:
    def __init__(self, pool: DagBagPool, in_flight: Any) -> None:
        self.registry = CollectorRegistry()
        self.checks = Counter(
            "autoair_validator_checks_total",
            "Import checks by result (ok, failed, timeout, crashed, worker_error)",
            ["result"],
            registry=self.registry,
        )
        self.seconds = Histogram(
            "autoair_validator_check_seconds",
            "Import check duration, queueing included",
            registry=self.registry,
            buckets=CHECK_BUCKETS,
        )
        self.rejected = Counter(
            "autoair_validator_rejected_total",
            "Requests refused (unauthorized, too_large, busy, invalid, no_key, disabled)",
            ["reason"],
            registry=self.registry,
        )
        gauge = Gauge(
            "autoair_validator_in_flight", "Checks running or queued", registry=self.registry
        )
        gauge.set_function(in_flight)
        started = Gauge(
            "autoair_validator_workers_started",
            "Worker processes started since the service started",
            registry=self.registry,
        )
        started.set_function(lambda: float(pool.started))
        self.runs = Counter(
            "autoair_validator_runs_total",
            "Test runs by kind (task, dag) and resulting state",
            ["kind", "state"],
            registry=self.registry,
        )
        self.run_seconds = Histogram(
            "autoair_validator_run_seconds",
            "Test-run duration",
            registry=self.registry,
            buckets=(*CHECK_BUCKETS, 300, 600),
        )


def _classify(result: ImportCheckResult) -> str:
    if result.ok:
        return "ok"
    first = result.import_errors[0] if result.import_errors else ""
    if first.startswith("import timed out"):
        return "timeout"
    if first.startswith("DagBag worker crashed"):
        return "crashed"
    if first.startswith("DagBag worker:"):
        return "worker_error"
    return "failed"


def worker_env(cfg: ValidatorSettings) -> dict[str, str]:
    """The workers' environment: no AutoAir secrets and not the HMAC key's connection."""
    env = user_code_env()
    env.pop(connection_env_name(cfg.secret_conn_id), None)
    return env


def build_pool(cfg: ValidatorSettings, *, scratch_root: str = "") -> DagBagPool:
    """The warm DagBag pool; ``scratch_root``: the folder holding every worker's (and test
    run's) scratch folder, so the audit hook keeps them from reading each other's."""
    sandbox = WorkerSandbox(
        memory_limit_mb=cfg.memory_limit_mb,
        cpu_limit_seconds=cfg.cpu_limit_seconds,
        file_size_limit_mb=cfg.file_size_limit_mb,
        open_files_limit=cfg.open_files_limit,
        process_limit=cfg.process_limit,
        audit_hook=cfg.audit_hook,
        network_isolation=cfg.network_isolation,
        scratch_root=scratch_root,
    )
    return DagBagPool(
        cfg.pool_size,
        max_tasks_per_child=cfg.max_tasks_per_child,
        start_timeout=cfg.start_timeout_seconds,
        env=worker_env(cfg),
        sandbox=sandbox,
    )


def run_sandbox(cfg: ValidatorSettings, scratch_root: str) -> WorkerSandbox:
    """The sandbox of a test-run process: the run profile of the audit hook (subprocesses,
    sockets and writes allowed), the validator's limits, network unless ``run_network`` is
    off, its own PID namespace when ``run_process_isolation`` allows."""
    return WorkerSandbox(
        profile="run",
        memory_limit_mb=cfg.run_memory_limit_mb,
        file_size_limit_mb=cfg.file_size_limit_mb,
        open_files_limit=cfg.open_files_limit,
        process_limit=cfg.process_limit,
        audit_hook=cfg.audit_hook,
        network_isolation="off" if cfg.run_network else cfg.network_isolation,
        process_isolation=cfg.run_process_isolation,
        scratch_root=scratch_root,
        private_home=True,
    )


def execute_run(req: RunRequest, cfg: ValidatorSettings, scratch_root: str) -> dict[str, Any]:
    """Run ``req`` here (never forwarded to a validator) and return the result's JSON."""
    from autoair.runner import run_dag_test, run_task_test

    timeout = min(req.timeout, cfg.max_run_seconds)
    common: dict[str, Any] = {
        "conf": req.conf,
        "timeout": timeout,
        "logical_date": req.logical_date,
        "max_log": req.max_log_chars,
        "max_value": req.max_value_chars,
        "remote": False,
        "sandbox": run_sandbox(cfg, scratch_root),
        "env_base": worker_env(cfg),
    }
    if req.kind == "task":
        task = run_task_test(
            req.source, req.dag_id, req.task_id or "", mock_xcoms=req.mock_xcoms, **common
        )
        return task.model_dump(mode="json")
    dag = run_dag_test(req.source, req.dag_id, mode="memory", **common)
    return dag.model_dump(mode="json")


class _Ready:
    """Result of the self-test check (the service is ready once one passed)."""

    def __init__(self, pool: DagBagPool, timeout: float) -> None:
        self.pool = pool
        self.timeout = timeout
        self.ok = False
        self.detail = "starting"
        self.last_attempt = 0.0
        self._lock = threading.Lock()
        self._running = False

    def start(self) -> None:
        with self._lock:
            if self._running or self.ok:
                return
            self._running = True
            self.last_attempt = time.monotonic()
        threading.Thread(target=self._run, name="validator-selftest", daemon=True).start()

    def _run(self) -> None:
        try:
            result = self.pool.submit(SELFTEST_DAG, "autoair_validator_selftest.py", self.timeout)
            ok = result.ok
            detail = "ok" if ok else "; ".join(result.import_errors)[:500] or "no DAG found"
        except Exception as exc:  # a shut-down pool, a worker that cannot start
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        with self._lock:
            self.ok, self.detail, self._running = ok, detail, False
        if not ok:
            log.warning("validator self-test failed: %s", detail)

    def maybe_retry(self) -> None:
        if not self.ok and time.monotonic() - self.last_attempt >= SELFTEST_RETRY_SECONDS:
            self.start()


async def _read_body(request: Request, limit: int) -> bytes | None:
    """The body, or ``None`` when it is larger than ``limit``."""
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > limit:
                return None
        except ValueError:
            return None
    data = bytearray()
    async for chunk in request.stream():
        data += chunk
        if len(data) > limit:
            return None
    return bytes(data)


def create_validator_app(
    cfg: ValidatorSettings,
    *,
    no_auth: bool = False,
    pool: DagBagPool | None = None,
    keys: KeyCache | None = None,
) -> FastAPI:
    """The validator ASGI app; it owns ``pool`` (started and shut down with the app)."""
    from autoair.ai.access import ReplayCache, verify_identity

    # One folder for the scratch folders of every check worker and test run: each sees only
    # its own (audit hook), and a run cannot plant files in a check worker's private home.
    scratch_root = tempfile.mkdtemp(prefix="autoair-validator-")
    pool = pool or build_pool(cfg, scratch_root=scratch_root)
    keys = keys or KeyCache(env_only=True)
    replay = ReplayCache()
    ready = _Ready(pool, min(cfg.max_check_seconds, cfg.start_timeout_seconds))
    lock = threading.Lock()
    state = {"in_flight": 0, "runs": 0}
    metrics = _Metrics(pool, lambda: float(state["in_flight"]))
    capacity = cfg.pool_size + cfg.max_queue

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        pool.warmup()
        ready.start()
        try:
            yield
        finally:
            pool.shutdown()
            shutil.rmtree(scratch_root, ignore_errors=True)

    app = FastAPI(
        title="AutoAir validator",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    def reject(status: int, reason: str, detail: str) -> JSONResponse:
        metrics.rejected.labels(reason).inc()
        return JSONResponse({"detail": detail}, status_code=status)

    async def authenticate(
        request: Request,
    ) -> tuple[bytes, bytes | None, str | None] | JSONResponse:
        """``(body, key, nonce)`` of a signed request, or the rejection to send."""
        payload = request.headers.get(IDENTITY_HEADER)
        signature = request.headers.get(SIGNATURE_HEADER)
        if not no_auth and (not payload or not signature):
            return reject(401, "unauthorized", "signature required")
        body = await _read_body(request, cfg.max_request_bytes)
        if body is None:
            return reject(413, "too_large", "request body too large (validator.max_request_bytes)")
        key: bytes | None = None
        nonce: str | None = None
        if no_auth:
            # Unauthenticated (loopback only), but a client holding the key still verifies
            # the answer: sign it when this side has the key too.
            with contextlib.suppress(KeyUnavailable):
                key = keys.get(cfg.secret_conn_id, cfg.key_cache_seconds)
            nonce = request_nonce(payload) if payload else None
            return body, key, nonce
        try:
            key = keys.get(cfg.secret_conn_id, cfg.key_cache_seconds)
        except KeyUnavailable:
            log.error(
                "no HMAC key: set %s (validator.secret_conn_id)",
                connection_env_name(cfg.secret_conn_id),
            )
            return reject(503, "no_key", "the validator has no key configured")
        claims = verify_identity(
            key,
            payload or "",
            signature or "",
            method=request.method,
            path=request.url.path,
            query=request.url.query,
            body=body,
            replay=replay,
        )
        if claims is None or claims.get("role") != SIGNED_ROLE:
            return reject(401, "unauthorized", "invalid signature")
        return body, key, str(claims.get("nonce") or "")

    def signed(data: Any, key: bytes | None, nonce: str | None) -> JSONResponse:
        answer = JSONResponse(data)
        if key is not None and nonce:
            answer.headers[RESPONSE_SIGNATURE_HEADER] = response_signature(
                key, nonce, answer.status_code, bytes(answer.body)
            )
        return answer

    async def bounded(
        counter: str, limit: int, work: Callable[[], Awaitable[Any]]
    ) -> Any | JSONResponse:
        with lock:
            if state[counter] >= limit:
                busy = True
            else:
                busy = False
                state[counter] += 1
        if busy:
            response = reject(503, "busy", "all validator workers are busy")
            response.headers["Retry-After"] = "1"
            return response
        try:
            return await work()
        finally:
            with lock:
                state[counter] -= 1

    @app.post("/v1/check")
    async def check(request: Request) -> Response:
        auth = await authenticate(request)
        if isinstance(auth, JSONResponse):
            return auth
        body, key, nonce = auth
        try:
            req = CheckRequest.model_validate_json(body)
        except ValidationError:
            return reject(422, "invalid", "invalid check request")
        start = time.perf_counter()
        result = await bounded(
            "in_flight",
            capacity,
            lambda: run_in_threadpool(
                pool.submit, req.source, req.filename, min(req.timeout, cfg.max_check_seconds)
            ),
        )
        if isinstance(result, JSONResponse):
            return result
        metrics.seconds.observe(time.perf_counter() - start)
        metrics.checks.labels(_classify(result)).inc()
        return signed(result.model_dump(mode="json"), key, nonce)

    @app.post("/v1/run")
    async def run(request: Request) -> Response:
        if not cfg.runs_enabled:
            return reject(404, "disabled", "test runs are disabled (validator.runs_enabled)")
        auth = await authenticate(request)
        if isinstance(auth, JSONResponse):
            return auth
        body, key, nonce = auth
        try:
            req = RunRequest.model_validate_json(body)
        except ValidationError:
            return reject(422, "invalid", "invalid run request")
        if req.kind == "task" and not req.task_id:
            return reject(422, "invalid", "a task run needs task_id")
        start = time.perf_counter()
        result = await bounded(
            "runs",
            cfg.max_concurrent_runs,
            lambda: run_in_threadpool(execute_run, req, cfg, scratch_root),
        )
        if isinstance(result, JSONResponse):
            return result
        seconds = time.perf_counter() - start
        metrics.run_seconds.observe(seconds)
        run_state = str(result.get("state") or "error")
        metrics.runs.labels(req.kind, run_state).inc()
        # ids and outcome only: the source, conf and log stay out of the service's log
        log.info(
            "test run kind=%s dag_id=%s task_id=%s state=%s seconds=%.1f",
            req.kind,
            req.dag_id,
            req.task_id or "-",
            run_state,
            seconds,
        )
        return signed(result, key, nonce)

    @app.get("/health", include_in_schema=False)
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "validator", "version": __version__}

    @app.get("/ready", include_in_schema=False)
    def is_ready(response: Response) -> dict[str, Any]:
        ready.maybe_retry()
        if not ready.ok:
            response.status_code = 503
        report = pool.sandbox_report
        return {
            "ready": ready.ok,
            "detail": ready.detail,
            "network_isolated": bool(report.get("network_isolated")),
            "audit_hook": bool(report.get("audit_hook")),
            "limits": list(report.get("limits") or []),
        }

    @app.get("/metrics", include_in_schema=False)
    def prometheus() -> Response:
        if not cfg.metrics_enabled:
            return JSONResponse({"detail": "metrics are disabled"}, status_code=404)
        return Response(generate_latest(metrics.registry), media_type=CONTENT_TYPE_LATEST)

    return app


def harden_process() -> None:
    """Make the service process non-dumpable (Linux): its ``/proc`` entries, which hold the
    HMAC key's environment variable, become unreadable for the workers (same user)."""
    if not make_non_dumpable():
        log.info("could not make the validator process non-dumpable (not Linux?)")


__all__ = [
    "CheckRequest",
    "RunRequest",
    "build_pool",
    "create_validator_app",
    "execute_run",
    "harden_process",
    "run_sandbox",
    "worker_env",
]
