"""Source-preserving deploys against a real Airflow importer and DagBag: the import ->
canvas change -> patch -> round trip -> DagBag chain, ejected-file detection on folder and
git targets, the source preview route and its permissions."""

from __future__ import annotations

import copy
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from autoair.iam import model as P
from autoair.preserve import fingerprint
from autoair.storage.config import GitStorageSettings, StorageSettings
from autoair.validation.dagbag_check import check_source
from tests.integration.test_iam_api import as_role, make_client, make_services

pytestmark = pytest.mark.airflow

HAND = '''"""Nightly load, written by hand."""

from __future__ import annotations

import pendulum

from airflow.providers.standard.operators.bash import BashOperator
from airflow.providers.standard.operators.empty import EmptyOperator
from airflow.sdk import DAG, TaskGroup, task

# keep the defaults in one place
DEFAULTS = {"owner": "data"}

with DAG(
    dag_id="hand_load",
    schedule="@daily",  # nightly
    start_date=pendulum.datetime(2026, 1, 1, tz="UTC"),
    catchup=False,
    default_args=DEFAULTS,
) as dag:
    start = EmptyOperator(task_id="start")

    with TaskGroup("load") as load:
        users = BashOperator(task_id="users",   bash_command="echo users")
        orders = BashOperator(
            task_id="orders",
            bash_command="echo orders",
            retries=2,  # the orders API is flaky
        )

    @task
    def summarize():
        # count what was loaded
        return 2

    done = summarize()

    start >> load >> done
'''


def client(tmp_path: Path, **settings: Any) -> TestClient:
    return make_client(make_services(tmp_path, import_check_on_deploy=True, **settings))


def imported(c: TestClient, source: str = HAND) -> dict[str, Any]:
    res = c.post(
        "/api/interchange/python/import",
        json={"source": source, "filename": "hand_load.py"},
        headers=as_role("Admin"),
    )
    assert res.status_code == 200, res.text
    [dag] = res.json()["dags"]
    spec: dict[str, Any] = dag["spec"]
    assert spec["source"]["origin"] == "python"
    assert spec["source"]["code"] == source
    return spec


def task_node(spec: dict[str, Any], task_id: str) -> dict[str, Any]:
    return next(n for n in spec["nodes"] if n.get("task_id") == task_id)


def deploy(c: TestClient, spec: dict[str, Any], role: str = "Admin", **body: Any) -> Any:
    return c.put(f"/api/specs/{spec['dag_id']}", json={"spec": spec, **body}, headers=as_role(role))


def test_import_edit_deploy_keeps_the_hand_written_file(tmp_path: Path) -> None:
    c = client(tmp_path)
    spec = imported(c)
    first = deploy(c, spec)
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["deployed"], body
    assert body["source"]["policy"] == "preserve"
    assert body["source"]["action"] == "unchanged"
    path = tmp_path / "dags" / "hand_load.py"
    assert path.read_text() == HAND  # written exactly as the user wrote it

    stored = c.get("/api/specs/hand_load", headers=as_role("Admin")).json()
    changed = copy.deepcopy(stored["spec"])
    task_node(changed, "orders")["retries"] = 5
    task_node(changed, "users")["params"]["bash_command"] = "echo users --all"
    changed["tags"] = ["nightly"]
    preview = c.post(
        "/api/specs/hand_load/source-preview", json={"spec": changed}, headers=as_role("Admin")
    )
    assert preview.status_code == 200, preview.text
    plan = preview.json()
    assert plan["action"] == "preserve", plan
    assert "+            retries=5,  # the orders API is flaky" in plan["diff"]
    assert path.read_text() == HAND  # a preview writes nothing

    second = deploy(c, changed, base_etag=stored["etag"])
    assert second.status_code == 200, second.text
    assert second.json()["deployed"], second.json()
    written = path.read_text()
    for kept in ("# keep the defaults in one place", "# nightly", "# count what was loaded"):
        assert kept in written
    assert 'users = BashOperator(task_id="users",   bash_command="echo users --all")' in written
    assert "retries=5,  # the orders API is flaky" in written
    assert "tags=" in written
    # the deployed file passes Airflow's DagBag with the change in place
    result = check_source(written, filename="hand_load.py", use_pool=False)
    assert result.ok, result.import_errors
    assert "load.orders" in result.task_ids["hand_load"]


