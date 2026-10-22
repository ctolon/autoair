"""Task / DAG test runner (isolated subprocess, no metadata DB)."""

from __future__ import annotations

import pytest

from autoair import runner

pytestmark = pytest.mark.airflow

SOURCE = """
from airflow.sdk import DAG, task
from airflow.providers.standard.operators.bash import BashOperator

with DAG("rt", schedule=None, params={"who": "world"}):
    hello = BashOperator(task_id="hello", bash_command="echo hi {{ params.who }}")

    @task
    def add(x: int, y: int = 2):
        print("adding", x, y)
        return x + y

    total = add(3)

    @task
    def use(value):
        return {"got": value}

    use(total)

    @task
    def boom():
        raise ValueError("kaboom")

    boom()

    @task
    def times_ten(v):
        return v * 10

    times_ten.expand(v=[1, 2, 3])

    @task
    def sleepy():
        import time
        time.sleep(60)

    sleepy()
    hello >> total
"""


def test_bash_operator_echo():
    r = runner.run_task_test(SOURCE, "rt", "hello", timeout=120)
    assert r.state == "success", r.error
    assert r.return_value == "hi world"
    assert "hi world" in r.log


def test_conf_overrides_params():
    r = runner.run_task_test(SOURCE, "rt", "hello", conf={"who": "conf"}, timeout=120)
    assert r.return_value == "hi conf"


def test_python_task_return_value():
    r = runner.run_task_test(SOURCE, "rt", "add", timeout=120)
    assert r.state == "success", r.error
    assert r.return_value == 5
    assert r.return_value_repr == "5"
    assert "adding 3 2" in r.log


def test_mock_xcoms():
    r = runner.run_task_test(SOURCE, "rt", "use", mock_xcoms={"add": 41}, timeout=120)
    assert r.return_value == {"got": 41}


def test_error_reporting():
    r = runner.run_task_test(SOURCE, "rt", "boom", timeout=120)
    assert r.state == "failed"
    assert "ValueError: kaboom" in (r.error or "")


def test_mapped_task_runs_first_index():
    r = runner.run_task_test(SOURCE, "rt", "times_ten", timeout=120)
    assert r.state == "success"
    assert r.mapped
    assert r.return_value == 10


def test_timeout_and_missing_task():
    r = runner.run_task_test(SOURCE, "rt", "sleepy", timeout=5)
    assert r.state == "timeout"
    assert runner.run_task_test(SOURCE, "rt", "nope", timeout=60).state == "not_found"
    bad = runner.run_task_test("raise RuntimeError('x')  # airflow dag", "rt", "a", timeout=60)
    assert bad.state == "import_error"
    assert "RuntimeError" in (bad.error or "")


def test_dag_in_memory():
    src = SOURCE.replace("    sleepy()\n", "")
    r = runner.run_dag_test(src, "rt", timeout=180)
    states = {t.task_id: t.state for t in r.tasks}
    assert r.mode == "memory"
    assert r.state == "failed"  # boom fails
    assert states["hello"] == states["add"] == states["use"] == "success"
    assert states["boom"] == "failed"
    use = next(t for t in r.tasks if t.task_id == "use")
    assert use.return_value_repr == "{'got': 5}"  # real upstream XCom


# --------------------------------------------------------------------------- API


def _client(tmp_path, **settings):
    from fastapi.testclient import TestClient

    from autoair.api.app import build_services, create_api
    from autoair.api.routers import runs
    from autoair.auth import AuthBackend
    from autoair.settings import Settings

    services = build_services(
        Settings(dags_folder=tmp_path, subfolder="", **settings), AuthBackend()
    )
    api = create_api(services)
    api.include_router(runs.router)  # wired in app.py by the app owner
    return TestClient(api)


SPEC = {
    "dag_id": "api_rt",
    "nodes": [
        {"kind": "task_group", "id": "g", "group_id": "grp"},
        {
            "kind": "operator",
            "id": "b",
            "task_id": "say",
            "parent_group": "g",
            "operator": "airflow.providers.standard.operators.bash.BashOperator",
            "params": {"bash_command": "echo {{ ti.xcom_pull(task_ids='up') }}"},
        },
        {
            "kind": "python_task",
            "id": "up",
            "task_id": "up",
            "function_name": "up",
            "code": "return 'from-up'",
        },
    ],
    "edges": [{"source": "up", "target": "b"}],
}


