"""The validator service end to end: a real ``autoair validator-serve`` on a free port,
checks sent through the client and the main API, authentication failures, and DAG code
that tries to reach the network, write files, read the service's secrets, start
processes, spin forever or exhaust memory - every case contained."""

from __future__ import annotations

import json
import os
import random
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient

from autoair.api.app import build_services, create_app
from autoair.auth import AuthBackend
from autoair.sections import ValidatorSettings
from autoair.settings import Settings
from autoair.validation import remote
from autoair.validation.remote import KeyCache, ValidatorUnavailable, check_remote

pytestmark = pytest.mark.airflow

KEY = "integration-validator-key-0123456789abcdef"
CONN_ENV = "AIRFLOW_CONN_AUTOAIR_VALIDATOR"
MARK = "# airflow dag\n"  # DagBag's safe mode skips files without "airflow" and "dag"
DAG = """from airflow.sdk import DAG
from airflow.providers.standard.operators.bash import BashOperator

with DAG("{dag_id}", schedule=None):
    BashOperator(task_id="hello", bash_command="echo hi")
"""


def _free_port() -> int:
    ports = list(range(18000, 18900))
    random.shuffle(ports)
    for port in ports:
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    pytest.skip("no free port in 18000-18899")


class Validator:
    def __init__(self, url: str, proc: subprocess.Popen[bytes], port: int) -> None:
        self.url = url
        self.proc = proc
        self.port = port

    def cfg(self, **extra: Any) -> ValidatorSettings:
        return ValidatorSettings(url=self.url, retries=0, **extra)

    def check(self, source: str, name: str = "dag.py", timeout: float = 20) -> Any:
        return check_remote(source, name, timeout, self.cfg(), keys=KeyCache(env_only=True))

    def ready(self) -> dict[str, Any]:
        return dict(httpx2.get(f"{self.url}/ready", timeout=5).json())


