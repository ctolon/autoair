"""Validator service and its client without Airflow: signatures, limits, retries, the
health state and the fail modes (a fake pool stands in for the DagBag workers)."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from autoair import sections
from autoair.sections import ValidatorSettings
from autoair.settings import Settings
from autoair.validation import dagbag_check, remote
from autoair.validation.dagbag_check import ImportCheckResult, ImportCheckUnavailable
from autoair.validation.remote import (
    IDENTITY_HEADER,
    SIGNATURE_HEADER,
    KeyCache,
    ValidatorUnavailable,
    check_remote,
    password_from_env,
    sign,
)
from autoair.validation.service import create_validator_app, worker_env

KEY = "unit-test-validator-key-0123456789"
CONN_ENV = "AIRFLOW_CONN_AUTOAIR_VALIDATOR"
URL = "http://validator.test"


class FakePool:
    """Stands in for DagBagPool: answers every check with ``result`` after ``delay``."""

    def __init__(self, result: ImportCheckResult | None = None, delay: float = 0.0) -> None:
        self.result = result or ImportCheckResult(ok=True, dag_ids=["d"], task_ids={"d": ["t"]})
        self.delay = delay
        self.started = 0
        self.sandbox_report: dict[str, Any] = {"network_isolated": True, "limits": ["RLIMIT_AS"]}
        self.calls: list[tuple[str, str, float]] = []
        self.closed = False

    def warmup(self) -> None:
        self.started += 1

    def shutdown(self) -> None:
        self.closed = True

    def submit(self, source: str, filename: str, timeout: float) -> ImportCheckResult:
        self.calls.append((source, filename, timeout))
        if self.delay:
            time.sleep(self.delay)
        return self.result.model_copy(deep=True)


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv(CONN_ENV, json.dumps({"conn_type": "generic", "password": KEY}))
    remote.reset()
    yield
    remote.reset()


def _service(pool: FakePool | None = None, **cfg: Any) -> tuple[TestClient, FakePool]:
    pool = pool or FakePool()
    app = create_validator_app(ValidatorSettings(**cfg), pool=pool)  # type: ignore[arg-type]
    return TestClient(app), pool


def _wait_ready(client: TestClient) -> dict[str, Any]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        resp = client.get("/ready")
        if resp.status_code == 200:
            return dict(resp.json())
        time.sleep(0.02)
    raise AssertionError(client.get("/ready").json())


def _signed(body: bytes, key: str = KEY, path: str = "/v1/check") -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        **sign(key.encode(), method="POST", path=path, body=body),
    }


def _body(**extra: Any) -> bytes:
    return json.dumps({"source": "x = 1\n", "filename": "d.py", "timeout": 5, **extra}).encode()


def _answer(request: httpx2.Request, payload: Any, key: str = KEY) -> httpx2.Response:
    """A 200 answer signed like the validator signs it (bound to the request's nonce)."""
    content = json.dumps(payload).encode()
    nonce = remote.request_nonce(request.headers[IDENTITY_HEADER]) or ""
    signature = remote.response_signature(key.encode(), nonce, 200, content)
    return httpx2.Response(
        200, content=content, headers={remote.RESPONSE_SIGNATURE_HEADER: signature}
    )


def _forward(client: TestClient, seen: list[httpx2.Request] | None = None) -> Any:
    """An httpx2 transport handler that forwards to the in-process service."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        if seen is not None:
            seen.append(request)
        resp = client.request(
            request.method,
            request.url.path,
            content=request.content,
            headers=dict(request.headers),
        )
        return httpx2.Response(resp.status_code, content=resp.content, headers=resp.headers)

    return handler


# --------------------------------------------------------------------------- settings / key


def test_settings_defaults_and_url_validation() -> None:
    cfg = Settings().validator
    assert cfg.url == ""
    assert cfg.fail_mode == "open"
    assert cfg.max_tasks_per_child == 1
    assert ValidatorSettings(url="http://v:8791/").url == "http://v:8791"
    with pytest.raises(ValidationError):
        ValidatorSettings(url="ftp://v")
    with pytest.raises(ValidationError):
        ValidatorSettings(fail_mode="ignore")  # type: ignore[arg-type]


def test_password_from_env_forms() -> None:
    env = {CONN_ENV: json.dumps({"password": "p1"})}
    assert password_from_env("autoair_validator", env) == "p1"
    env = {CONN_ENV: "generic://:s%40cret@"}
    assert password_from_env("autoair_validator", env) == "s@cret"
    assert password_from_env("autoair_validator", {}) is None
    assert password_from_env("autoair_validator", {CONN_ENV: "{not json"}) is None
    assert password_from_env("autoair_validator", {CONN_ENV: "generic://host"}) is None


def test_key_cache_reads_again_after_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    keys = KeyCache(env_only=True)
    assert keys.get("autoair_validator", 60) == KEY.encode()
    monkeypatch.setenv(CONN_ENV, json.dumps({"password": "rotated-key-0123456789"}))
    assert keys.get("autoair_validator", 60) == KEY.encode()  # cached for 60 s
    uncached = KeyCache(env_only=True)
    assert uncached.get("autoair_validator", 0) == b"rotated-key-0123456789"
    monkeypatch.setenv(CONN_ENV, json.dumps({"password": "rotated-again-0123456789"}))
    assert uncached.get("autoair_validator", 0) == b"rotated-again-0123456789"
    monkeypatch.delenv(CONN_ENV)
    keys.clear()
    with pytest.raises(remote.KeyUnavailable, match="AIRFLOW_CONN_AUTOAIR_VALIDATOR"):
        keys.get("autoair_validator", 60)


def test_worker_env_drops_the_key_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIRFLOW_CONN_OTHER", "generic://a")
    env = worker_env(ValidatorSettings())
    assert CONN_ENV not in env
    assert "AIRFLOW_CONN_OTHER" in env


# --------------------------------------------------------------------------- service


def test_health_ready_and_metrics() -> None:
    client, pool = _service()
    with client:
        assert client.get("/health").json()["service"] == "validator"
        ready = _wait_ready(client)
        assert ready["ready"] is True
        assert ready["network_isolated"] is True
        assert pool.calls
        assert pool.calls[0][1] == "autoair_validator_selftest.py"
        body = _body()
        assert client.post("/v1/check", content=body, headers=_signed(body)).status_code == 200
        text = client.get("/metrics").text
        assert 'autoair_validator_checks_total{result="ok"}' in text
        assert "autoair_validator_in_flight" in text
    assert pool.closed


def test_metrics_can_be_disabled() -> None:
    client, _ = _service(metrics_enabled=False)
    with client:
        assert client.get("/metrics").status_code == 404


def test_ready_is_503_while_the_selftest_fails() -> None:
    bad = FakePool(ImportCheckResult(ok=False, import_errors=["DagBag worker: ImportError"]))
    client, _ = _service(bad)
    with client:
        time.sleep(0.2)
        resp = client.get("/ready")
        assert resp.status_code == 503
        assert "ImportError" in resp.json()["detail"]


def test_signed_check_runs_and_caps_the_timeout() -> None:
    client, pool = _service(max_check_seconds=3)
    with client:
        body = _body(timeout=60)
        resp = client.post("/v1/check", content=body, headers=_signed(body))
        assert resp.status_code == 200, resp.text
        assert resp.json()["dag_ids"] == ["d"]
        assert pool.calls[-1] == ("x = 1\n", "d.py", 3)


@pytest.mark.parametrize(
    ("headers", "why"),
    [
        ({}, "missing"),
        ("wrong-key", "another key"),
        ("other-path", "signed for another path"),
    ],
)
def test_unauthenticated_checks_are_refused(headers: Any, why: str) -> None:
    client, pool = _service()
    body = _body()
    if headers == "wrong-key":
        headers = _signed(body, key="not-the-key-0123456789")
    elif headers == "other-path":
        headers = _signed(body, path="/v1/other")
    with client:
        _wait_ready(client)
        before = len(pool.calls)
        resp = client.post("/v1/check", content=body, headers=headers)
        assert resp.status_code == 401, why
        assert len(pool.calls) == before
        assert (
            'autoair_validator_rejected_total{reason="unauthorized"}' in client.get("/metrics").text
        )


def test_tampered_body_and_replay_are_refused() -> None:
    client, _ = _service()
    body = _body()
    headers = _signed(body)
    with client:
        assert (
            client.post("/v1/check", content=_body(source="evil"), headers=headers).status_code
            == 401
        )
        assert client.post("/v1/check", content=body, headers=headers).status_code == 200
        assert client.post("/v1/check", content=body, headers=headers).status_code == 401


def test_service_without_key_refuses_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(CONN_ENV)
    client, _ = _service()
    body = _body()
    with client:
        resp = client.post("/v1/check", content=body, headers=_signed(body))
        assert resp.status_code == 503
        assert KEY not in resp.text


def test_too_large_and_invalid_requests() -> None:
    client, pool = _service(max_request_bytes=1024)
    with client:
        _wait_ready(client)
        before = len(pool.calls)
        big = _body(source="x" * 4096)
        assert client.post("/v1/check", content=big, headers=_signed(big)).status_code == 413
        for bad in (
            _body(filename="../escape.py"),
            _body(filename="dir/d.py"),
            _body(filename="d.txt"),
            _body(timeout=0),
            _body(extra_field=1),
            b"not json",
        ):
            assert client.post("/v1/check", content=bad, headers=_signed(bad)).status_code == 422
        assert len(pool.calls) == before


def test_busy_service_answers_503() -> None:
    client, _ = _service(FakePool(delay=0.5), pool_size=1, max_queue=0)
    with client:
        _wait_ready(client)
        codes: list[int] = []

        def call() -> None:
            body = _body()
            codes.append(client.post("/v1/check", content=body, headers=_signed(body)).status_code)

        threads = [threading.Thread(target=call) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sorted(codes)[0] == 200
        assert 503 in codes


def test_no_auth_mode_accepts_unsigned_checks() -> None:
    app = create_validator_app(ValidatorSettings(), no_auth=True, pool=FakePool())  # type: ignore[arg-type]
    with TestClient(app) as client:
        assert client.post("/v1/check", content=_body()).status_code == 200


# --------------------------------------------------------------------------- client


def test_client_round_trip_through_the_service() -> None:
    client, pool = _service()
    with client:
        _wait_ready(client)
        seen: list[httpx2.Request] = []
        result = check_remote(
            "x = 2\n",
            "d.py",
            7,
            ValidatorSettings(url=URL),
            transport=httpx2.MockTransport(_forward(client, seen)),
        )
        assert result.ok
        assert result.dag_ids == ["d"]
        assert pool.calls[-1] == ("x = 2\n", "d.py", 7)
        assert IDENTITY_HEADER in seen[-1].headers
        assert SIGNATURE_HEADER in seen[-1].headers
        assert KEY not in json.dumps(dict(seen[-1].headers))


def test_client_with_a_path_prefix_signs_the_full_path() -> None:
    client, _ = _service()
    with client:
        _wait_ready(client)

        def handler(request: httpx2.Request) -> httpx2.Response:
            assert request.url.path == "/validator/v1/check"
            resp = client.request(
                "POST",
                "/v1/check",
                content=request.content,
                headers=dict(request.headers),
            )
            return httpx2.Response(resp.status_code, content=resp.content, headers=resp.headers)

        cfg = ValidatorSettings(url=f"{URL}/validator")
        assert check_remote("x", "d.py", 5, cfg, transport=httpx2.MockTransport(handler)).ok


def test_client_retries_transient_failures() -> None:
    answers = [503, 502, 200]
    calls: list[int] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        code = answers[len(calls)]
        calls.append(code)
        if code != 200:
            return httpx2.Response(code)
        return _answer(request, {"ok": True, "dag_ids": ["d"], "import_errors": []})

    cfg = ValidatorSettings(url=URL, retries=2, retry_backoff_seconds=0)
    result = check_remote("x", "d.py", 5, cfg, transport=httpx2.MockTransport(handler))
    assert result.ok
    assert calls == [503, 502, 200]
    # every attempt is signed afresh (single-use nonces)
    assert remote.status(cfg) == {"configured": True, "down": False, "reason": None}


def test_client_gives_up_and_fails_fast_until_ready() -> None:
    calls: list[str] = []
    ready = {"ok": False}

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request.url.path)
        if request.url.path == "/ready":
            return httpx2.Response(200 if ready["ok"] else 503)
        raise httpx2.ConnectError("refused", request=request)

    cfg = ValidatorSettings(
        url=URL, retries=1, retry_backoff_seconds=0, health_check_interval_seconds=0.2
    )
    transport = httpx2.MockTransport(handler)
    with pytest.raises(ValidatorUnavailable, match="ConnectError"):
        check_remote("x", "d.py", 5, cfg, transport=transport)
    assert calls == ["/v1/check", "/v1/check"]
    with pytest.raises(ValidatorUnavailable):
        check_remote("x", "d.py", 5, cfg, transport=transport)
    assert len(calls) == 2  # down: no request until the next probe is due
    time.sleep(0.25)
    with pytest.raises(ValidatorUnavailable, match="not ready: HTTP 503"):
        check_remote("x", "d.py", 5, cfg, transport=transport)
    assert calls[-1] == "/ready"
    time.sleep(0.25)
    ready["ok"] = True
    with pytest.raises(ValidatorUnavailable):  # ready, but the check still cannot connect
        check_remote("x", "d.py", 5, cfg, transport=transport)
    assert calls[-3:] == ["/ready", "/v1/check", "/v1/check"]


def test_client_does_not_retry_rejections_or_timeouts() -> None:
    calls: list[str] = []

    def rejected(request: httpx2.Request) -> httpx2.Response:
        calls.append("401")
        return httpx2.Response(401, json={"detail": "invalid signature"})

    cfg = ValidatorSettings(url=URL, retries=3, retry_backoff_seconds=0)
    with pytest.raises(ValidatorUnavailable, match="rejected the signature"):
        check_remote("x", "d.py", 5, cfg, transport=httpx2.MockTransport(rejected))
    assert calls == ["401"]
    remote.reset()

    def slow(request: httpx2.Request) -> httpx2.Response:
        calls.append("timeout")
        raise httpx2.ReadTimeout("slow", request=request)

    with pytest.raises(ValidatorUnavailable, match="no answer within 35s"):
        check_remote("x", "d.py", 5, cfg, transport=httpx2.MockTransport(slow))
    assert calls == ["401", "timeout"]


def test_client_reports_oversized_files_as_import_errors() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(413)

    result = check_remote(
        "x", "d.py", 5, ValidatorSettings(url=URL), transport=httpx2.MockTransport(handler)
    )
    assert not result.ok
    assert "max_request_bytes" in result.import_errors[0]


def test_client_refuses_invalid_or_huge_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = ValidatorSettings(url=URL, retries=0)
    with pytest.raises(ValidatorUnavailable, match="invalid answer"):
        check_remote(
            "x",
            "d.py",
            5,
            cfg,
            transport=httpx2.MockTransport(lambda r: _answer(r, [])),
        )
    remote.reset()
    monkeypatch.setattr(remote, "MAX_RESPONSE_BYTES", 10)
    with pytest.raises(ValidatorUnavailable, match="larger"):
        check_remote(
            "x",
            "d.py",
            5,
            cfg,
            transport=httpx2.MockTransport(lambda r: httpx2.Response(200, content=b"x" * 100)),
        )


def test_service_signs_its_answer_bound_to_the_request_nonce() -> None:
    client, _ = _service()
    with client:
        body = _body()
        headers = _signed(body)
        resp = client.post("/v1/check", content=body, headers=headers)
        assert resp.status_code == 200
        nonce = remote.request_nonce(headers[IDENTITY_HEADER])
        assert nonce
        got = resp.headers[remote.RESPONSE_SIGNATURE_HEADER]
        assert remote.verify_response(KEY.encode(), nonce, 200, resp.content, got)
        assert not remote.verify_response(KEY.encode(), "other-nonce", 200, resp.content, got)
        assert not remote.verify_response(b"another-key-0123456789", nonce, 200, resp.content, got)
        assert KEY not in json.dumps(dict(resp.headers))


def test_no_auth_service_with_a_key_still_signs_its_answer() -> None:
    app = create_validator_app(ValidatorSettings(), no_auth=True, pool=FakePool())  # type: ignore[arg-type]
    with TestClient(app) as client:
        body = _body()
        headers = _signed(body)
        resp = client.post("/v1/check", content=body, headers=headers)
        nonce = remote.request_nonce(headers[IDENTITY_HEADER]) or ""
        sig = resp.headers[remote.RESPONSE_SIGNATURE_HEADER]
        assert remote.verify_response(KEY.encode(), nonce, 200, resp.content, sig)


@pytest.mark.parametrize(
    "forge",
    [
        pytest.param(
            lambda r: httpx2.Response(200, json={"ok": True, "dag_ids": ["d"]}), id="unsigned"
        ),
        pytest.param(
            lambda r: _answer(r, {"ok": True, "dag_ids": ["d"]}, key="not-the-shared-key-0123"),
            id="wrong-key",
        ),
        pytest.param(
            lambda r: httpx2.Response(
                200,
                json={"ok": True, "dag_ids": ["d"]},
                headers={
                    remote.RESPONSE_SIGNATURE_HEADER: remote.response_signature(
                        KEY.encode(),
                        "nonce-of-an-earlier-request",
                        200,
                        json.dumps({"ok": True, "dag_ids": ["d"]}).encode(),
                    )
                },
            ),
            id="replayed-answer",
        ),
    ],
)
def test_client_rejects_unsigned_or_mismatched_answers(forge: Any) -> None:
    """A host between the server and the validator cannot claim that a DAG imports."""
    cfg = ValidatorSettings(url=URL, retries=0)
    with pytest.raises(ValidatorUnavailable, match="not signed with the shared key"):
        check_remote("x", "d.py", 5, cfg, transport=httpx2.MockTransport(forge))
    assert remote.status(cfg)["down"] is True


def test_client_without_key_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(CONN_ENV)
    monkeypatch.setattr(remote, "password_from_airflow", lambda conn_id: None)

    def handler(request: httpx2.Request) -> httpx2.Response:  # pragma: no cover - not reached
        raise AssertionError("no request without a key")

    with pytest.raises(ValidatorUnavailable, match="no HMAC key"):
        check_remote(
            "x", "d.py", 5, ValidatorSettings(url=URL), transport=httpx2.MockTransport(handler)
        )


# --------------------------------------------------------------------------- fail modes


def _down(monkeypatch: pytest.MonkeyPatch, mode: str) -> list[str]:
    sections.activate(Settings(validator=ValidatorSettings(url=URL, fail_mode=mode)))  # type: ignore[arg-type]
    local: list[str] = []

    def unavailable(*args: Any, **kwargs: Any) -> ImportCheckResult:
        raise ValidatorUnavailable("not reachable: ConnectError")

    def run_locally(source: str, *args: Any) -> ImportCheckResult:
        local.append(source)
        return ImportCheckResult(ok=True, dag_ids=["d"])

    monkeypatch.setattr(remote, "check_remote", unavailable)
    monkeypatch.setattr(dagbag_check, "_check_source_local", run_locally)
    return local


def test_fail_closed_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    local = _down(monkeypatch, "closed")
    with pytest.raises(ImportCheckUnavailable) as err:
        dagbag_check.check_source("src", "d.py", 5)
    assert err.value.severity == "error"
    assert str(err.value) == (
        "the import check service is unavailable (not reachable: ConnectError); "
        "deploys are blocked (fail closed)"
    )
    assert local == []  # user code never runs in this process


def test_fail_open_is_a_warning_and_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    local = _down(monkeypatch, "open")
    with pytest.raises(ImportCheckUnavailable) as err:
        dagbag_check.check_source("src", "d.py", 5)
    assert err.value.severity == "warning"
    assert "fail open" in str(err.value)
    assert local == []
    assert ValidatorSettings(url=URL).fail_mode == "open"


def test_fail_local_falls_back_to_the_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    local = _down(monkeypatch, "local")
    assert dagbag_check.check_source("src", "d.py", 5).ok
    assert local == ["src"]


def test_explicit_use_pool_stays_local(monkeypatch: pytest.MonkeyPatch) -> None:
    local = _down(monkeypatch, "closed")
    assert dagbag_check.check_source("src", "d.py", 5, use_pool=True).ok
    assert local == ["src"]


def test_validate_spec_reports_the_unavailable_service(
    monkeypatch: pytest.MonkeyPatch, simple_spec_data: dict[str, Any]
) -> None:
    from autoair.spec import DagSpec
    from autoair.validation import validate_spec

    spec = DagSpec.model_validate(simple_spec_data)
    _down(monkeypatch, "closed")
    report = validate_spec(spec, import_check=True)
    problems = [p for p in report.problems if p.code == "import_service"]
    assert not report.ok
    assert problems
    assert problems[0].severity == "error"
    assert report.import_check is None
    _down(monkeypatch, "open")
    report = validate_spec(spec, import_check=True)
    assert [p.severity for p in report.problems if p.code == "import_service"] == ["warning"]


def test_validate_raw_reports_the_unavailable_service(monkeypatch: pytest.MonkeyPatch) -> None:
    from autoair.spec import DagSpec
    from autoair.validation import Policy
    from autoair.validation.raw import validate_raw

    spec = DagSpec.model_validate(
        {
            "dag_id": "raw_dag",
            "raw_code": 'from airflow.sdk import DAG\n\nwith DAG("raw_dag", schedule=None):\n'
            "    pass\n",
        }
    )
    _down(monkeypatch, "closed")
    report = validate_raw(spec, policy=Policy(), import_check=True)
    found = [p for p in report.problems if p.code == "import_service"]
    assert found
    assert found[0].severity == "error"
    assert found[0].field == "raw_code"


def test_worker_exit_reasons() -> None:
    import signal

    from autoair.validation.pool import _exit_reason

    assert _exit_reason(7) == "DagBag worker crashed (exit code 7)"
    assert "CPU time limit exceeded" in _exit_reason(-signal.SIGXCPU)
    assert "SIGKILL" in _exit_reason(-signal.SIGKILL)
    assert "SIGSEGV" in _exit_reason(-signal.SIGSEGV)


def test_busy_timeouts_and_refusals_do_not_open_the_circuit() -> None:
    answers: list[Any] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        kind = answers.pop(0)
        if kind == "timeout":
            raise httpx2.ReadTimeout("slow", request=request)
        return httpx2.Response(kind)

    cfg = ValidatorSettings(url=URL, retries=0, retry_backoff_seconds=0)
    transport = httpx2.MockTransport(handler)
    for kind, match in ((503, "HTTP 503"), (429, "HTTP 429"), ("timeout", "no answer")):
        answers.append(kind)
        with pytest.raises(ValidatorUnavailable, match=match):
            check_remote("x", "d.py", 5, cfg, transport=transport)
        assert remote.status(cfg)["down"] is False
    answers.append(422)
    result = check_remote("x", "d.py", 5, cfg, transport=transport)
    assert "HTTP 422" in result.import_errors[0]
    assert remote.status(cfg)["down"] is False
    answers.append(502)
    with pytest.raises(ValidatorUnavailable):
        check_remote("x", "d.py", 5, cfg, transport=transport)
    assert remote.status(cfg)["down"] is True


def test_short_keys_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CONN_ENV, json.dumps({"password": "short"}))
    with pytest.raises(remote.KeyUnavailable, match="shorter than 16"):
        KeyCache(env_only=True).get("autoair_validator", 0)


def test_dag_id_characters_are_valid_file_names() -> None:
    client, pool = _service()
    with client:
        body = _body(filename="-odd.name.py")
        assert client.post("/v1/check", content=body, headers=_signed(body)).status_code == 200
        assert pool.calls[-1][1] == "-odd.name.py"


def test_main_server_scrubs_the_key_from_user_code(monkeypatch: pytest.MonkeyPatch) -> None:
    from autoair._subprocess import user_code_env

    monkeypatch.setenv("AIRFLOW_CONN_OTHER", "generic://a")
    env = user_code_env()
    assert CONN_ENV not in env
    assert "AIRFLOW_CONN_OTHER" in env
    sections.activate(Settings(validator=ValidatorSettings(secret_conn_id="vkey")))
    monkeypatch.setenv("AIRFLOW_CONN_VKEY", "generic://:k@")
    assert "AIRFLOW_CONN_VKEY" not in user_code_env()


def test_cli_refuses_no_auth_on_a_public_bind() -> None:
    from typer.testing import CliRunner

    from autoair.cli import app

    result = CliRunner().invoke(app, ["validator-serve", "--no-auth", "--host", "0.0.0.0"])
    assert result.exit_code == 2
    assert "loopback" in result.output
