"""GET /api/config, feature flags on the API and in /api/meta, configurable request limits."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import Depends, HTTPException, Request
from fastapi.testclient import TestClient

from autoair import sections
from autoair.api.app import build_services, create_app
from autoair.auth import AuthBackend
from autoair.settings import Settings

pytestmark = pytest.mark.airflow


@pytest.fixture(autouse=True)
def _reset_sections():
    yield
    sections.reset()


def client(tmp_path: Path, auth: AuthBackend | None = None, **settings: object) -> TestClient:
    s = Settings(dags_folder=tmp_path, subfolder="", **settings)  # type: ignore[arg-type]
    return TestClient(create_app(build_services(s, auth or AuthBackend())))


def _admin_only(request: Request) -> None:
    if request.headers.get("x-admin") != "yes":
        raise HTTPException(status_code=403, detail="Forbidden")


def test_config_is_hidden_by_default(tmp_path):
    assert client(tmp_path).get("/api/config").status_code == 403


def test_config_requires_admin(tmp_path):
    auth = AuthBackend(admin=[Depends(_admin_only)], name="test")
    c = client(tmp_path, auth, expose_config="true")
    assert c.get("/api/config").status_code == 403
    assert c.get("/api/config", headers={"x-admin": "yes"}).status_code == 200


def test_config_masks_secrets_and_reports_sources(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTOAIR_TEAM", "data")
    c = client(
        tmp_path,
        expose_config="true",
        collab_secret="s3cr3t",
        storage={"git": {"token": "ghp_x"}, "storage_options": {"key": "AKIA"}},
    )
    body = c.get("/api/config").json()
    assert body["expose"] == "true"
    options = {o["key"]: o for o in body["options"]}
    assert options["team"]["value"] == "data"
    assert options["team"]["source"] == "env"
    assert options["team"]["env"] == "AUTOAIR_TEAM"
    assert options["subfolder"]["source"] == "init"
    assert options["expose_config"]["source"] == "init"
    assert options["runner.max_timeout"]["source"] == "default"
    assert options["runner.max_timeout"]["default"] == 600
    for key in ("collab_secret", "storage.git.token", "storage.storage_options"):
        assert options[key]["secret"] is True
        assert options[key]["value"] == "***"
    assert "s3cr3t" not in c.get("/api/config").text
    assert "ghp_x" not in c.get("/api/config").text
    assert options["storage.git.mode"]["type"] == "'pr' | 'push'"
    assert options["storage.git.mode"]["description"]


def test_config_non_sensitive_only_omits_secrets(tmp_path):
    c = client(tmp_path, expose_config="non-sensitive-only", collab_secret="s3cr3t")
    keys = {o["key"] for o in c.get("/api/config").json()["options"]}
    assert "collab_secret" not in keys
    assert "storage.git.token" not in keys
    assert "subfolder" in keys


def test_meta_reports_effective_features(tmp_path):
    meta = (
        client(
            tmp_path,
            features={"marketplace": False, "collab": False},
            expose_config="true",
        )
        .get("/api/meta")
        .json()
    )
    assert meta["features"]["marketplace"] is False
    assert meta["features"]["collab"] is False
    assert meta["features"]["templates"] is True
    assert meta["collab_enabled"] is False
    assert meta["expose_config"] == "true"


@pytest.mark.parametrize(
    ("feature", "method", "path", "body"),
    [
        ("marketplace", "get", "/api/marketplace", None),
        ("templates", "get", "/api/templates", None),
        ("imports", "post", "/api/interchange/yaml/import", {"source": "dag_id: x\n"}),
        ("test_runs", "post", "/api/runner/test-dag", {"spec": {"dag_id": "d", "nodes": []}}),
        ("runs", "post", "/api/airflow/dags/d/trigger", {}),
        ("custom_icons", "delete", "/api/icons/custom:x", None),
        ("deploy", "delete", "/api/specs/d", None),
        ("collab", "post", "/api/collab/d/ticket", None),
    ],
)
def test_disabled_features_answer_404(tmp_path, feature, method, path, body):
    c = client(tmp_path, features={feature: False})
    kwargs = {"json": body} if body is not None else {}
    resp = getattr(c, method)(path, **kwargs)
    assert resp.status_code == 404, resp.text
    assert feature in resp.json()["detail"]


def test_enabled_features_are_served(tmp_path):
    c = client(tmp_path)
    assert c.get("/api/templates").status_code == 200
    assert c.get("/api/marketplace").status_code == 200


def test_configured_request_limits(tmp_path):
    c = client(tmp_path, interchange={"max_source_chars": 1000}, editor={"max_source_chars": 1000})
    big = "x" * 2000
    resp = c.post("/api/interchange/yaml/import", json={"source": big})
    assert resp.status_code == 422
    assert "AUTOAIR_INTERCHANGE__MAX_SOURCE_CHARS" in resp.json()["detail"]
    resp = c.post("/api/lsp/shell/lint", json={"code": big})
    assert resp.status_code == 422
    assert "AUTOAIR_EDITOR__MAX_SOURCE_CHARS" in resp.json()["detail"]


def test_runner_timeout_is_capped(tmp_path):
    c = client(tmp_path, runner={"max_timeout": 10})
    spec = {"dag_id": "d", "nodes": []}
    resp = c.post("/api/runner/test-dag", json={"spec": spec, "timeout": 60})
    assert resp.status_code == 422
    assert "AUTOAIR_RUNNER__MAX_TIMEOUT" in resp.json()["detail"]
