"""API hardening: privileged routes need DAG edit permission, no in-process code
execution from previews, bounded uploads, cache validators, configured timeouts."""

from __future__ import annotations

import base64
import io
from pathlib import Path
from typing import Annotated

import pytest
from fastapi import Depends, HTTPException, Request
from fastapi.testclient import TestClient
from PIL import Image

from autoair.api.app import build_services, create_app
from autoair.auth import AuthBackend
from autoair.sections import IconSettings
from autoair.settings import Settings

pytestmark = pytest.mark.airflow

SIMPLE = {
    "dag_id": "simple",
    "nodes": [
        {
            "kind": "operator",
            "id": "a",
            "task_id": "a",
            "operator": "airflow.providers.standard.operators.empty.EmptyOperator",
        }
    ],
}


def current_user(request: Request) -> str:
    return request.headers.get("x-user", "viewer")


def can_edit(request: Request, user: Annotated[str, Depends(current_user)]) -> None:
    """Shaped like Airflow's ``requires_access_dag("PUT")``: a sub-dependency resolves the
    user (string annotations under ``from __future__ import annotations``), 403 if denied."""
    if user != "editor":
        raise HTTPException(status_code=403, detail="Forbidden")


def rbac() -> AuthBackend:
    return AuthBackend(write_dag=[Depends(can_edit)], admin=[Depends(can_edit)], name="test")


def client(tmp_path: Path, auth: AuthBackend | None = None, **settings: object) -> TestClient:
    if auth is None:  # anonymous callers get iam.anonymous_roles (Viewer by default)
        settings.setdefault("iam", {"anonymous_roles": ["Admin"]})
    s = Settings(dags_folder=tmp_path, subfolder="", ai={"enabled": False}, **settings)  # type: ignore[arg-type]
    return TestClient(create_app(build_services(s, auth or AuthBackend())))


EDITOR = {"x-user": "editor"}


def test_import_check_needs_edit_permission(tmp_path):
    c = client(tmp_path, rbac())
    assert c.post("/api/validate", json=SIMPLE).status_code == 200  # static checks: readers ok
    denied = c.post("/api/validate?import_check=true", json=SIMPLE)
    assert denied.status_code == 403
    assert "edit permission" in denied.json()["detail"]
    ok = c.post("/api/validate?import_check=true", json=SIMPLE, headers=EDITOR)
    assert ok.status_code == 200
    assert ok.json()["import_check"] is not None


def test_python_dag_import_needs_edit_permission(tmp_path):
    c = client(tmp_path, rbac())
    body = {"source": "x = 1\n", "filename": "x.py"}
    assert c.post("/api/interchange/python/import", json=body).status_code == 403
    assert c.post("/api/interchange/airflow/some_dag/import").status_code == 403
    assert c.post("/api/interchange/python/import", json=body, headers=EDITOR).status_code == 200


def test_template_writes_and_catalog_refresh_need_edit_permission(tmp_path):
    c = client(tmp_path, rbac())
    tpl = {"id": "mine", "title": "Mine", "kind": "dag", "spec": SIMPLE}
    assert c.get("/api/templates").status_code == 200
    assert c.post("/api/templates", json=tpl).status_code == 403
    response = c.delete("/api/templates/mine")
    assert response.status_code == 403
    assert c.post("/api/catalog/refresh").status_code == 403
    assert c.post("/api/templates", json=tpl, headers=EDITOR).status_code == 200
    assert c.get("/api/templates/mine").json()["title"] == "Mine"
    response = c.delete("/api/templates/mine", headers=EDITOR)
    assert response.json() == {"deleted": True}


