"""features.iam: AutoAir's own authorization can be switched off (pre-IAM behaviour)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastapi import Depends
from fastapi.testclient import TestClient

from autoair.api.app import build_services, create_app
from autoair.auth import AuthBackend, TokenVerifier, bearer_passthrough
from autoair.iam import model as P
from autoair.iam.config import IamSettings
from autoair.iam.deps import direct_push_roles, iam_enabled, iam_settings, iam_without_roles
from autoair.settings import Settings
from autoair.storage.config import GitStorageSettings, StorageSettings
from tests.integration.test_iam_api import (
    AIRFLOW,
    SIMPLE,
    airflow_ok,
    as_role,
    can_edit,
    git_repo,  # noqa: F401 - fixture
    is_admin,
)

pytestmark = pytest.mark.airflow

STRICT_IAM: dict[str, Any] = {
    "strategies": ["role_mapping"],
    "require_approval": True,
    "roles": {"Auditor": ["spec:read"]},
    "default_roles": ["Viewer"],
    "force_pr_without_direct_push": True,
}


def services(tmp_path: Path, *, iam_on: bool, **settings: Any) -> Any:
    s = Settings(  # type: ignore[call-arg]
        dags_folder=tmp_path / "dags",
        subfolder="",
        ai={"enabled": False},
        airflow_url=AIRFLOW,
        import_check_on_deploy=False,
        iam=IamSettings(**STRICT_IAM),
        features={"iam": iam_on},
        **settings,
    )
    verifier = TokenVerifier(AIRFLOW, transport=httpx2.MockTransport(airflow_ok))
    return build_services(s, bearer_passthrough(verifier))


def client(svc: Any) -> TestClient:
    return TestClient(create_app(svc), raise_server_exceptions=False)


def test_iam_off_settings_drop_iam_rules() -> None:
    configured = IamSettings(**STRICT_IAM, login_enabled=False, anonymous_roles=["Viewer"])
    off = iam_without_roles(configured)
    assert off.strategies == ["airflow_permissions"]
    assert off.role_mapping == {}
    assert off.roles == {}
    assert off.default_roles == []
    assert off.require_approval is False
    assert off.force_pr_without_direct_push is False
    assert off.direct_push_roles == []
    # sign-in and anonymous roles keep their configured values
    assert off.login_enabled is False
    assert off.anonymous_roles == ["Viewer"]


def test_flag_is_on_by_default(tmp_path: Path) -> None:
    svc = services(tmp_path, iam_on=True)
    assert iam_enabled(svc)
    assert iam_settings(svc).require_approval is True
    me = client(svc).get("/api/me", headers=as_role("Viewer")).json()
    assert me["iam_enabled"] is True
    assert me["strategies"] == ["role_mapping"]


def test_iam_off_standalone_keeps_pre_iam_rights(tmp_path: Path) -> None:
    """Standalone cannot check Airflow's DAG permissions: as before IAM, every signed-in
    user may use everything (Airflow enforces its RBAC on proxied calls); the token's role
    claim, custom roles and approvals are ignored."""
    on = client(services(tmp_path / "on", iam_on=True))
    assert (
        on.put("/api/specs/simple", json={"spec": SIMPLE}, headers=as_role("Viewer")).status_code
        == 403
    )

    off_svc = services(tmp_path / "off", iam_on=False)
    off = client(off_svc)
    me = off.get("/api/me", headers=as_role("Viewer")).json()
    assert me["iam_enabled"] is False
    assert me["strategies"] == ["airflow_permissions"]
    assert "Authenticated" in me["roles"]
    assert "Auditor" not in me["roles"]
    assert P.DEPLOY in me["permissions"]
    assert P.DEPLOY_DIRECT_PUSH not in me["permissions"]
    assert me["deploy"]["require_approval"] is False
    # no proposal (202): the deploy goes live directly
    resp = off.put("/api/specs/simple", json={"spec": SIMPLE}, headers=as_role("Viewer"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "deployed"


def test_iam_on_requires_approval(tmp_path: Path) -> None:
    on = client(services(tmp_path, iam_on=True))
    resp = on.put("/api/specs/simple", json={"spec": SIMPLE}, headers=as_role("Admin"))
    assert resp.status_code == 202
    assert resp.json()["status"] == "pending_approval"


def test_iam_off_plugin_shaped_checks(tmp_path: Path) -> None:
    """With real Airflow permission checks (plugin mode), features.iam off follows them."""
    s = Settings(  # type: ignore[call-arg]
        dags_folder=tmp_path,
        subfolder="",
        ai={"enabled": False},
        import_check_on_deploy=False,
        iam=IamSettings(**STRICT_IAM),
        features={"iam": False},
    )
    auth = AuthBackend(write_dag=[Depends(can_edit)], admin=[Depends(is_admin)], name="test")
    c = client(build_services(s, auth))
    viewer = c.get("/api/me").json()
    assert P.SPEC_READ in viewer["permissions"]
    assert P.SPEC_WRITE not in viewer["permissions"]
    editor = c.get("/api/me", headers={"x-user": "editor"}).json()
    assert P.DEPLOY in editor["permissions"]
    assert P.ADMIN_CONFIG not in editor["permissions"]
    admin = c.get("/api/me", headers={"x-user": "admin"}).json()
    assert "Admin" in admin["roles"]
    # no direct-push roles: the target's mode decides
    assert P.DEPLOY_DIRECT_PUSH not in admin["permissions"]
    assert c.put("/api/specs/simple", json={"spec": SIMPLE}).status_code == 403
    deployed = c.put("/api/specs/simple", json={"spec": SIMPLE}, headers={"x-user": "editor"})
    assert deployed.status_code == 200, deployed.text


def _git(tmp_path: Path, repo: Path, mode: str, *, iam_on: bool) -> Any:
    storage = StorageSettings(
        mode="git",
        git=GitStorageSettings(
            provider="local",
            repo=f"file://{repo}",
            mode=mode,  # type: ignore[arg-type]
            cache_dir=tmp_path / f"cache-{mode}-{iam_on}",
            direct_push_roles=["Deployer"],
        ),
    )
    return services(tmp_path / f"{mode}-{iam_on}", iam_on=iam_on, storage=storage)


def test_iam_off_git_target_mode_decides(tmp_path: Path, git_repo: Path) -> None:  # noqa: F811
    pr = _git(tmp_path, git_repo, "pr", iam_on=False)
    assert direct_push_roles(pr) == []  # storage.git.direct_push_roles ignored
    c = client(pr)
    me = c.get("/api/me", headers=as_role("Deployer")).json()["deploy"]
    assert me["direct_push"] is False
    assert me["default_opens_pr"] is True
    denied = c.put(
        "/api/specs/simple", json={"spec": SIMPLE, "direct_push": True}, headers=as_role("Admin")
    )
    assert denied.status_code == 403
    opened = c.put("/api/specs/simple", json={"spec": SIMPLE}, headers=as_role("Viewer"))
    assert opened.status_code == 200, opened.text
    assert opened.json()["branch"] == "autoair/simple"

    # push mode: force_pr_without_direct_push is ignored, everyone pushes
    push = client(_git(tmp_path, git_repo, "push", iam_on=False))
    pushed = push.put("/api/specs/simple", json={"spec": SIMPLE}, headers=as_role("Viewer"))
    assert pushed.status_code == 200, pushed.text
    assert pushed.json()["branch"] == "main"

    # with IAM on the configured direct-push role applies
    on = client(_git(tmp_path, git_repo, "pr", iam_on=True))
    assert on.get("/api/me", headers=as_role("Deployer")).json()["deploy"]["direct_push"] is True


# --------------------------------------------------------------------------- MCP
MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
    "mcp-protocol-version": "2025-06-18",
}


def _mcp_save(c: TestClient, role: str) -> dict[str, Any]:
    resp = c.post(
        "/mcp/",
        headers={**MCP_HEADERS, **as_role(role)},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "save_dag_spec", "arguments": {"spec": SIMPLE, "confirm": True}},
        },
    )
    assert resp.status_code == 200, resp.text
    result: dict[str, Any] = resp.json()["result"]
    return result


def test_mcp_follows_the_iam_switch(tmp_path: Path) -> None:
    pytest.importorskip("fastmcp")
    on = client(services(tmp_path / "on", iam_on=True))
    viewer = _mcp_save(on, "Viewer")
    assert viewer["isError"]
    assert "spec:write" in viewer["content"][0]["text"]
    admin = _mcp_save(on, "Admin")  # four-eyes rule: agents cannot deploy live
    assert admin["isError"]
    assert "approved" in admin["content"][0]["text"]

    off = client(services(tmp_path / "off", iam_on=False))
    saved = _mcp_save(off, "Viewer")  # features.iam off: every signed-in user, no approvals
    assert not saved["isError"], saved
    assert list((tmp_path / "off").rglob("simple*"))
