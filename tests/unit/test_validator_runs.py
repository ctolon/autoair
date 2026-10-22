"""Test runs on the validator service (``POST /v1/run``) without Airflow: signing, limits,
the client and the fail modes of the main server's runner (no run falls back to this host
unless ``fail_mode`` is ``local``)."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient

from autoair import runner
from autoair.runner import DagTestResult, TaskTestResult
from autoair.sections import ValidatorSettings
from autoair.validation import remote, service
from autoair.validation.remote import RUN_PATH, ValidatorUnavailable, run_remote, sign
from autoair.validation.service import RunRequest, create_validator_app

KEY = "unit-test-validator-key-0123456789"
CONN_ENV = "AIRFLOW_CONN_AUTOAIR_VALIDATOR"
URL = "http://validator.test"


class FakePool:
    started = 0

    def __init__(self) -> None:
        self.sandbox_report: dict[str, Any] = {}

    def warmup(self) -> None:
        pass

    def shutdown(self) -> None:
        pass

    def submit(self, source: str, filename: str, timeout: float) -> Any:
        from autoair.validation.dagbag_check import ImportCheckResult

        return ImportCheckResult(ok=True, dag_ids=["d"])


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv(CONN_ENV, json.dumps({"conn_type": "generic", "password": KEY}))
    remote.reset()
    yield
    remote.reset()


@pytest.fixture
def runs(monkeypatch: pytest.MonkeyPatch) -> list[tuple[RunRequest, str]]:
    """Replaces the run process: records each request and answers success."""
    seen: list[tuple[RunRequest, str]] = []

    def fake(req: RunRequest, cfg: ValidatorSettings, scratch_root: str) -> dict[str, Any]:
        seen.append((req, scratch_root))
        if req.kind == "task":
            return TaskTestResult(
                task_id=req.task_id, state="success", return_value=42, log="ran remotely"
            ).model_dump(mode="json")
        return DagTestResult(state="success", mode="memory", log="ran remotely").model_dump(
            mode="json"
        )

    monkeypatch.setattr(service, "execute_run", fake)
    return seen


def _client(**cfg: Any) -> TestClient:
    return TestClient(create_validator_app(ValidatorSettings(**cfg), pool=FakePool()))  # type: ignore[arg-type]


def _body(**extra: Any) -> bytes:
    payload = {"kind": "task", "source": "x = 1\n", "dag_id": "d", "task_id": "t", **extra}
    return json.dumps(payload).encode()


def _signed(body: bytes) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        **sign(KEY.encode(), method="POST", path=RUN_PATH, body=body),
    }


def _forward(client: TestClient) -> Any:
    def handler(request: httpx2.Request) -> httpx2.Response:
        resp = client.request(
            request.method, request.url.path, content=request.content, headers=dict(request.headers)
        )
        return httpx2.Response(resp.status_code, content=resp.content, headers=resp.headers)

    return handler


# --------------------------------------------------------------------------- the service


def test_signed_run_is_executed_and_its_answer_signed(runs: list[Any]) -> None:
    with _client(max_run_seconds=30) as client:
        body = _body(timeout=999)
        resp = client.post(RUN_PATH, content=body, headers=_signed(body))
        assert resp.status_code == 200, resp.text
        assert resp.json()["state"] == "success"
        assert remote.RESPONSE_SIGNATURE_HEADER in resp.headers
        ((req, root),) = runs
        assert req.task_id == "t"
        assert root  # the shared scratch root of workers and runs
        metrics = client.get("/metrics").text
        assert 'autoair_validator_runs_total{kind="task",state="success"} 1.0' in metrics


def test_execute_run_caps_the_timeout_and_never_forwards(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_task(source: str, dag_id: str, task_id: str, **kw: Any) -> TaskTestResult:
        seen.update(kw, dag_id=dag_id, task_id=task_id)
        return TaskTestResult(task_id=task_id, state="success")

    monkeypatch.setattr(runner, "run_task_test", fake_task)
    req = RunRequest.model_validate_json(_body(timeout=999))
    service.execute_run(req, ValidatorSettings(max_run_seconds=30), "/tmp/root")
    assert seen["timeout"] == 30
    assert seen["remote"] is False  # the validator runs it itself
    box = seen["sandbox"]
    assert box.profile == "run"
    assert box.private_home is True
    assert box.network_isolation == "off"  # validator.run_network defaults to allowed
    assert CONN_ENV not in seen["env_base"]  # the HMAC key never reaches a run


def test_run_network_off_isolates_the_run() -> None:
    box = service.run_sandbox(ValidatorSettings(run_network=False), "/r")
    assert box.network_isolation == "auto"
    assert service.run_sandbox(ValidatorSettings(), "/r").network_isolation == "off"


@pytest.mark.parametrize(
    ("headers", "body", "status"),
    [
        ({}, _body(), 401),
        (None, b"{", 422),
        (None, _body(kind="task", task_id=None), 422),
        (None, _body(mode="db"), 422),  # the validator has no metadata DB
        (None, _body(extra_field=1), 422),
    ],
)
def test_bad_run_requests_are_refused(
    runs: list[Any], headers: dict[str, str] | None, body: bytes, status: int
) -> None:
    with _client() as client:
        resp = client.post(RUN_PATH, content=body, headers=_signed(body) if headers is None else {})
        assert resp.status_code == status, resp.text
    assert runs == []


def test_too_large_run_is_refused(runs: list[Any]) -> None:
    with _client(max_request_bytes=1024) as client:
        body = _body(source="x" * 4096)
        assert client.post(RUN_PATH, content=body, headers=_signed(body)).status_code == 413


def test_concurrent_runs_beyond_the_limit_get_503(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = threading.Event()

    def slow(req: RunRequest, cfg: ValidatorSettings, root: str) -> dict[str, Any]:
        gate.wait(5)
        return {"task_id": "t", "state": "success"}

    monkeypatch.setattr(service, "execute_run", slow)
    codes: list[int] = []
    with _client(max_concurrent_runs=1) as client:

        def call() -> None:
            body = _body()
            codes.append(client.post(RUN_PATH, content=body, headers=_signed(body)).status_code)

        first = threading.Thread(target=call)
        first.start()
        time.sleep(0.3)
        call()  # the only run slot is taken
        gate.set()
        first.join()
    assert sorted(codes) == [200, 503]


# --------------------------------------------------------------------------- the client


def test_client_round_trip(runs: list[Any]) -> None:
    with _client() as client:
        data = run_remote(
            json.loads(_body()),
            10,
            ValidatorSettings(url=URL),
            transport=httpx2.MockTransport(_forward(client)),
        )
    assert data["state"] == "success"
    assert data["return_value"] == 42


def test_client_reports_an_old_validator_without_runs() -> None:
    transport = httpx2.MockTransport(lambda r: httpx2.Response(404, json={"detail": "x"}))
    with pytest.raises(ValidatorUnavailable, match="upgrade"):
        run_remote(json.loads(_body()), 5, ValidatorSettings(url=URL), transport=transport)
    assert remote.status(ValidatorSettings(url=URL))["down"] is False  # it is up


def test_client_rejects_an_unsigned_run_answer() -> None:
    transport = httpx2.MockTransport(lambda r: httpx2.Response(200, json={"state": "success"}))
    with pytest.raises(ValidatorUnavailable, match="not signed"):
        run_remote(json.loads(_body()), 5, ValidatorSettings(url=URL), transport=transport)


# --------------------------------------------------------------------------- fail modes


def _local_runs(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake_child(request: dict[str, Any], *a: Any, **kw: Any) -> Any:
        calls.append(request)
        return {"state": "success", "task_id": request.get("task_id")}, "local", 0.1, False

    monkeypatch.setattr(runner, "_run_child", fake_child)
    return calls


def _down(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable(*a: Any, **kw: Any) -> Any:
        raise ValidatorUnavailable("not reachable: ConnectError")

    monkeypatch.setattr(remote, "run_remote", unavailable)


@pytest.mark.parametrize("mode", ["open", "closed"])
def test_open_and_closed_never_run_locally(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    calls = _local_runs(monkeypatch)
    _down(monkeypatch)
    cfg = ValidatorSettings(url=URL, fail_mode=mode)  # type: ignore[arg-type]
    task = runner.run_task_test("x", "d", "t", validator=cfg)
    assert task.state == "error"
    assert "test-run service is unavailable" in (task.error or "")
    dag = runner.run_dag_test("x", "d", validator=cfg)
    assert dag.state == "error"
    assert calls == []


def test_fail_mode_local_runs_here(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _local_runs(monkeypatch)
    _down(monkeypatch)
    result = runner.run_task_test(
        "x", "d", "t", validator=ValidatorSettings(url=URL, fail_mode="local")
    )
    assert result.state == "success"
    assert len(calls) == 1


def test_runs_go_to_the_validator_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _local_runs(monkeypatch)
    sent: list[dict[str, Any]] = []

    def fake_remote(payload: dict[str, Any], timeout: float, cfg: Any) -> dict[str, Any]:
        sent.append(payload)
        return {"task_id": payload.get("task_id"), "state": "success", "log": "remote"}

    monkeypatch.setattr(remote, "run_remote", fake_remote)
    cfg = ValidatorSettings(url=URL)
    assert runner.run_task_test("src", "d", "t", validator=cfg, timeout=9).log == "remote"
    assert sent[-1]["source"] == "src"
    assert sent[-1]["timeout"] == 9
    assert runner.run_dag_test("src", "d", validator=cfg).state == "success"
    assert calls == []
    # db / auto need the metadata DB: they stay here; test_runs=false keeps all runs here
    runner.run_dag_test("src", "d", mode="db", validator=cfg)
    runner.run_task_test("src", "d", "t", validator=ValidatorSettings(url=URL, test_runs=False))
    assert len(calls) == 2
    assert len(sent) == 2


def test_an_invalid_validator_answer_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _local_runs(monkeypatch)
    monkeypatch.setattr(
        remote, "run_remote", lambda *a, **kw: {"state": "success", "duration": "x"}
    )
    result = runner.run_task_test("src", "d", "t", validator=ValidatorSettings(url=URL))
    assert result.state == "error"


def test_runs_can_go_to_a_separate_validator(runs: list[Any]) -> None:
    seen: list[str] = []
    with _client() as client:
        forward = _forward(client)

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(str(request.url))
            return forward(request)

        cfg = ValidatorSettings(url=URL, run_url="http://runs.validator.test")
        run_remote(json.loads(_body()), 5, cfg, transport=httpx2.MockTransport(handler))
    assert seen == ["http://runs.validator.test/v1/run"]
    assert ValidatorSettings(url=URL).runs_url == URL
    with pytest.raises(ValueError, match="http"):
        ValidatorSettings(run_url="ftp://x")


# --------------------------------------------------------------------------- no double runs


@pytest.mark.parametrize("status", [502, 504])
def test_runs_are_not_retried_after_they_may_have_run(status: int) -> None:
    calls: list[int] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(1)
        return httpx2.Response(status)

    cfg = ValidatorSettings(url=URL, retries=3, retry_backoff_seconds=0)
    with pytest.raises(ValidatorUnavailable) as err:
        run_remote(json.loads(_body()), 5, cfg, transport=httpx2.MockTransport(handler))
    assert err.value.may_have_run
    assert len(calls) == 1


def test_lost_connection_after_sending_is_not_retried() -> None:
    calls: list[int] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(1)
        raise httpx2.ReadError("connection reset", request=request)

    cfg = ValidatorSettings(url=URL, retries=3, retry_backoff_seconds=0)
    with pytest.raises(ValidatorUnavailable) as err:
        run_remote(json.loads(_body()), 5, cfg, transport=httpx2.MockTransport(handler))
    assert err.value.may_have_run
    assert len(calls) == 1


def test_refused_connections_and_busy_are_retried() -> None:
    answers = [503, 200]

    with _client() as client:
        forward = _forward(client)

        def handler(request: httpx2.Request) -> httpx2.Response:
            if answers.pop(0) == 503:
                return httpx2.Response(503)
            return forward(request)

        cfg = ValidatorSettings(url=URL, retries=2, retry_backoff_seconds=0)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(service, "execute_run", lambda req, cfg, root: {"state": "success"})
            data = run_remote(json.loads(_body()), 5, cfg, transport=httpx2.MockTransport(handler))
    assert data["state"] == "success"


def test_fail_mode_local_never_reruns_a_run_that_may_have_happened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _local_runs(monkeypatch)

    def timed_out(*a: Any, **kw: Any) -> Any:
        raise ValidatorUnavailable("no answer within 35s", may_have_run=True)

    monkeypatch.setattr(remote, "run_remote", timed_out)
    cfg = ValidatorSettings(url=URL, fail_mode="local")
    result = runner.run_task_test("x", "d", "t", validator=cfg)
    assert result.state == "error"
    assert "not started again" in (result.error or "")
    assert runner.ran_on(result) == "validator"
    assert calls == []


def test_path_like_ids_are_refused(runs: list[Any]) -> None:
    with _client() as client:
        for body in (_body(dag_id="../../x"), _body(task_id="a/b")):
            assert client.post(RUN_PATH, content=body, headers=_signed(body)).status_code == 422
    assert runs == []


def test_validator_can_refuse_runs(runs: list[Any]) -> None:
    with _client(runs_enabled=False) as client:
        body = _body()
        assert client.post(RUN_PATH, content=body, headers=_signed(body)).status_code == 404
    assert runs == []