def test_api_test_task_and_policy(tmp_path):
    client = _client(tmp_path)
    r = client.post(
        "/runner/test-task",
        json={"spec": SPEC, "task_node_id": "b", "mock_xcoms": {"up": "mocked"}},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["task_id"] == "grp.say"
    assert body["state"] == "success", body
    assert body["return_value"] == "mocked"

    r = client.post("/runner/test-task", json={"spec": SPEC, "task_node_id": "up"})
    assert r.json()["return_value"] == "from-up"
    assert (
        client.post("/runner/test-task", json={"spec": SPEC, "task_node_id": "x"}).status_code
        == 404
    )
    assert (
        client.post("/runner/test-task", json={"spec": SPEC, "task_node_id": "g"}).status_code
        == 422
    )

    dag = client.post("/runner/test-dag", json={"spec": SPEC})
    assert dag.status_code == 200
    assert dag.json()["state"] == "success"

    locked = _client(tmp_path, allow_python_code=False)
    assert (
        locked.post("/runner/test-task", json={"spec": SPEC, "task_node_id": "b"}).status_code
        == 403
    )


def test_api_catalog_extras(tmp_path):
    client = _client(tmp_path)
    notifiers = client.get("/catalog/notifiers").json()
    fqn = "airflow.providers.smtp.notifications.smtp.SmtpNotifier"
    assert fqn in {n["fqn"] for n in notifiers}
    assert client.get(f"/catalog/notifiers/{fqn}").json()["connection_types"] == ["smtp"]
    assert client.get("/catalog/notifiers/x.Y").status_code == 404
    assert any(d["name"] == "bash" for d in client.get("/catalog/decorators").json())
    assert any(q["scheme"] == "sqs" for q in client.get("/catalog/queue-schemes").json())
    assert any(c["conn_type"] == "smtp" for c in client.get("/catalog/connection-types").json())
    ops = client.get("/catalog/operators").json()
    assert "params_schema" not in ops[0]  # summaries stay small


# --------------------------------------------------------------------------- sandbox


def _isolation_available() -> bool:
    import sys

    from autoair.validation.sandbox import run_in_process_namespace

    return run_in_process_namespace([sys.executable, "-c", "pass"]) == 0


PROBE = """
import os
import subprocess
from airflow.sdk import DAG, task
from airflow.providers.standard.operators.bash import BashOperator

with DAG("probe", schedule=None):
    @task
    def parent_env():
        return open(f"/proc/{{os.getppid()}}/environ", "rb").read().decode(errors="replace")

    @task
    def server_env():
        return open("/proc/{server}/environ", "rb").read().decode(errors="replace")

    @task
    def other_run():
        return open("{other}", encoding="utf-8").read()

    @task
    def own_proc():
        return "Pid" in open("/proc/self/status", encoding="utf-8").read()

    @task
    def shell_out():
        return subprocess.run(["echo", "child"], capture_output=True, text=True).stdout.strip()

    parent_env()
    server_env()
    other_run()
    own_proc()
    shell_out()
    BashOperator(task_id="bash_env", bash_command="cat /proc/{server}/environ")
"""


@pytest.fixture
def probe_source():
    """The probe DAG; the test process plays the server that starts the runs, and another
    run's folder holds a file."""
    import os

    other = os.path.join(runner.runs_root(), "run-other-probe")
    os.makedirs(other, exist_ok=True)
    secret_file = os.path.join(other, "result.json")
    with open(secret_file, "w", encoding="utf-8") as fh:
        fh.write("another run's data")
    yield PROBE.format(server=os.getpid(), other=secret_file)
    import shutil

    shutil.rmtree(other, ignore_errors=True)


@pytest.mark.parametrize("task_id", ["parent_env", "server_env", "other_run"])
def test_task_cannot_read_the_server_environment_or_other_runs(probe_source, task_id):
    """Plugin mode: the api-server stays dumpable, so its /proc entry is readable by any
    process of the same user. A test run must not read it (nor another run's files)."""
    r = runner.run_task_test(probe_source, "probe", task_id, timeout=120)
    assert r.state == "failed", (r.state, r.return_value, r.log[-2000:])
    assert "PATH=" not in (r.error or "") + r.log  # no environment block leaked
    assert "another run's data" not in (r.error or "") + r.log
    assert "PermissionError" in (r.error or "") or "FileNotFoundError" in (r.error or "")


def test_unsandboxed_child_could_read_the_server_environment(probe_source):
    """The leak the sandbox closes (the 0.2.2 behaviour: no audit hook, no namespace)."""
    from autoair.sections import RunnerSettings

    off = runner.local_sandbox(RunnerSettings(process_isolation="off", audit_hook=False))
    r = runner.run_task_test(probe_source, "probe", "server_env", timeout=120, sandbox=off)
    assert r.state == "success", r.error
    assert "PATH=" in str(r.return_value)


def test_normal_task_code_still_works_in_the_sandbox(probe_source):
    own = runner.run_task_test(probe_source, "probe", "own_proc", timeout=120)
    assert own.state == "success", own.error
    assert own.return_value is True
    shell = runner.run_task_test(probe_source, "probe", "shell_out", timeout=120)
    assert shell.state == "success", shell.error
    assert shell.return_value == "child"
    bash = runner.run_task_test(SOURCE, "rt", "hello", timeout=120)
    assert bash.state == "success", bash.error
    py = runner.run_task_test(SOURCE, "rt", "add", timeout=120)
    assert py.return_value == 5


@pytest.mark.skipif(
    not _isolation_available(), reason="unprivileged user + PID namespaces are not allowed here"
)
def test_bash_cannot_read_the_server_environment_with_process_isolation(probe_source):
    r = runner.run_task_test(probe_source, "probe", "bash_env", timeout=120)
    assert "PATH=" not in r.log + str(r.return_value)
    assert r.state == "failed"


@pytest.mark.skipif(_isolation_available(), reason="namespaces are available here")
def test_required_process_isolation_refuses_to_run_without_namespaces(probe_source):
    from autoair.sections import RunnerSettings

    box = runner.local_sandbox(RunnerSettings(process_isolation="required"))
    r = runner.run_task_test(probe_source, "probe", "own_proc", timeout=60, sandbox=box)
    assert r.state == "error"
    assert "process isolation is required" in (r.error or "")
    auto = runner.run_task_test(probe_source, "probe", "own_proc", timeout=60)
    assert auto.state == "success"
    assert "process isolation is not available" in auto.log


# --------------------------------------------------------------------------- mode=db


def _db_client(tmp_path, monkeypatch, role, **runner_cfg):
    from fastapi.testclient import TestClient

    from autoair.api.app import build_services, create_app
    from autoair.api.routers import runs
    from autoair.auth import AuthBackend
    from autoair.iam.config import IamSettings
    from autoair.settings import Settings

    modes: list[str] = []

    def fake_dag_test(source, dag_id, *, mode="memory", **kw):
        modes.append(mode)
        return runner.DagTestResult(state="success", mode="memory" if mode == "memory" else "db")

    monkeypatch.setattr(runs, "run_dag_test", fake_dag_test)
    settings = Settings(
        dags_folder=tmp_path,
        subfolder="",
        iam=IamSettings(anonymous_roles=[role]),
        runner=runner_cfg,
    )
    services = build_services(settings, AuthBackend())
    return TestClient(create_app(services)), modes, services


@pytest.mark.parametrize("role", ["Author", "Admin"])
def test_db_mode_is_off_by_default(tmp_path, monkeypatch, role):
    client, modes, _ = _db_client(tmp_path, monkeypatch, role)
    resp = client.post("/api/runner/test-dag", json={"spec": SPEC, "mode": "db"})
    assert resp.status_code == 403
    assert "runner.allow_db_mode" in resp.json()["detail"]
    # auto never picks db when it is not allowed
    assert (
        client.post("/api/runner/test-dag", json={"spec": SPEC, "mode": "auto"}).status_code == 200
    )
    assert modes == ["memory"]
    meta = client.get("/api/meta").json()["test_runs"]
    assert meta == {"on_validator": False, "db_mode_unavailable": "disabled"}


def test_db_mode_needs_admin_config_when_allowed(tmp_path, monkeypatch):
    client, modes, services = _db_client(tmp_path, monkeypatch, "Author", allow_db_mode=True)
    resp = client.post("/api/runner/test-dag", json={"spec": SPEC, "mode": "db"})
    assert resp.status_code == 403
    assert "admin:config" in resp.json()["detail"]
    assert (
        client.post("/api/runner/test-dag", json={"spec": SPEC, "mode": "auto"}).status_code == 200
    )
    assert modes == ["memory"]
    assert client.get("/api/meta").json()["test_runs"]["db_mode_unavailable"] == "admin_only"
    denied = services.audit.tail(20, None)
    assert any(r.action == "permission_denied" for r in denied)


def test_admin_may_use_db_mode_when_allowed(tmp_path, monkeypatch):
    client, modes, services = _db_client(tmp_path, monkeypatch, "Admin", allow_db_mode=True)
    for mode in ("db", "auto"):
        resp = client.post("/api/runner/test-dag", json={"spec": SPEC, "mode": mode})
        assert resp.status_code == 200, resp.text
    assert modes == ["db", "auto"]
    assert client.get("/api/meta").json()["test_runs"]["db_mode_unavailable"] is None
    records = [r for r in services.audit.tail(20, None) if r.action == "test_run"]
    assert {r.details["requested_mode"] for r in records} == {"db", "auto"}
    assert all(r.details["where"] == "local" for r in records)


@pytest.fixture
def metadata_db(tmp_path, monkeypatch):
    """A private, migrated SQLite metadata DB (never the demo's ``.airflow-dev``)."""
    import os
    import subprocess
    import sys

    home = tmp_path / "airflow-home"
    (home / "dags").mkdir(parents=True)
    env = {
        "AIRFLOW_HOME": str(home),
        "AIRFLOW_CONFIG": str(home / "airflow.cfg"),
        "AIRFLOW__DATABASE__SQL_ALCHEMY_CONN": f"sqlite:///{home / 'airflow.db'}",
        "AIRFLOW__CORE__LOAD_EXAMPLES": "False",
        "AIRFLOW__CORE__DAGS_FOLDER": str(home / "dags"),
    }
    done = subprocess.run(
        [sys.executable, "-m", "airflow", "db", "migrate"],
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-3000:]
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return home


DB_SPEC = {
    "dag_id": "db_mode_rt",
    "nodes": [
        {
            "kind": "python_task",
            "id": "up",
            "task_id": "up",
            "function_name": "up",
            "code": "return 41",
        },
        {
            "kind": "python_task",
            "id": "down",
            "task_id": "down",
            "function_name": "down",
            "code": "print('db mode got', 1)\nreturn 1",
        },
    ],
    "edges": [{"source": "up", "target": "down"}],
}


def test_db_mode_runs_dag_test_against_the_metadata_db(tmp_path, metadata_db):
    """A real ``dag.test()`` run: the generated DAG is serialized first (Airflow 3.1+) and
    the in-process execution API accepts the task calls (3.0's client, 3.2's lifespan)."""
    import sqlite3

    from fastapi.testclient import TestClient

    from autoair.api.app import build_services, create_app
    from autoair.auth import AuthBackend
    from autoair.iam.config import IamSettings
    from autoair.settings import Settings

    out = tmp_path / "out"
    out.mkdir()
    settings = Settings(
        dags_folder=out,
        subfolder="",
        iam=IamSettings(anonymous_roles=["Admin"]),
        runner={"allow_db_mode": True},
    )
    client = TestClient(create_app(build_services(settings, AuthBackend())))
    for _ in range(2):  # the second run finds the DAG serialized already
        resp = client.post(
            "/api/runner/test-dag", json={"spec": DB_SPEC, "mode": "db", "timeout": 240}
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["mode"] == "db", body
        assert body["state"] == "success", (body["error"], body["log"][-4000:])
        assert {t["task_id"]: t["state"] for t in body["tasks"]} == {
            "up": "success",
            "down": "success",
        }
        assert "db mode got 1" in body["log"]
    with sqlite3.connect(metadata_db / "airflow.db") as db:
        runs = db.execute("SELECT state FROM dag_run WHERE dag_id = 'db_mode_rt'").fetchall()
    assert runs == [("success",), ("success",)]


# --------------------------------------------------------------------------- Airflow's files

CFG_PROBE = """
import os
from airflow.sdk import DAG, task

with DAG("cfg_probe", schedule=None):
    @task
    def fernet():
        from airflow.configuration import conf
        return conf.get("core", "fernet_key", fallback="")

    @task
    def read_cfg():
        return open("{cfg}", encoding="utf-8").read()

    @task
    def plant():
        with open(os.path.join("{dags}", "planted.py"), "w", encoding="utf-8") as fh:
            fh.write("x = 1")
        return "planted"

    @task
    def timezone():
        from airflow.configuration import conf
        return conf.get("core", "default_timezone")

    fernet()
    read_cfg()
    plant()
    timezone()
"""


@pytest.fixture
def airflow_home(tmp_path, monkeypatch):
    """An AIRFLOW_HOME whose airflow.cfg holds secrets next to ordinary settings."""
    home = tmp_path / "airflow"
    (home / "dags").mkdir(parents=True)
    (home / "airflow.cfg").write_text(
        "[core]\nfernet_key = cfg-fernet-marker\ndefault_timezone = Europe/Istanbul\n"
        "[database]\nsql_alchemy_conn = postgresql://u:cfg-db-marker@db/airflow\n"
        "[api_auth]\njwt_secret = cfg-jwt-marker\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AIRFLOW_HOME", str(home))
    monkeypatch.delenv("AIRFLOW_CONFIG", raising=False)
    monkeypatch.delenv("AIRFLOW__CORE__DAGS_FOLDER", raising=False)
    return home


def _cfg_run(home, task_id):
    src = CFG_PROBE.replace("{cfg}", str(home / "airflow.cfg")).replace(
        "{dags}", str(home / "dags")
    )
    return runner.run_task_test(src, "cfg_probe", task_id, timeout=120)


def test_runs_do_not_get_the_secrets_of_airflow_cfg(airflow_home):
    fernet = _cfg_run(airflow_home, "fernet")
    assert "cfg-fernet-marker" not in str(fernet.return_value) + fernet.log
    read = _cfg_run(airflow_home, "read_cfg")
    assert read.state == "failed"
    assert "PermissionError" in (read.error or "")
    assert "marker" not in (read.error or "") + read.log
    # the rest of the configuration still applies
    assert _cfg_run(airflow_home, "timezone").return_value == "Europe/Istanbul"


def test_runs_cannot_write_into_the_dag_folders(airflow_home):
    r = _cfg_run(airflow_home, "plant")
    assert r.state == "failed"
    assert "PermissionError" in (r.error or "")
    assert not (airflow_home / "dags" / "planted.py").exists()


def test_sanitized_config_drops_secrets(tmp_path):
    src = tmp_path / "in.cfg"
    src.write_text(
        "[core]\nfernet_key = a\nparallelism = 4\n[database]\nsql_alchemy_conn_cmd = cat x\n"
        "[smtp]\nsmtp_password = p\nsmtp_host = mail\n[secrets]\nbackend_kwargs = {}\n",
        encoding="utf-8",
    )
    out = tmp_path / "out.cfg"
    runner.sanitized_airflow_config(str(src), str(out))
    text = out.read_text(encoding="utf-8")
    assert "parallelism = 4" in text
    assert "smtp_host = mail" in text
    for gone in ("fernet_key", "sql_alchemy_conn_cmd", "smtp_password"):
        assert gone not in text
    runner.sanitized_airflow_config(str(tmp_path / "missing.cfg"), str(out))
    assert out.read_text(encoding="utf-8") == ""


def test_process_isolation_defaults_to_auto_in_every_mode():
    from autoair.sections import RunnerSettings

    assert RunnerSettings().process_isolation == "auto"
    assert runner.local_sandbox(RunnerSettings()).process_isolation == "auto"
    explicit = RunnerSettings(process_isolation="required")
    assert runner.local_sandbox(explicit).process_isolation == "required"


def _plugin_client(tmp_path, **runner_cfg):
    from fastapi.testclient import TestClient

    from autoair.api.app import build_services, create_app
    from autoair.auth import AuthBackend
    from autoair.settings import Settings

    services = build_services(
        Settings(dags_folder=tmp_path, subfolder="", mode="plugin", runner=runner_cfg),
        AuthBackend(),
    )
    return TestClient(create_app(services)), services


def test_plugin_test_run_reports_whether_it_was_isolated(tmp_path, caplog):
    """auto in the plugin too: without namespaces the run still executes (audit hook) and
    its result says it was not fully isolated; one warning per process."""
    client, services = _plugin_client(tmp_path)
    isolated = _isolation_available()
    saved = runner._warned_not_isolated
    runner._warned_not_isolated = False
    try:
        with caplog.at_level("WARNING", logger="autoair.runner"):
            for _ in range(2):
                body = client.post(
                    "/api/runner/test-task", json={"spec": SPEC, "task_node_id": "up"}
                ).json()
                assert body["state"] == "success", body
                assert body["where"] == "local"
                assert body["process_isolated"] is isolated
    finally:
        runner._warned_not_isolated = saved
    warnings = [r for r in caplog.records if "not fully isolated" in r.getMessage()]
    assert len(warnings) == (0 if isolated else 1)
    record = next(r for r in services.audit.tail(10, None) if r.action == "test_run")
    assert record.details["where"] == "local"
    assert record.details["process_isolated"] is isolated


@pytest.mark.skipif(_isolation_available(), reason="namespaces are available here")
def test_plugin_test_run_refuses_without_namespaces_when_required(tmp_path):
    client, services = _plugin_client(tmp_path, process_isolation="required")
    body = client.post("/api/runner/test-task", json={"spec": SPEC, "task_node_id": "up"}).json()
    assert body["state"] == "error", body
    assert "process isolation is required" in body["error"]
    record = next(r for r in services.audit.tail(10, None) if r.action == "test_run")
    assert record.details["where"] == "local"