CLASSIC_PY = '''"""Classic PythonOperator with a callable defined in the file."""

from __future__ import annotations

import json

import pendulum

from airflow.providers.standard.operators.python import PythonOperator
from airflow.sdk import DAG


@staticmethod
def _unused():  # a decorated helper the DAG does not use
    return None


def build_payload(**context):
    # what the API expects
    return json.dumps({"ok": True})


with DAG(
    dag_id="hand_load",
    schedule=None,
    start_date=pendulum.datetime(2026, 1, 1, tz="UTC"),
) as dag:
    payload = PythonOperator(task_id="payload", python_callable=build_payload)
'''


def test_classic_python_operator_file_is_deployed_and_patched(tmp_path: Path) -> None:
    """A PythonOperator whose python_callable is defined in the file keeps it on import, so
    the preserve deploy is not refused and a canvas change patches the file in place."""
    c = client(tmp_path)
    spec = imported(c, CLASSIC_PY)
    value = task_node(spec, "payload")["params"]["python_callable"]
    assert value["$type"] == "function"
    assert "def build_payload(**context):" in value["source"]
    first = deploy(c, spec).json()
    assert first["deployed"], first
    assert first["source"]["policy"] == "preserve"
    path = tmp_path / "dags" / "hand_load.py"
    assert path.read_text() == CLASSIC_PY

    stored = c.get("/api/specs/hand_load", headers=as_role("Admin")).json()
    changed = copy.deepcopy(stored["spec"])
    task_node(changed, "payload")["retries"] = 3
    second = deploy(c, changed, base_etag=stored["etag"]).json()
    assert second["deployed"], str(second["source"])
    written = path.read_text()
    assert "python_callable=build_payload" in written
    assert "# what the API expects" in written
    assert "retries=3" in written
    result = check_source(written, filename="hand_load.py", use_pool=False)
    assert result.ok, result.import_errors


def test_new_task_and_edge_are_inserted_and_verified(tmp_path: Path) -> None:
    c = client(tmp_path)
    spec = imported(c)
    assert deploy(c, spec).json()["deployed"]
    stored = c.get("/api/specs/hand_load", headers=as_role("Admin")).json()
    changed = copy.deepcopy(stored["spec"])
    changed["nodes"].append(
        {
            "kind": "operator",
            "id": "n_notify",
            "task_id": "notify",
            "operator": "airflow.providers.standard.operators.bash.BashOperator",
            "params": {"bash_command": "echo done"},
        }
    )
    done = task_node(changed, "summarize")["id"]
    changed["edges"].append({"source": done, "target": "n_notify"})
    res = deploy(c, changed, base_etag=stored["etag"]).json()
    assert res["deployed"], res
    written = (tmp_path / "dags" / "hand_load.py").read_text()
    assert 'notify = BashOperator(task_id="notify", bash_command="echo done")' in written
    assert written.rstrip().endswith("done >> notify")


def test_unsupported_change_is_refused_in_strict_mode(tmp_path: Path) -> None:
    c = client(tmp_path)
    spec = imported(c)
    assert deploy(c, spec).json()["deployed"]
    stored = c.get("/api/specs/hand_load", headers=as_role("Admin")).json()
    changed = copy.deepcopy(stored["spec"])
    # default_args is written as a variable: replacing it is risky
    changed["default_args"] = {"owner": "ops"}
    changed["source"]["policy"] = "strict-preserve"
    res = deploy(c, changed, base_etag=stored["etag"]).json()
    assert res["deployed"] is False
    assert res["status"] == "source_refused"
    assert res["source"]["refusals"][0]["code"] == "risky"
    assert set(res["source"]["options"]) == {"raw", "generate"}
    assert (tmp_path / "dags" / "hand_load.py").read_text() == HAND  # untouched
    assert res["source"]["current_code"] == HAND  # for "keep as raw code DAG"
    changed["source"]["policy"] = "preserve"
    loose = deploy(c, changed, base_etag=stored["etag"]).json()
    assert loose["deployed"], loose
    assert any("refers to a variable" in w for w in loose["source"]["warnings"])


def test_a_refused_file_can_be_kept_as_raw_code(tmp_path: Path) -> None:
    c = client(tmp_path)
    path = tmp_path / "dags" / "hand_load.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(HAND)  # a hand-written file AutoAir never wrote
    raw = {"dag_id": "hand_load", "raw_code": HAND, "nodes": [], "edges": []}
    res = deploy(c, raw).json()
    assert res["deployed"], res  # the file's own content may take it over
    assert path.read_text().endswith(HAND)
    other = {**raw, "raw_code": HAND.replace("echo users", "echo other")}
    path.write_text(HAND + "# edited elsewhere\n")
    blocked = deploy(c, other, base_etag=res["etag"])
    assert blocked.status_code == 409, blocked.text  # not its content: never overwritten
    assert path.read_text() == HAND + "# edited elsewhere\n"


