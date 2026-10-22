"""Deploy target chosen in the UI: discovery of a git-sync style checkout, test, save, deploy."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from autoair.api.app import build_services, create_app
from autoair.auth import AuthBackend
from autoair.deploy_setup import git_checkout, suggested_provider
from autoair.settings import Settings

pytestmark = pytest.mark.airflow
SPEC = {
    "dag_id": "pushed_dag",
    "nodes": [
        {
            "kind": "operator",
            "id": "a",
            "task_id": "a",
            "operator": "airflow.providers.standard.operators.empty.EmptyOperator",
        }
    ],
}


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, Path]:
    """A bare 'remote' and a checkout of it whose dags/ folder Airflow reads (like git-sync)."""
    bare = tmp_path / "remote.git"
    git("init", "--bare", "-q", "-b", "main", str(bare), cwd=tmp_path)
    work = tmp_path / "checkout"
    git("clone", "-q", str(bare), str(work), cwd=tmp_path)
    (work / "dags").mkdir()
    (work / "dags" / ".keep").write_text("")
    git("-c", "user.email=a@b", "-c", "user.name=a", "add", ".", cwd=work)
    git("-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "init", cwd=work)
    git("push", "-q", "origin", "main", cwd=work)
    return bare, work / "dags"


def test_checkout_discovery(repo):
    bare, dags = repo
    remote, branch, subdir = git_checkout(dags)
    assert remote == str(bare)
    assert branch == "main"
    assert subdir == "dags"
    assert suggested_provider("https://github.com/acme/dags.git") == "github"
    assert suggested_provider("git@gitlab.example.com:acme/dags.git") == "gitlab"
    assert suggested_provider("file:///srv/dags.git") == "local"


def test_choose_target_in_ui_and_deploy(repo, tmp_path):
    bare, dags = repo
    settings = Settings(
        dags_folder=dags, subfolder="autoair", ai={"enabled": False}, import_check_on_deploy=False
    )
    client = TestClient(create_app(build_services(settings, AuthBackend())))
    info = client.get("/api/deploy-target").json()
    assert info["source"] == "settings"
    checkout = next(c for c in info["candidates"] if c["kind"] == "git-checkout")
    assert checkout["branch"] == "main"
    assert checkout["subdir"] == "dags"

    proposal = checkout["settings"]
    proposal["git"].update(
        {"repo": f"file://{bare}", "mode": "push", "cache_dir": str(tmp_path / "cache")}
    )
    assert client.post("/api/deploy-target/test", json=proposal).json()["ok"] is True
    saved = client.put("/api/deploy-target", json=proposal)
    assert saved.status_code == 200, saved.text
    assert saved.json()["source"] == "ui"
    assert saved.json()["status"]["kind"] == "git"

    deployed = client.put("/api/specs/pushed_dag", json={"spec": SPEC}).json()
    assert deployed["deployed"], deployed
    files = git("ls-tree", "-r", "--name-only", "main", cwd=bare)
    assert "dags/pushed_dag.py" in files.splitlines()

    # The choice survives a restart (no env change), and can be reset.
    again = TestClient(create_app(build_services(settings, AuthBackend())))
    assert again.get("/api/deploy-target").json()["source"] == "ui"
    reset = again.delete("/api/deploy-target").json()
    assert reset["source"] == "settings"
    assert reset["settings"]["mode"] == "local"


def test_rejects_unusable_targets_and_secrets(repo, tmp_path):
    _bare, dags = repo
    settings = Settings(dags_folder=dags, subfolder="", ai={"enabled": False})
    client = TestClient(create_app(build_services(settings, AuthBackend())))
    bad = {"mode": "git", "git": {"provider": "local", "repo": f"file://{tmp_path}/missing.git"}}
    assert client.post("/api/deploy-target/test", json=bad).json()["ok"] is False
    assert client.put("/api/deploy-target", json=bad).status_code == 400
    secret = {"mode": "git", "git": {"provider": "github", "repo": "a/b", "token": "ghp_x"}}
    put = client.put("/api/deploy-target", json=secret)
    assert put.status_code == 400
    assert "Airflow connection" in put.json()["detail"]


def test_object_storage_is_offered_and_accepted(tmp_path, monkeypatch):
    """Object storage targets are listed (an S3DagBundle and a manual prefix) and can be
    switched to from the dialog, like git and local ones."""
    monkeypatch.setattr(
        "autoair.deploy_setup._bundles",
        lambda: [
            {
                "name": "s3-dags",
                "classpath": "airflow.providers.amazon.aws.bundles.s3.S3DagBundle",
                "kwargs": {"bucket_name": "acme", "prefix": "dags/", "aws_conn_id": "aws_dags"},
            }
        ],
    )
    settings = Settings(
        dags_folder=tmp_path, subfolder="", ai={"enabled": False}, import_check_on_deploy=False
    )
    client = TestClient(create_app(build_services(settings, AuthBackend())))
    candidates = client.get("/api/deploy-target").json()["candidates"]
    bundle = next(c for c in candidates if c["kind"] == "object-bundle")
    assert bundle["object_url"] == "s3://acme/dags"
    assert bundle["conn_id"] == "aws_dags"
    assert bundle["settings"]["mode"] == "object"
    manual = next(c for c in candidates if c["kind"] == "object")
    assert manual["settings"]["mode"] == "object"

    proposal = {**manual["settings"], "object_url": "memory://autoair-ui-switch/dags"}
    assert client.post("/api/deploy-target/test", json=proposal).json()["ok"] is True
    saved = client.put("/api/deploy-target", json=proposal)
    assert saved.status_code == 200, saved.text
    assert saved.json()["status"]["kind"] == "object"
    # The active object target prefills the manual option.
    again = next(c for c in saved.json()["candidates"] if c["kind"] == "object")
    assert again["object_url"] == "memory://autoair-ui-switch/dags"

    secret = {**proposal, "storage_options": {"secret": "s3cr3t"}}
    assert client.put("/api/deploy-target", json=secret).status_code == 400


def test_object_storage_hidden_when_not_allowed(tmp_path):
    settings = Settings(
        dags_folder=tmp_path,
        subfolder="",
        ai={"enabled": False},
        storage={"allowed_targets": ["local", "git"]},
    )
    client = TestClient(create_app(build_services(settings, AuthBackend())))
    kinds = {c["kind"] for c in client.get("/api/deploy-target").json()["candidates"]}
    assert not kinds & {"object", "object-bundle"}
    put = client.put("/api/deploy-target", json={"mode": "object", "object_url": "memory://x"})
    assert put.status_code == 400
