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
