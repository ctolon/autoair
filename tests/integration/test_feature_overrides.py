"""Runtime feature flags (admin panel): overrides shared by replicas, locks, audit, /api/meta
and request-time gating of routes mounted at startup (collab, AI, deploy, MCP)."""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from autoair.api.app import Services, build_services, create_app
from autoair.auth import TokenVerifier, bearer_passthrough
from autoair.feature_overrides import FeatureOverrideStore
from autoair.iam.config import IamSettings
from autoair.persistence import FileDocumentStore
from autoair.settings import Settings
from tests.db_backends import BACKENDS, database_settings, drop_tables
from tests.optional import requires_mcp

pytestmark = pytest.mark.airflow

AIRFLOW = "http://airflow.test"


def jwt(**claims: Any) -> str:
    def enc(obj: Any) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{enc({'alg': 'HS512', 'typ': 'JWT'})}.{enc(claims)}.signature"


def as_role(role: str, user: str | None = None) -> dict[str, str]:
    token = jwt(sub=user or role.lower(), role=f"AutoAir {role}", exp=time.time() + 3600)
    return {"Authorization": f"Bearer {token}"}


ADMIN = as_role("Admin", "root")


def make_settings(root: Path, database: dict[str, Any] | None = None, **extra: Any) -> Settings:
    values: dict[str, Any] = {
        "dags_folder": root / "dags",
        "subfolder": "",
        "ai": {"enabled": False},
        "airflow_url": AIRFLOW,
        "import_check_on_deploy": False,
        "iam": IamSettings(strategies=["role_mapping"]),
        "database": database or {"backend": "file"},
        "features_override_ttl": 0,
        **extra,
    }
    return Settings(**values)  # type: ignore[arg-type]


