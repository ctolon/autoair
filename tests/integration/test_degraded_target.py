"""A git deploy target that cannot be built at start (its Airflow connection does not exist
yet) must not take AutoAir down: the app mounts, /api/ready reports the error and the
first deploy builds the target again."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from autoair.api.app import build_services, create_app
from autoair.auth import AuthBackend
from autoair.settings import Settings
from autoair.storage.config import UnavailableTarget
from autoair.storage.git import GitTarget
from autoair.storage.git.config import GitCredentials

pytestmark = [
    pytest.mark.airflow,
    pytest.mark.skipif(shutil.which("git") is None, reason="git not installed"),
]
SPEC = {
    "dag_id": "late_dag",
    "nodes": [
        {
            "kind": "operator",
            "id": "a",
            "task_id": "a",
            "operator": "airflow.providers.standard.operators.empty.EmptyOperator",
        }
    ],
}


def git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def bare(tmp_path: Path) -> Path:
    bare = tmp_path / "remote.git"
    git("init", "--bare", "-q", "-b", "main", str(bare))
    work = tmp_path / "seed"
    git("clone", "-q", str(bare), str(work))
    (work / "README.md").write_text("seed\n")
    git("-C", str(work), "add", ".")
    git("-C", str(work), "-c", "user.name=S", "-c", "user.email=s@x", "commit", "-qm", "seed")
    git("-C", str(work), "push", "-q", "origin", "HEAD:main")
    return bare


def test_missing_connection_starts_degraded_and_first_deploy_retries(
    bare: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autoair.storage.git import config as git_config

    connection: dict[str, GitCredentials | None] = {"gitea_dags": None}
    monkeypatch.setattr(git_config, "credentials_from_connection", lambda c: connection.get(c))
    settings = Settings(
        dags_folder=tmp_path / "dags",
        ai={"enabled": False},
        import_check_on_deploy=False,
        storage={
            "mode": "git",
            "git": {
                "provider": "local",
                "conn_id": "gitea_dags",
                "mode": "push",
                "cache_dir": str(tmp_path / "cache"),
            },
        },
    )
    services = build_services(settings, AuthBackend())  # must not raise
    assert isinstance(services.target, UnavailableTarget)
    client = TestClient(create_app(services))

    ready = client.get("/api/ready")
    assert ready.status_code == 503
    assert "git storage needs a repository" in ready.json()["checks"]["storage"]
    assert client.get("/api/meta").status_code == 200
    info = client.get("/api/deploy-target").json()
    assert "git storage needs a repository" in info["error"]
    assert client.get("/api/specs").json() == []

    # A deploy while the connection is still missing fails with the reason.
    refused = client.put("/api/specs/late_dag", json={"spec": SPEC})
    assert 400 <= refused.status_code < 500, refused.text
    assert "unavailable" in refused.text

    # The connection is created later: the next deploy builds the target and writes.
    connection["gitea_dags"] = GitCredentials(repo_url=f"file://{bare}")
    deployed = client.put("/api/specs/late_dag", json={"spec": SPEC}).json()
    assert deployed["deployed"], deployed
    assert isinstance(services.target, GitTarget)
    assert "late_dag.py" in git("--git-dir", str(bare), "ls-tree", "-r", "--name-only", "main")
    assert client.get("/api/ready").json()["checks"]["storage"] == "ok (git)"