def _start(tmp: Path, **settings: str) -> Iterator[Validator]:
    port = _free_port()
    home = tmp / "airflow-home"
    home.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "AIRFLOW_HOME": str(home),
        CONN_ENV: json.dumps({"conn_type": "generic", "password": KEY}),
        "AUTOAIR_VALIDATOR__POOL_SIZE": "2",
        **{f"AUTOAIR_VALIDATOR__{k.upper()}": v for k, v in settings.items()},
    }
    env.pop("AUTOAIR_CONFIG_FILE", None)
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from autoair.cli import app; app()",
            "validator-serve",
            "--port",
            str(port),
        ],
        cwd=tmp,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 180
        while True:
            if proc.poll() is not None:
                out = proc.stdout.read().decode() if proc.stdout else ""
                pytest.fail(f"validator-serve exited: {out[-3000:]}")
            try:
                if httpx2.get(f"{url}/ready", timeout=2).status_code == 200:
                    break
            except httpx2.HTTPError:
                pass  # not listening yet: keep polling until the deadline
            if time.monotonic() > deadline:
                pytest.fail("validator-serve did not become ready")
            time.sleep(0.3)
        yield Validator(url, proc, port)
    finally:
        proc.terminate()
        try:
            proc.wait(20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(5)


@pytest.fixture(scope="module")
def validator(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Validator]:
    yield from _start(
        tmp_path_factory.mktemp("validator"),
        cpu_limit_seconds="10",
        file_size_limit_mb="4",
        memory_limit_mb="1024",
    )


@pytest.fixture(autouse=True)
def _key_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv(CONN_ENV, json.dumps({"conn_type": "generic", "password": KEY}))
    remote.reset()
    yield
    remote.reset()


def _error(result: Any) -> str:
    assert not result.ok, result
    return "\n".join(result.import_errors)


# --------------------------------------------------------------------------- happy path


def test_ready_reports_the_sandbox(validator: Validator) -> None:
    ready = validator.ready()
    assert ready["ready"] is True
    assert ready["audit_hook"] is True
    assert {"RLIMIT_AS", "RLIMIT_CPU", "RLIMIT_FSIZE"} <= set(ready["limits"])
    assert httpx2.get(f"{validator.url}/health", timeout=5).json()["service"] == "validator"


def test_valid_dag_passes(validator: Validator) -> None:
    result = validator.check(DAG.format(dag_id="remote_ok"), "remote_ok.py")
    assert result.ok, result.import_errors
    assert result.dag_ids == ["remote_ok"]
    assert result.task_ids == {"remote_ok": ["hello"]}


def test_import_error_is_reported(validator: Validator) -> None:
    assert "boom" in _error(validator.check(MARK + "raise ValueError('boom')\n"))


def test_client_reads_the_key_through_airflow(validator: Validator) -> None:
    result = check_remote(DAG.format(dag_id="via_airflow"), "via_airflow.py", 20, validator.cfg())
    assert result.ok, result.import_errors


def test_metrics_count_checks(validator: Validator) -> None:
    validator.check(DAG.format(dag_id="counted"), "counted.py")
    text = httpx2.get(f"{validator.url}/metrics", timeout=5).text
    assert 'autoair_validator_checks_total{result="ok"}' in text
    assert "autoair_validator_check_seconds_bucket" in text


# --------------------------------------------------------------------------- authentication


def test_unsigned_and_wrongly_signed_requests_are_refused(
    validator: Validator, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = json.dumps({"source": DAG.format(dag_id="x"), "filename": "x.py"}).encode()
    assert httpx2.post(f"{validator.url}/v1/check", content=body, timeout=10).status_code == 401
    monkeypatch.setenv(CONN_ENV, json.dumps({"password": "a-different-key-0123456789"}))
    with pytest.raises(ValidatorUnavailable, match="rejected the signature"):
        validator.check(DAG.format(dag_id="wrong_key"), "wrong_key.py")


def test_oversized_source_is_refused(validator: Validator) -> None:
    source = MARK + "x = 1\n" + "#" * (5 * 1024 * 1024)
    assert "max_request_bytes" in _error(validator.check(source))


# --------------------------------------------------------------------------- containment


def test_network_access_is_blocked(validator: Validator) -> None:
    code = (
        MARK
        + "import socket\n"
        + f"socket.create_connection(('127.0.0.1', {validator.port}), timeout=3)\n"
        + "raise RuntimeError('LEAK connected')\n"
    )
    err = _error(validator.check(code))
    assert "LEAK" not in err
    assert "PermissionError" in err or "unreachable" in err.lower()


def test_dns_lookups_are_blocked(validator: Validator) -> None:
    code = MARK + "import socket\nsocket.getaddrinfo('localhost', 80)\nraise RuntimeError('LEAK')\n"
    assert "socket.getaddrinfo is not allowed" in _error(validator.check(code))


def test_writes_outside_the_scratch_folder_are_blocked(
    validator: Validator, tmp_path: Path
) -> None:
    target = tmp_path / "pwned.txt"
    code = MARK + f"open({str(target)!r}, 'w').write('x')\nraise RuntimeError('LEAK')\n"
    err = _error(validator.check(code))
    assert "is not allowed during an AutoAir import check" in err
    assert not target.exists()


def test_large_files_hit_the_file_size_limit(validator: Validator) -> None:
    code = (
        MARK
        + "import tempfile, os\n"
        + "path = os.path.join(tempfile.gettempdir(), 'big.bin')\n"
        + "with open(path, 'wb') as fh:\n"
        + "    fh.write(b'x' * (8 * 1024 * 1024))\n"
        + "raise RuntimeError('LEAK wrote')\n"
    )
    err = _error(validator.check(code))
    assert "LEAK" not in err
    assert "File too large" in err or "Errno 27" in err


def test_the_service_secret_cannot_be_read(validator: Validator) -> None:
    code = (
        MARK
        + "import os\n"
        + "found = [k for k in os.environ if 'AIRFLOW_CONN_AUTOAIR_VALIDATOR' in k]\n"
        + "if found:\n"
        + "    raise RuntimeError('LEAK env ' + os.environ[found[0]])\n"
        + "data = open(f'/proc/{os.getppid()}/environ', 'rb').read()\n"
        + "raise RuntimeError('LEAK proc ' + data.decode(errors='replace'))\n"
    )
    err = _error(validator.check(code))
    assert "LEAK" not in err
    assert KEY not in err


def test_processes_and_signals_are_blocked(validator: Validator) -> None:
    for code in (
        MARK + "import subprocess\nsubprocess.run(['true'])\nraise RuntimeError('LEAK')\n",
        MARK + "import os\nos.system('true')\nraise RuntimeError('LEAK')\n",
        MARK + "import os, signal\nos.kill(os.getppid(), signal.SIGKILL)\n",
        MARK + "import ctypes\nctypes.CDLL('libc.so.6')\nraise RuntimeError('LEAK')\n",
    ):
        err = _error(validator.check(code))
        assert "LEAK" not in err
        assert "is not allowed during an AutoAir import check" in err
    assert validator.proc.poll() is None
    assert validator.check(DAG.format(dag_id="still_alive"), "still_alive.py").ok


def test_infinite_loop_times_out(validator: Validator) -> None:
    start = time.monotonic()
    err = _error(validator.check(MARK + "while True:\n    pass\n", timeout=3))
    assert "import timed out after 3" in err
    assert time.monotonic() - start < 20
    assert validator.check(DAG.format(dag_id="after_loop"), "after_loop.py").ok


def test_cpu_limit_kills_a_busy_worker(validator: Validator) -> None:
    err = _error(validator.check(MARK + "while True:\n    pass\n", timeout=60))
    assert "CPU time limit exceeded" in err


def test_memory_blowup_is_contained(validator: Validator) -> None:
    err = _error(validator.check(MARK + "data = bytearray(4 * 1024 ** 3)\n"))
    assert "MemoryError" in err
    assert validator.check(DAG.format(dag_id="after_memory"), "after_memory.py").ok


def test_one_check_cannot_see_the_next(validator: Validator) -> None:
    """Fresh worker per check (max_tasks_per_child=1): module state does not survive."""
    plant = (
        MARK + "import builtins\nbuiltins.AUTOAIR_PLANTED = 'x'\nraise RuntimeError('planted')\n"
    )
    validator.check(plant)
    probe = (
        MARK
        + "import builtins\n"
        + "raise RuntimeError('seen' if hasattr(builtins, 'AUTOAIR_PLANTED') else 'clean')\n"
    )
    for _ in range(3):
        assert "clean" in _error(validator.check(probe))


# --------------------------------------------------------------------------- without the hook


@pytest.fixture(scope="module")
def bare_validator(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Validator]:
    """Audit hook off: only the kernel-level layers (namespace, non-dumpable, rlimits)."""
    yield from _start(tmp_path_factory.mktemp("bare-validator"), audit_hook="false")


def test_kernel_layers_contain_without_the_audit_hook(bare_validator: Validator) -> None:
    secret = (
        MARK
        + "import os\n"
        + "data = open(f'/proc/{os.getppid()}/environ', 'rb').read()\n"
        + "raise RuntimeError('LEAK ' + data.decode(errors='replace'))\n"
    )
    err = _error(bare_validator.check(secret))
    assert KEY not in err
    assert "LEAK" not in err
    assert "PermissionError" in err
    if not bare_validator.ready()["network_isolated"]:
        pytest.skip("unprivileged network namespaces are not available here")
    net = (
        MARK
        + "import socket\n"
        + f"socket.create_connection(('127.0.0.1', {bare_validator.port}), timeout=3)\n"
        + "raise RuntimeError('LEAK connected')\n"
    )
    err = _error(bare_validator.check(net))
    assert "LEAK" not in err


# --------------------------------------------------------------------------- the main API


def _api(tmp_path: Path, url: str, **validator: Any) -> TestClient:
    settings = Settings(
        dags_folder=tmp_path,
        subfolder="",
        import_check_on_deploy=True,
        validator=ValidatorSettings(url=url, retries=0, **validator),
    )
    return TestClient(create_app(build_services(settings, AuthBackend())))


def test_deploy_goes_through_the_validator(
    validator: Validator, tmp_path: Path, simple_spec_data: dict[str, Any]
) -> None:
    from autoair.validation import pool

    pool.shutdown_pool()
    client = _api(tmp_path, validator.url)
    res = client.put("/api/specs/simple", json={"spec": simple_spec_data}).json()
    assert res["deployed"], res
    assert res["report"]["import_check"]["dag_ids"] == ["simple"]
    assert pool._pool is None  # no local worker ran the DAG code
    assert (
        'autoair_import_checks_total{backend="remote",result="ok"}'
        in client.get("/api/metrics").text
    )


def test_deploy_fails_open_by_default_when_the_validator_is_down(
    tmp_path: Path, simple_spec_data: dict[str, Any]
) -> None:
    from autoair.validation import pool

    pool.shutdown_pool()
    client = _api(tmp_path, f"http://127.0.0.1:{_free_port()}")
    res = client.put("/api/specs/simple", json={"spec": simple_spec_data}).json()
    assert res["deployed"], res
    problems = [p for p in res["report"]["problems"] if p["code"] == "import_service"]
    assert [p["severity"] for p in problems] == ["warning"]
    assert "fail open" in problems[0]["message"]
    assert (tmp_path / "simple.py").exists()
    assert pool._pool is None  # the DAG code still never ran in this process


def test_deploy_fails_closed_when_the_validator_is_down(
    tmp_path: Path, simple_spec_data: dict[str, Any]
) -> None:
    from autoair.validation import pool

    pool.shutdown_pool()
    client = _api(tmp_path, f"http://127.0.0.1:{_free_port()}", fail_mode="closed")
    res = client.put("/api/specs/simple", json={"spec": simple_spec_data}).json()
    assert res["deployed"] is False
    problems = [p for p in res["report"]["problems"] if p["code"] == "import_service"]
    assert problems
    assert problems[0]["severity"] == "error"
    assert "deploys are blocked (fail closed)" in problems[0]["message"]
    assert not (tmp_path / "simple.py").exists()
    assert pool._pool is None
    live = client.post("/api/validate?import_check=true", json=simple_spec_data).json()
    assert not live["ok"]
    assert any(p["code"] == "import_service" for p in live["problems"])


def test_checks_get_private_homes(validator: Validator) -> None:
    """A check cannot plant airflow.cfg / user site files for the next worker's warm-up."""
    plant = (
        MARK
        + "import os\n"
        + "home = os.environ['AIRFLOW_HOME']\n"
        + "open(os.path.join(home, 'planted'), 'w').write('x')\n"
        + "raise RuntimeError('planted in ' + home)\n"
    )
    assert "planted in" in _error(validator.check(plant))
    probe = (
        MARK
        + "import os\n"
        + "home = os.environ['AIRFLOW_HOME']\n"
        + "seen = os.path.exists(os.path.join(home, 'planted'))\n"
        + "flag = os.environ['PYTHONNOUSERSITE']\n"
        + "raise RuntimeError(('seen' if seen else 'clean') + ' ' + flag)\n"
    )
    assert "clean 1" in _error(validator.check(probe))


def test_ctypes_symbol_lookup_is_blocked(validator: Validator) -> None:
    code = MARK + "import ctypes\nctypes.pythonapi.Py_GetVersion\nraise RuntimeError('LEAK')\n"
    assert "LEAK" not in _error(validator.check(code))


# --------------------------------------------------------------------------- test runs

RUN_DAG = """import os
import socket
from airflow.sdk import DAG, task
from airflow.providers.standard.operators.bash import BashOperator

with DAG("remote_run", schedule=None):
    BashOperator(task_id="hello", bash_command="echo hi from the validator")

    @task
    def add():
        return 40 + 2

    @task
    def parent_env():
        return open(f"/proc/{os.getppid()}/environ", "rb").read().decode(errors="replace")

    @task
    def key_env():
        return sorted(k for k in os.environ if k.startswith("AIRFLOW_CONN_"))

    @task
    def call_service(port: int = 0):
        with socket.create_connection(("127.0.0.1", int(os.environ.get("PROBE_PORT", port)))) as s:
            return s.recv(16).decode()

    add()
    parent_env()
    key_env()
    call_service({port})
"""


def _run(validator: Validator, task_id: str, port: int = 0) -> Any:
    from autoair.runner import run_task_test

    return run_task_test(
        RUN_DAG.replace("{port}", str(port)),
        "remote_run",
        task_id,
        timeout=60,
        validator=validator.cfg(),
    )


def test_task_runs_execute_on_the_validator(
    validator: Validator, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autoair import runner

    def no_local(*a: Any, **kw: Any) -> Any:
        raise AssertionError("the test run ran on the main server")

    monkeypatch.setattr(runner, "_run_child", no_local)
    hello = _run(validator, "hello")
    assert hello.state == "success", hello.log
    assert hello.return_value == "hi from the validator"
    assert _run(validator, "add").return_value == 42
    metrics = httpx2.get(f"{validator.url}/metrics", timeout=5).text
    assert 'autoair_validator_runs_total{kind="task",state="success"}' in metrics


def test_a_run_cannot_read_the_validator_environment(validator: Validator) -> None:
    leak = _run(validator, "parent_env")
    assert leak.state == "failed"
    assert KEY not in (leak.error or "") + leak.log + str(leak.return_value)
    assert _run(validator, "key_env").return_value == []


def test_runs_may_reach_the_network_by_default(validator: Validator) -> None:
    import threading

    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)

        def answer() -> None:
            conn, _ = server.accept()
            with conn:
                conn.sendall(b"pong")

        thread = threading.Thread(target=answer, daemon=True)
        thread.start()
        result = _run(validator, "call_service", server.getsockname()[1])
        thread.join(5)
    assert result.state == "success", result.error
    assert result.return_value == "pong"


def test_dag_runs_execute_on_the_validator(validator: Validator) -> None:
    from autoair.runner import run_dag_test

    src = (
        RUN_DAG.replace("{port}", "1")
        .replace("    call_service(1)\n", "")
        .replace("    parent_env()\n", "")
    )
    result = run_dag_test(src, "remote_run", timeout=120, validator=validator.cfg())
    states = {t.task_id: t.state for t in result.tasks}
    assert result.mode == "memory"
    assert states["hello"] == states["add"] == "success", result.log


def test_api_test_run_fails_closed_when_the_validator_is_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autoair import runner

    def no_local(*a: Any, **kw: Any) -> Any:
        raise AssertionError("fail_mode=open must not run the test on the main server")

    monkeypatch.setattr(runner, "_run_child", no_local)
    client = _api(tmp_path, f"http://127.0.0.1:{_free_port()}")  # fail_mode open (default)
    spec = {
        "dag_id": "down",
        "nodes": [
            {
                "kind": "operator",
                "id": "a",
                "task_id": "a",
                "operator": "airflow.providers.standard.operators.bash.BashOperator",
                "params": {"bash_command": "echo hi"},
            }
        ],
    }
    resp = client.post("/api/runner/test-task", json={"spec": spec, "task_node_id": "a"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "error"
    assert "test-run service is unavailable" in resp.json()["error"]