SIMPLE: dict[str, Any] = {
    "dag_id": "born_here",
    "nodes": [
        {
            "kind": "operator",
            "id": "a",
            "task_id": "a",
            "operator": "airflow.providers.standard.operators.bash.BashOperator",
            "params": {"bash_command": "echo a"},
        }
    ],
}


def test_ejected_folder_file_is_blocked_then_preserved(tmp_path: Path) -> None:
    c = client(tmp_path)
    first = deploy(c, SIMPLE).json()
    assert first["deployed"]
    assert first["source"]["action"] == "generate"
    path = tmp_path / "dags" / "born_here.py"
    generated = path.read_text()
    assert fingerprint.state(generated) == "valid"
    # someone edits the deployed file by hand
    edited = generated.replace("    t_a = BashOperator(", "    # by hand\n    t_a = BashOperator(")
    assert edited != generated
    path.write_text(edited)
    changed = copy.deepcopy(SIMPLE)
    changed["nodes"][0]["retries"] = 3
    blocked = deploy(c, changed, base_etag=first["etag"]).json()
    assert blocked["deployed"] is False
    assert blocked["status"] == "ejected"
    assert blocked["source"]["target_state"] == "ejected"
    assert blocked["source"]["options"] == ["preserve", "raw"]
    assert path.read_text() == edited
    # legacy force does not overwrite an ejected Python file any more
    forced = deploy(c, changed, base_etag=first["etag"], force=True).json()
    assert forced["status"] == "ejected"
    preserved = deploy(c, changed, base_etag=first["etag"], source_resolution="preserve").json()
    assert preserved["deployed"], preserved
    written = path.read_text()
    assert "# by hand" in written
    assert "retries=3" in written
    # the DAG now counts as hand-written: later deploys preserve by default
    stored = preserved["stored_spec"]
    assert stored["source"]["origin"] == "python"
    again = copy.deepcopy(stored)
    again["nodes"][0]["retries"] = 4
    later = deploy(c, again, base_etag=preserved["etag"]).json()
    assert later["deployed"], later
    assert later["source"]["action"] == "preserve"
    assert "# by hand" in path.read_text()


def test_overwrite_of_an_ejected_file_needs_setting_and_permission(tmp_path: Path) -> None:
    roles = {"Writer": ["builder:use", "spec:*", "deploy", "code:execute"]}
    c = client(
        tmp_path,
        preserve={"ejected_files": "overwrite", "allow_force_overwrite": True},
        iam={"roles": roles},
    )
    first = deploy(c, SIMPLE).json()
    path = tmp_path / "dags" / "born_here.py"
    path.write_text(path.read_text() + "# edited\n")
    changed = copy.deepcopy(SIMPLE)
    changed["nodes"][0]["retries"] = 1
    offer = deploy(c, changed, "Writer", base_etag=first["etag"]).json()
    assert offer["status"] == "ejected"
    assert "overwrite" in offer["source"]["options"]
    denied = deploy(c, changed, "Writer", base_etag=first["etag"], source_resolution="overwrite")
    assert denied.json()["status"] == "ejected"
    assert P.DEPLOY_OVERWRITE_EJECTED in denied.json()["message"]
    ok = deploy(c, changed, base_etag=first["etag"], source_resolution="overwrite").json()
    assert ok["deployed"], ok
    assert "# edited" not in path.read_text()


def test_ejected_files_overwrite_needs_allow_force_overwrite(tmp_path: Path) -> None:
    """preserve.ejected_files=overwrite without preserve.allow_force_overwrite acts like
    block: the overwrite is neither offered nor accepted, also for an admin."""
    c = client(tmp_path, preserve={"ejected_files": "overwrite"})
    first = deploy(c, SIMPLE).json()
    path = tmp_path / "dags" / "born_here.py"
    path.write_text(path.read_text() + "# edited\n")
    changed = copy.deepcopy(SIMPLE)
    changed["nodes"][0]["retries"] = 1
    offer = deploy(c, changed, base_etag=first["etag"]).json()
    assert offer["status"] == "ejected"
    assert "overwrite" not in offer["source"]["options"]
    refused = deploy(c, changed, base_etag=first["etag"], source_resolution="overwrite").json()
    assert not refused["deployed"]
    assert "preserve.allow_force_overwrite" in refused["message"]
    assert "# edited" in path.read_text()
    meta = c.get("/api/meta", headers=as_role("Admin")).json()
    assert meta["allow_force_overwrite"] is False
    on = client(tmp_path / "on", preserve={"allow_force_overwrite": True})
    assert on.get("/api/meta", headers=as_role("Admin")).json()["allow_force_overwrite"] is True