def replica(settings: Settings) -> tuple[TestClient, Services]:
    def airflow(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/api/v2/dags":  # token verification
            return httpx2.Response(200, json={"dags": [], "total_entries": 0})
        return httpx2.Response(404, json={"detail": "not found"})

    verifier = TokenVerifier(AIRFLOW, transport=httpx2.MockTransport(airflow))
    services = build_services(settings, bearer_passthrough(verifier))
    return TestClient(create_app(services), raise_server_exceptions=False), services


def flag(view: dict[str, Any], name: str) -> dict[str, Any]:
    return next(f for f in view["flags"] if f["name"] == name)


def switch(client: TestClient, name: str, value: bool | None, **headers: str) -> Any:
    return client.put(
        f"/api/admin/features/{name}",
        json={"value": value, "reason": "maintenance window"},
        headers=headers or ADMIN,
    )


@pytest.fixture
def app(tmp_path: Path) -> Any:
    client, services = replica(make_settings(tmp_path))
    return client, services


# --------------------------------------------------------------------------- replicas


@pytest.mark.parametrize("backend", BACKENDS)
def test_replicas_share_overrides(backend: Any, tmp_path: Path) -> None:
    database = database_settings(backend, tmp_path)
    # Two independent Settings objects (two processes) on one state folder / database file.
    a, sa = replica(make_settings(tmp_path, database))
    b, sb = replica(make_settings(tmp_path, database))
    try:
        assert sa.settings.feature_overrides is not sb.settings.feature_overrides
        assert b.get("/api/meta", headers=ADMIN).json()["features"]["templates"] is True
        resp = switch(a, "templates", False)
        assert resp.status_code == 200, resp.text
        assert flag(resp.json(), "templates")["effective"] is False
        assert b.get("/api/meta", headers=ADMIN).json()["features"]["templates"] is False
        assert b.get("/api/templates", headers=ADMIN).status_code == 404
        # Removing the override restores the configured value everywhere.
        assert switch(b, "templates", None).status_code == 200
        assert a.get("/api/meta", headers=ADMIN).json()["features"]["templates"] is True
        assert a.get("/api/templates", headers=ADMIN).status_code == 200
    finally:
        drop_tables(sa.settings)


def test_sqlite_replica_picks_up_a_change_after_the_ttl(tmp_path: Path) -> None:
    pytest.importorskip("sqlalchemy")
    database = database_settings("sqlite", tmp_path)
    a, _sa = replica(make_settings(tmp_path, database))
    _b, sb = replica(make_settings(tmp_path, database, features_override_ttl=60))
    try:
        assert sb.settings.feature("runs") is True  # cached for 60 s from now on
        assert switch(a, "runs", False).status_code == 200
        assert sb.settings.feature("runs") is True  # still the cached copy
        store = sb.settings.feature_overrides
        store._next_read = 0.0  # the TTL elapsed
        assert sb.settings.feature("runs") is False
    finally:
        drop_tables(sb.settings)


def test_store_keeps_last_known_overrides_on_backend_errors(tmp_path: Path) -> None:
    store = FeatureOverrideStore(FileDocumentStore(str(tmp_path)), ttl=0)
    store.set("ai", False, user="root", reason="x")
    assert store.values() == {"ai": False}

    def broken(key: str) -> Any:
        raise RuntimeError("database down")

    store.documents.get = broken  # type: ignore[method-assign]
    assert store.values() == {"ai": False}


# --------------------------------------------------------------------------- locks


def test_features_locked_refuses_overrides(tmp_path: Path) -> None:
    client, services = replica(make_settings(tmp_path, features_locked=["ai", "dep*"]))
    view = client.get("/api/admin/features", headers=ADMIN).json()
    assert flag(view, "ai")["locked"] == "features_locked"
    assert flag(view, "deploy")["locked"] == "features_locked"
    assert flag(view, "templates")["locked"] is None
    resp = switch(client, "deploy", False)
    assert resp.status_code == 409
    assert "locked" in resp.json()["detail"]
    # An override written directly to the store (e.g. before the lock) is ignored.
    services.settings.feature_overrides.set("deploy", False, user="x", reason="y")
    assert services.settings.feature("deploy") is True


def test_flags_set_in_the_environment_are_locked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTOAIR_FEATURES__MARKETPLACE", "false")
    client, _ = replica(make_settings(tmp_path))
    view = client.get("/api/admin/features", headers=ADMIN).json()
    assert flag(view, "marketplace") | {"locked": "env", "effective": False} == flag(
        view, "marketplace"
    )
    resp = switch(client, "marketplace", True)
    assert resp.status_code == 409
    assert "AUTOAIR_FEATURES__MARKETPLACE" in resp.json()["detail"]


def test_validation_permissions_and_audit(app: Any) -> None:
    client, services = app
    assert switch(client, "nope", False).status_code == 404
    no_reason = client.put(
        "/api/admin/features/templates", json={"value": False, "reason": ""}, headers=ADMIN
    )
    assert no_reason.status_code == 422
    # admin:panel may look, only admin:config may change.
    viewer = as_role("Viewer")
    assert client.get("/api/admin/features", headers=viewer).status_code == 403
    assert switch(client, "templates", False, **viewer).status_code == 403
    # Never both editors off.
    assert switch(client, "visual_editor", False).status_code == 200
    resp = switch(client, "code_editor", False)
    assert resp.status_code == 422
    assert "visual_editor" in resp.json()["detail"]

    view = switch(client, "templates", False).json()
    entry = flag(view, "templates")
    assert entry["override"] is False
    assert entry["configured"] is True
    assert entry["updated_by"] == "root"
    assert entry["reason"] == "maintenance window"
    records = services.audit.query(action="feature_override", limit=10)
    latest = records[0]
    assert latest.user == "root"
    assert (
        latest.details
        | {
            "feature": "templates",
            "old": True,
            "new": False,
            "reason": "maintenance window",
        }
        == latest.details
    )


def test_constraints_still_apply_after_overrides(tmp_path: Path) -> None:
    client, _ = replica(make_settings(tmp_path, allow_python_code=False))
    view = switch(client, "python_code", True).json()
    entry = flag(view, "python_code")
    assert entry["effective"] is False
    assert entry["constrained"] is True


# --------------------------------------------------------------------------- request-time gating


def test_deploy_routes_are_gated_at_request_time(app: Any, simple_spec_data: Any) -> None:
    client, _ = app
    body = {"spec": simple_spec_data}
    assert switch(client, "deploy", False).status_code == 200
    resp = client.put("/api/specs/simple", json=body, headers=ADMIN)
    assert resp.status_code == 404
    assert "deploy feature is disabled" in resp.json()["detail"]
    assert switch(client, "deploy", True).status_code == 200
    resp = client.put("/api/specs/simple", json=body, headers=ADMIN)
    assert resp.status_code == 200, resp.text


def test_ai_routes_are_gated_at_request_time(tmp_path: Path) -> None:
    # Off in the configuration (not locked): mounted, and switchable at runtime.
    client, _ = replica(make_settings(tmp_path, features={"ai": False}))
    status = client.get("/api/ai/status", headers=ADMIN)
    assert status.status_code == 200
    assert status.json()["enabled"] is False
    resp = client.post("/api/ai/describe", json={}, headers=ADMIN)
    assert resp.status_code == 404
    assert "ai feature is disabled" in resp.json()["detail"]
    assert client.get("/api/meta", headers=ADMIN).json()["features"]["ai"] is False

    assert switch(client, "ai", True).status_code == 200
    assert client.get("/api/meta", headers=ADMIN).json()["features"]["ai"] is True
    assert client.get("/api/ai/status", headers=ADMIN).json()["enabled"] is True
    resp = client.post("/api/ai/describe", json={}, headers=ADMIN)
    assert resp.status_code != 404 or "disabled" not in resp.text


def test_ai_locked_off_mounts_only_the_disabled_status(tmp_path: Path) -> None:
    client, _ = replica(make_settings(tmp_path, features={"ai": False}, features_locked=["ai"]))
    assert client.get("/api/ai/status", headers=ADMIN).json()["enabled"] is False
    assert switch(client, "ai", True).status_code == 409


def test_collab_is_gated_at_request_time(app: Any) -> None:
    from autoair import collab

    if not collab.available():
        pytest.skip("collab extra not installed")
    client, _ = app
    assert client.post("/api/collab/demo/ticket", headers=ADMIN).status_code == 200
    assert switch(client, "collab", False).status_code == 200
    resp = client.post("/api/collab/demo/ticket", headers=ADMIN)
    assert resp.status_code == 404
    # The WebSocket app mounted at startup refuses connections while the flag is off.
    off = client.get("/collab/demo")
    assert off.status_code == 404
    assert "collab feature is disabled" in off.json()["detail"]
    with pytest.raises(WebSocketDisconnect) as exc, client.websocket_connect("/collab/demo"):
        pass
    assert exc.value.code == 1008
    assert client.get("/api/meta", headers=ADMIN).json()["collab_enabled"] is False
    assert switch(client, "collab", True).status_code == 200
    assert client.post("/api/collab/demo/ticket", headers=ADMIN).status_code == 200
    assert "collab feature is disabled" not in client.get("/collab/demo").text


@requires_mcp
def test_mcp_tools_follow_the_runtime_flag(app: Any) -> None:
    pytest.importorskip("mcp")
    client, services = app
    from autoair.mcp.server import create_mcp_server

    create_mcp_server(services)  # the tool gate reads services().settings.feature(...)
    assert switch(client, "mcp", False).status_code == 200
    assert client.get("/api/meta", headers=ADMIN).json()["mcp_enabled"] is False
    resp = client.post("/mcp/", json={}, headers=ADMIN)
    assert resp.status_code == 404
    assert "mcp feature is disabled" in resp.json()["detail"]
    assert switch(client, "mcp", None).status_code == 200
    assert "mcp feature is disabled" not in client.post("/mcp/", json={}, headers=ADMIN).text


# --------------------------------------------------------------------------- admin limits


def test_admin_limits_are_bounded() -> None:
    from autoair.sections import ADMIN_EXPORT_MAX, AdminSettings

    assert AdminSettings().export_limit == 50_000
    with pytest.raises(ValueError, match="less than or equal"):
        AdminSettings(export_limit=ADMIN_EXPORT_MAX + 1)
    with pytest.raises(ValueError, match="greater than or equal"):
        AdminSettings(scan_limit=0)


def test_audit_export_honours_the_export_limit(tmp_path: Path) -> None:
    client, services = replica(make_settings(tmp_path, admin={"export_limit": 5}))
    for i in range(8):
        services.audit.record("probe", user=f"u{i}")
    over = client.get("/api/admin/audit.csv", params={"limit": 6}, headers=ADMIN)
    assert over.status_code == 422
    assert "admin.export_limit" in over.json()["detail"]
    default = client.get("/api/admin/audit.csv", params={"action": "probe"}, headers=ADMIN)
    assert default.status_code == 200
    assert len(default.text.strip().splitlines()) == 1 + 5  # header + capped default
