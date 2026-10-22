"""POST /api/interchange/python/sync: code mode applies edited DAG code to the spec."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from autoair import sections
from autoair.api.app import build_services, create_app
from autoair.auth import AuthBackend
from autoair.settings import Settings

pytestmark = pytest.mark.airflow

SPEC = {
    "dag_id": "code_mode",
    "nodes": [
        {
            "id": "a",
            "kind": "operator",
            "task_id": "hello",
            "operator": "airflow.providers.standard.operators.bash.BashOperator",
            "params": {"bash_command": "echo hello"},
            "position": {"x": 40, "y": 60},
        }
    ],
}


@pytest.fixture(autouse=True)
def _reset_sections():
    yield
    sections.reset()


def client(tmp_path: Path, **settings: object) -> TestClient:
    # Unauthenticated callers are Viewers by default; these tests sync and deploy code.
    settings.setdefault("iam", {"anonymous_roles": ["Admin"]})
    s = Settings(dags_folder=tmp_path, subfolder="", **settings)  # type: ignore[arg-type]
    return TestClient(create_app(build_services(s, AuthBackend())))


def test_generated_code_round_trips_and_edits_show_in_the_diff(tmp_path):
    c = client(tmp_path)
    code = c.post("/api/codegen", json=SPEC).json()["code"]
    same = c.post("/api/interchange/python/sync", json={"source": code, "current": SPEC}).json()
    assert same["ok"], same["problems"]
    assert same["spec"]["nodes"][0]["id"] == "a"
    position = same["spec"]["nodes"][0]["position"]
    assert (position["x"], position["y"]) == (40, 60)
    assert not same["diff"]["added_nodes"]
    assert not same["diff"]["removed_nodes"]

    edited = code.replace("echo hello", "echo edited")
    res = c.post("/api/interchange/python/sync", json={"source": edited, "current": SPEC}).json()
    assert res["ok"]
    assert res["spec"]["nodes"][0]["params"]["bash_command"] == "echo edited"
    assert [n["id"] for n in res["diff"]["changed_nodes"]] == ["a"]


def test_broken_code_reports_located_problems(tmp_path):
    c = client(tmp_path)
    syntax = c.post("/api/interchange/python/sync", json={"source": "def (:\n"}).json()
    assert syntax["ok"] is False
    assert syntax["spec"] is None
    assert syntax["problems"][0]["line"] == 1
    runtime = "from airflow.sdk import DAG\n\nx = 1\nraise RuntimeError('nope')\n"
    res = c.post("/api/interchange/python/sync", json={"source": runtime}).json()
    assert res["ok"] is False
    assert any(p["line"] == 4 for p in res["problems"]), res["problems"]


def test_sync_obeys_the_code_editor_flag_and_python_policy(tmp_path):
    off = client(tmp_path, features={"code_editor": False})
    assert off.post("/api/interchange/python/sync", json={"source": "x=1"}).status_code == 404
    no_python = client(tmp_path, allow_python_code=False)
    assert no_python.post("/api/interchange/python/sync", json={"source": "x=1"}).status_code == 403


def test_at_least_one_editor_must_be_enabled(tmp_path):
    with pytest.raises(ValueError, match="at least one"):
        Settings(features={"visual_editor": False, "code_editor": False})  # type: ignore[arg-type]
    flags = client(tmp_path, features={"visual_editor": False}).get("/api/meta").json()["features"]
    assert flags["visual_editor"] is False
    assert flags["code_editor"] is True
    assert flags["code_edit_inline"] is False
    assert flags["ide_language_server"] is False