def test_source_preview_permissions(tmp_path: Path) -> None:
    c = client(tmp_path)
    url = "/api/specs/born_here/source-preview"
    assert c.post(url, json={"spec": SIMPLE}, headers=as_role("Viewer")).status_code == 403
    plan = c.post(url, json={"spec": SIMPLE}, headers=as_role("Author")).json()
    assert plan["action"] == "generate"
    assert plan["target_state"] == "absent"
    assert "+# autoair:fingerprint sha256=" in plan["diff"]
    yaml = {**SIMPLE, "output_mode": "yaml"}
    assert c.post(url, json={"spec": yaml}, headers=as_role("Author")).json()["applicable"] is False
    off = client(tmp_path / "off", features={"deploy": False})
    assert off.post(url, json={"spec": SIMPLE}, headers=as_role("Admin")).status_code == 404


def test_hand_written_code_needs_code_raw(tmp_path: Path) -> None:
    roles = {"Writer": ["builder:use", "spec:*", "deploy", "code:execute"]}
    c = client(tmp_path, iam={"roles": roles})
    spec = imported(c)
    res = deploy(c, spec, "Writer")  # storing the hand-written file needs code:raw
    assert res.status_code == 403, res.text
    assert "code:raw" in res.text
    # switching to generate (without the hand-written code) deploys the generated file
    spec["source"]["policy"] = "generate"
    spec["source"]["code"] = None
    gen = deploy(c, spec, "Writer").json()
    assert gen["deployed"]
    assert gen["source"]["action"] == "generate"


# --------------------------------------------------------------------------- git target


@pytest.fixture
def bare_repo(tmp_path: Path) -> Path:
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(bare)], check=True)
    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", "-q", str(bare), str(seed)], check=True)
    (seed / "dags").mkdir()
    (seed / "dags" / "hand_load.py").write_text(HAND)
    commit(seed, "a hand-written DAG")
    return bare


def commit(work: Path, message: str) -> None:
    git = ["git", "-C", str(work), "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", message], check=True)
    subprocess.run([*git, "push", "-q", "origin", "HEAD:main"], check=True)


def git_client(tmp_path: Path, repo: Path) -> TestClient:
    storage = StorageSettings(
        mode="git",
        git=GitStorageSettings(  # type: ignore[arg-type]
            provider="local",
            repo=f"file://{repo}",
            mode="push",
            subdir="dags",
            cache_dir=tmp_path / "cache",
            fetch_interval=0,  # see outside commits at once
        ),
    )
    return client(tmp_path / "svc", storage=storage)


def show(bare: Path, path: str) -> str:
    return subprocess.run(
        ["git", "--git-dir", str(bare), "show", f"main:{path}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def test_git_target_patches_the_committed_hand_written_file(
    tmp_path: Path, bare_repo: Path
) -> None:
    c = git_client(tmp_path, bare_repo)
    spec = imported(c)
    # the repository already holds this very file (foreign to AutoAir): patched in place
    task_node(spec, "orders")["retries"] = 7
    res = deploy(c, spec).json()
    assert res["deployed"], res
    assert res["source"]["base"] == "target"
    assert res["source"]["target_state"] in (
        "clean",
        "foreign",
    )
    content = show(bare_repo, "dags/hand_load.py")
    assert "retries=7,  # the orders API is flaky" in content
    assert "# keep the defaults in one place" in content

    # an outside commit edits the file: the next deploy merges instead of overwriting
    seed = tmp_path / "seed"
    subprocess.run(["git", "-C", str(seed), "pull", "-q", "origin", "main"], check=True)
    target = seed / "dags" / "hand_load.py"
    target.write_text(target.read_text().replace("echo users", "echo users --fast"))
    commit(seed, "tweak by hand")
    stored = c.get("/api/specs/hand_load", headers=as_role("Admin")).json()
    changed = copy.deepcopy(stored["spec"])
    task_node(changed, "orders")["params"]["bash_command"] = "echo orders --v2"
    merged = deploy(c, changed, base_etag=stored["etag"]).json()
    assert merged["deployed"], merged
    assert any("changed outside AutoAir" in w for w in merged["source"]["warnings"])
    content = show(bare_repo, "dags/hand_load.py")
    assert "echo users --fast" in content
    assert "echo orders --v2" in content