def test_next_runs_preview_never_executes_spec_code(tmp_path):
    marker = tmp_path / "executed"
    src = f"def f(x=open({str(marker)!r}, 'w').write('x')):\n    return x\n"
    code = {"$type": "function", "source": src}
    spec = {
        "dag_id": "p",
        "schedule": {
            "type": "assets_or_time",
            "expression": {"op": "asset", "asset": {"name": "a", "extra": {"f": code}}},
            "time": {"type": "preset", "preset": "@daily"},
        },
        "params": {"a": {"default": code}},
        "default_args": {"owner": "x"},
    }
    res = client(tmp_path).post("/api/airflow/next-runs", json={"spec": spec, "count": 2})
    assert res.status_code == 200, res.text
    assert not marker.exists()
    assert len(res.json()["runs"]) == 2


def _png() -> str:
    out = io.BytesIO()
    Image.new("RGBA", (16, 16), (1, 2, 3, 255)).save(out, format="PNG")
    return base64.b64encode(out.getvalue()).decode()


def test_icon_upload_is_bounded_and_served_with_etag(tmp_path):
    c = client(tmp_path)
    huge = "A" * (IconSettings().max_upload_bytes * 2)
    assert c.post("/api/icons", json={"name": "big", "data": huge}).status_code == 422
    info = c.post("/api/icons", json={"name": "dot", "data": _png()}).json()
    first = c.get(f"/api/icons/{info['id']}")
    assert first.status_code == 200
    etag = first.headers["etag"]
    again = c.get(f"/api/icons/{info['id']}", headers={"If-None-Match": etag})
    assert again.status_code == 304
    assert again.content == b""
    assert c.get("/api/icons/custom:dot%0A").status_code == 404


def test_audit_and_runs_limits_are_bounded(tmp_path):
    c = client(tmp_path)
    assert c.get("/api/specs/x/audit?limit=100000").status_code == 422
    assert c.get("/api/airflow/dags/x/runs?limit=0").status_code == 422


def test_settings_reach_services(tmp_path):
    s = Settings(
        dags_folder=tmp_path,
        subfolder="",
        ai={"enabled": False},
        marketplace_timeout=3,
        marketplace_cache_ttl=7,
        marketplace_max_bytes=4096,
        catalog_fallback_timeout=42,
        collab_ticket_ttl=60,
    )
    services = build_services(s, AuthBackend())
    assert services.marketplace is not None
    assert (services.marketplace.timeout, services.marketplace.cache_ttl) == (3, 7)
    assert services.marketplace.max_bytes == 4096
    assert services.catalog.fallback_timeout == 42
    c = TestClient(create_app(services))
    ticket = c.post("/api/collab/my_dag/ticket")
    if ticket.status_code == 200:  # collab extra installed
        assert ticket.json()["expires_in"] == 60


def test_reset_deploy_target_restores_configured_storage(tmp_path):
    custom = tmp_path / "custom"
    s = Settings(
        dags_folder=tmp_path,
        subfolder="",
        ai={"enabled": False},
        storage={"mode": "local", "local_path": str(custom)},  # type: ignore[arg-type]
        iam={"anonymous_roles": ["Admin"]},  # type: ignore[arg-type]
    )
    services = build_services(s, AuthBackend())
    assert services.configured_storage is not None
    services.apply_storage(s.storage.model_copy(update={"local_path": None}), source="ui")
    c = TestClient(create_app(services))
    info = c.delete("/api/deploy-target").json()
    assert info["source"] == "settings"
    assert info["settings"]["local_path"] == str(custom)


def test_wildcard_cors_never_allows_credentials(tmp_path):
    c = client(tmp_path, cors_origins=["*"])
    res = c.get("/api/health", headers={"Origin": "https://evil.example", "Cookie": "_token=x"})
    assert res.headers.get("access-control-allow-credentials") != "true"
    pinned = client(tmp_path, cors_origins=["https://ui.example"])
    ok = pinned.get("/api/health", headers={"Origin": "https://ui.example"})
    assert ok.headers["access-control-allow-origin"] == "https://ui.example"
    assert ok.headers["access-control-allow-credentials"] == "true"
