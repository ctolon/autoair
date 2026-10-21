"""IAM end to end: every router x role, direct push vs PR, approvals, /me, sign-in."""

from __future__ import annotations

import base64
import json
import logging
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastapi import Depends, Request
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from autoair.api.app import Services, build_services, create_app
from autoair.auth import AuthBackend, TokenVerifier, bearer_passthrough
from autoair.iam import model as P
from autoair.iam.config import IamSettings
from autoair.iam.login import LoginService
from autoair.settings import Settings
from autoair.storage.config import GitStorageSettings, StorageSettings

pytestmark = pytest.mark.airflow

AIRFLOW = "http://airflow.test"
ROLES = list(P.BUILTIN_ROLES)

SIMPLE: dict[str, Any] = {
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


def jwt(**claims: Any) -> str:
    def enc(obj: Any) -> str:
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")

    return f"{enc({'alg': 'HS512', 'typ': 'JWT'})}.{enc(claims)}.signature"


def as_role(role: str, user: str | None = None) -> dict[str, str]:
    """Bearer token of a user whose Airflow role is ``AutoAir <role>`` (role_prefix)."""
    token = jwt(sub=user or role.lower(), role=f"AutoAir {role}", exp=time.time() + 3600)
    return {"Authorization": f"Bearer {token}"}


def airflow_ok(request: httpx2.Request) -> httpx2.Response:
    """A permissive Airflow: accepts every token, answers the REST calls AutoAir makes."""
    if request.url.path == "/api/v2/dags":
        return httpx2.Response(200, json={"dags": [], "total_entries": 0})
    return httpx2.Response(404, json={"detail": "not found"})


def make_services(
    tmp_path: Path, *, iam: dict[str, Any] | None = None, **settings: Any
) -> Services:
    s = Settings(  # type: ignore[call-arg]
        dags_folder=tmp_path / "dags",
        subfolder="",
        ai={"enabled": False},
        airflow_url=AIRFLOW,
        iam=IamSettings(**{"strategies": ["role_mapping"], **(iam or {})}),
        **settings,
    )
    verifier = TokenVerifier(AIRFLOW, transport=httpx2.MockTransport(airflow_ok))
    return build_services(s, bearer_passthrough(verifier))


def make_client(services: Services) -> TestClient:
    return TestClient(create_app(services), raise_server_exceptions=False)


# --------------------------------------------------------------------------- route matrix
# (method, path, body, permissions required); body {} is invalid on purpose: an allowed
# caller gets a 4xx/5xx from validation or the route, never 403.
MATRIX: list[tuple[str, str, Any, tuple[str, ...]]] = [
    ("GET", "/api/meta", None, ()),
    ("GET", "/api/me", None, ()),
    ("GET", "/api/catalog/node-kinds", None, (P.BUILDER_USE,)),
    ("POST", "/api/catalog/refresh", None, (P.CATALOG_REFRESH,)),
    ("GET", "/api/airflow/pools", None, (P.AIRFLOW_READ,)),
    ("POST", "/api/airflow/connections", {}, (P.AIRFLOW_READ, P.CONNECTION_WRITE)),
    ("POST", "/api/airflow/connections/test", {}, (P.AIRFLOW_READ, P.CONNECTION_WRITE)),
    ("GET", "/api/airflow/connections/hook-meta", None, (P.AIRFLOW_READ, P.CONNECTION_WRITE)),
    ("POST", "/api/airflow/assets/1/events", {}, (P.AIRFLOW_READ, P.RUN_TRIGGER)),
    ("POST", "/api/airflow/dags/x/trigger", [], (P.AIRFLOW_READ, P.RUN_TRIGGER)),
    ("PUT", "/api/airflow/dags/x/paused", [], (P.AIRFLOW_READ, P.RUN_MANAGE)),
    ("POST", "/api/airflow/dags/x/runs/r/clear", [], (P.AIRFLOW_READ, P.RUN_MANAGE)),
    ("POST", "/api/airflow/dags/x/backfill", [], (P.AIRFLOW_READ, P.RUN_MANAGE)),
    ("POST", "/api/ai/generate", {}, (P.AI_USE,)),
    ("POST", "/api/lsp/shell/lint", {}, (P.BUILDER_USE,)),
    ("POST", "/api/lsp/validate", {}, (P.BUILDER_USE,)),
    ("POST", "/api/codegen", {}, (P.BUILDER_USE,)),
    ("POST", "/api/validate", {}, (P.BUILDER_USE,)),
    ("POST", "/api/review/rules", {}, (P.BUILDER_USE,)),
    ("POST", "/api/lint/secrets", {}, (P.BUILDER_USE,)),
    ("POST", "/api/interchange/yaml/import", {}, (P.BUILDER_USE,)),
    ("POST", "/api/interchange/python/import", {}, (P.BUILDER_USE,)),
    ("POST", "/api/interchange/python/sync", {}, (P.BUILDER_USE,)),
    ("POST", "/api/interchange/airflow/x/import", None, (P.AIRFLOW_READ, P.CODE_EXECUTE)),
    ("GET", "/api/templates", None, (P.BUILDER_USE,)),
    ("POST", "/api/templates", {}, (P.TEMPLATE_WRITE,)),
    ("DELETE", "/api/templates/nope", None, (P.TEMPLATE_WRITE,)),
    ("GET", "/api/icons", None, (P.BUILDER_USE,)),
    ("PUT", "/api/icons/assignments", {}, (P.ICON_WRITE,)),
    ("DELETE", "/api/icons/nope", None, (P.ICON_WRITE,)),
    ("GET", "/api/specs", None, (P.SPEC_READ,)),
    ("GET", "/api/specs/x", None, (P.SPEC_READ,)),
    ("GET", "/api/specs/x/audit", None, (P.AUDIT_READ,)),
    ("GET", "/api/specs/x/reviews", None, (P.SPEC_READ,)),
    ("POST", "/api/specs/x/reviews", {}, (P.REVIEW,)),
    ("PUT", "/api/specs/x", {}, (P.SPEC_WRITE,)),
    ("DELETE", "/api/specs/x", None, (P.SPEC_DELETE,)),
    ("GET", "/api/vcs/status", None, (P.SPEC_READ,)),
    ("POST", "/api/vcs/diff", {}, (P.SPEC_READ,)),
    ("POST", "/api/vcs/x/promote", {}, (P.PROMOTE,)),
    ("POST", "/api/runner/test-task", {}, (P.RUN_TEST,)),
    ("POST", "/api/collab/x/ticket", None, (P.SPEC_WRITE,)),
    ("GET", "/api/deploy-target", None, (P.BUILDER_USE,)),
    ("PUT", "/api/deploy-target", {}, (P.ADMIN_DEPLOY_TARGET,)),
    ("POST", "/api/deploy-target/test", {}, (P.ADMIN_DEPLOY_TARGET,)),
    ("DELETE", "/api/deploy-target", None, (P.ADMIN_DEPLOY_TARGET,)),
    ("GET", "/api/marketplace", None, (P.BUILDER_USE,)),
    ("POST", "/api/marketplace/install", {}, (P.TEMPLATE_WRITE,)),
]


@pytest.fixture(scope="module")
def matrix_client(tmp_path_factory: pytest.TempPathFactory) -> TestClient:
    services = make_services(tmp_path_factory.mktemp("matrix"))
    services.catalog.refresh = lambda: services.catalog.stats()  # type: ignore[method-assign]
    return make_client(services)


@pytest.mark.parametrize("role", ROLES)
def test_route_permission_matrix(matrix_client: TestClient, role: str) -> None:
    granted = set(P.BUILTIN_ROLES[role])
    wrong: list[str] = []
    for method, path, body, needed in MATRIX:
        kwargs: dict[str, Any] = {"headers": as_role(role)}
        if body is not None:
            kwargs["json"] = body
        status = matrix_client.request(method, path, **kwargs).status_code
        if path.startswith("/api/ai/") and status == 404:
            continue  # AI not mounted (features.ai off); its guard is checked below
        allowed = set(needed) <= granted
        if allowed == (status == 403):
            wrong.append(f"{method} {path}: {status} (allowed={allowed})")
    assert not wrong, f"{role}: " + "; ".join(wrong)


def test_every_protected_route_requires_a_permission(matrix_client: TestClient) -> None:
    """Guards new routes: each one depends on ``require(...)`` (or is listed here)."""
    open_routes = {
        "GET /meta",
        "GET /me",
        "POST /auth/login",
        "POST /auth/logout",
        # sign-in with the Airflow UI session (autoair.iam.sso): they establish the session
        "GET /auth/config",
        "GET /auth/session",
        "POST /auth/refresh",
        "GET /auth/sso/login",
        "GET /health",
        "GET /ready",
        "GET /metrics",
        "GET /icons/{icon_id}",  # icon files (public_router)
    }
    api = next(r.app for r in matrix_client.app.routes if getattr(r, "path", "") == "/api")

    def names(dependant: Any) -> set[str]:
        out = set()
        for dep in dependant.dependencies:
            out.add(getattr(dep.call, "__name__", ""))
            out |= names(dep)
        return out

    missing = [
        f"{method} {route.path}"
        for route in api.routes
        if isinstance(route, APIRoute)
        for method in sorted(route.methods)
        if f"{method} {route.path}" not in open_routes
        and not any(n.startswith("require_") for n in names(route.dependant))
    ]
    assert not missing


def test_unauthenticated_and_denials_are_audited(tmp_path: Path) -> None:
    services = make_services(tmp_path)
    c = make_client(services)
    assert c.get("/api/specs").status_code == 401
    denied = c.get("/api/specs/x/audit", headers=as_role("Authenticated", "eve"))
    assert denied.status_code == 403
    assert "audit:read" in denied.json()["detail"]
    records = services.audit.tail(10)
    assert records[0].action == "permission_denied"
    assert records[0].user == "eve"
    assert records[0].dag_id == "x"
    assert records[0].details["permission"] == "audit:read"


def test_denials_audit_can_be_switched_off(tmp_path: Path) -> None:
    services = make_services(tmp_path, iam={"audit_denials": False})
    c = make_client(services)
    assert c.get("/api/specs", headers=as_role("Authenticated")).status_code == 403
    assert services.audit.tail(10) == []


def test_untrusted_claims_are_ignored_without_verification(tmp_path: Path) -> None:
    """``bearer_passthrough(None)``: tokens are not verified, so role claims grant nothing."""
    s = Settings(  # type: ignore[call-arg]
        dags_folder=tmp_path,
        subfolder="",
        ai={"enabled": False},
        iam=IamSettings(strategies=["role_mapping"]),
    )
    c = make_client(build_services(s, bearer_passthrough(None)))
    me = c.get("/api/me", headers=as_role("Admin"))
    assert me.status_code == 200
    assert me.json()["permissions"] == []
    assert c.put("/api/deploy-target", json={}, headers=as_role("Admin")).status_code == 403


def test_expired_token_is_rejected_before_asking_airflow(tmp_path: Path) -> None:
    c = make_client(make_services(tmp_path))
    token = jwt(sub="old", role="AutoAir Admin", exp=time.time() - 5)
    resp = c.get("/api/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
    assert "expired" in resp.json()["detail"]


# --------------------------------------------------------------------------- default behaviour
def test_default_strategy_keeps_pre_iam_behaviour(tmp_path: Path) -> None:
    """No IAM config: standalone users may do everything they could before, except the
    new direct push (deny by default)."""
    s = Settings(dags_folder=tmp_path, subfolder="", ai={"enabled": False}, airflow_url=AIRFLOW)  # type: ignore[call-arg]
    verifier = TokenVerifier(AIRFLOW, transport=httpx2.MockTransport(airflow_ok))
    c = make_client(build_services(s, bearer_passthrough(verifier)))
    headers = {"Authorization": f"Bearer {jwt(sub='bob')}"}
    me = c.get("/api/me", headers=headers).json()
    assert me["user"] == "bob"
    assert me["auth"] == "bearer"
    assert me["strategies"] == ["airflow_permissions"]
    assert set(me["permissions"]) == P.ALL_PERMISSIONS - {P.DEPLOY_DIRECT_PUSH}
    assert me["deploy"]["direct_push"] is False
    deployed = c.put("/api/specs/simple", json={"spec": SIMPLE}, headers=headers)
    assert deployed.status_code == 200, deployed.text
    assert deployed.json()["deployed"] is True


def current_user(request: Request) -> str:
    return request.headers.get("x-user", "viewer")


def can_edit(request: Request, user: str = Depends(current_user)) -> None:
    from fastapi import HTTPException

    if user not in ("editor", "admin"):
        raise HTTPException(status_code=403, detail="Forbidden")


def is_admin(request: Request, user: str = Depends(current_user)) -> None:
    from fastapi import HTTPException

    if user != "admin":
        raise HTTPException(status_code=403, detail="Forbidden")


def test_airflow_permissions_strategy_with_real_checks(tmp_path: Path) -> None:
    """Shaped like plugin mode: DAG edit / configuration edit decide the roles, and only a
    verified configuration edit (Admin) unlocks direct push."""
    s = Settings(dags_folder=tmp_path, subfolder="", ai={"enabled": False})  # type: ignore[call-arg]
    auth = AuthBackend(write_dag=[Depends(can_edit)], admin=[Depends(is_admin)], name="test")
    c = make_client(build_services(s, auth))
    viewer = c.get("/api/me").json()
    assert P.SPEC_READ in viewer["permissions"]
    assert P.SPEC_WRITE not in viewer["permissions"]
    editor = c.get("/api/me", headers={"x-user": "editor"}).json()
    assert P.DEPLOY in editor["permissions"]
    assert P.ADMIN_DEPLOY_TARGET not in editor["permissions"]
    admin = c.get("/api/me", headers={"x-user": "admin"}).json()
    assert P.DEPLOY_DIRECT_PUSH in admin["permissions"]
    assert "Admin" in admin["roles"]
    assert c.put("/api/specs/simple", json={"spec": SIMPLE}).status_code == 403
    assert c.delete("/api/deploy-target", headers={"x-user": "editor"}).status_code == 403
    assert c.delete("/api/deploy-target", headers={"x-user": "admin"}).status_code == 200


class FabLikeUser:
    def __init__(self, name: str, roles: list[str]) -> None:
        self.name = name
        self.roles = [type("Role", (), {"name": r})() for r in roles]

    def get_name(self) -> str:
        return self.name


USERS = {
    "ann": FabLikeUser("ann", ["Op"]),
    "vic": FabLikeUser("vic", ["Viewer"]),
    "cv": FabLikeUser("cv", ["Public"]),
}


def fake_get_user(request: Request) -> FabLikeUser:
    from fastapi import HTTPException

    user = USERS.get(request.headers.get("x-user", ""))
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def test_plugin_role_mapping_and_custom_view(tmp_path: Path) -> None:
    grants = {("cv", "POST", "AutoAir Deploy"), ("cv", "GET", "AutoAir Builder")}

    def custom_view(user: FabLikeUser) -> Any:
        return lambda method, resource: (user.name, method, resource) in grants

    auth = AuthBackend(
        app=[Depends(fake_get_user)],
        name="airflow",
        user=Depends(fake_get_user),
        custom_view=custom_view,
    )
    s = Settings(  # type: ignore[call-arg]
        mode="plugin",
        dags_folder=tmp_path,
        subfolder="",
        ai={"enabled": False},
        iam=IamSettings(strategies=["role_mapping", "custom_view"]),
    )
    c = make_client(build_services(s, auth))
    assert c.get("/api/me").status_code == 401
    ann = c.get("/api/me", headers={"x-user": "ann"}).json()
    assert ann["user"] == "ann"
    assert ann["mode"] == "plugin"
    assert {"Author", "Deployer", "Operator"} <= set(ann["roles"])
    assert P.ADMIN_CONFIG not in ann["permissions"]
    vic = c.get("/api/me", headers={"x-user": "vic"}).json()
    assert vic["roles"] == ["Viewer"]
    cv = c.get("/api/me", headers={"x-user": "cv"}).json()
    assert cv["roles"] == []
    assert cv["permissions"] == sorted([P.BUILDER_USE, P.DEPLOY])
    # no password sign-in inside Airflow (its own login page is used)
    assert c.post("/api/auth/login", json={"username": "a", "password": "b"}).status_code == 404
    meta = c.get("/api/meta", headers={"x-user": "vic"}).json()
    assert meta["iam"]["roles"] == ["Viewer"]


# --------------------------------------------------------------------------- direct push / PR
@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    if shutil.which("git") is None:
        pytest.skip("git not installed")
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(bare)], check=True)
    seed = tmp_path / "seed"
    git = ["git", "-C", str(seed), "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run(["git", "clone", "-q", str(bare), str(seed)], check=True)
    (seed / "README.md").write_text("dags\n")
    subprocess.run([*git, "add", "README.md"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "init"], check=True)
    subprocess.run([*git, "push", "-q", "origin", "HEAD:main"], check=True)
    return bare


def git_client(tmp_path: Path, repo: Path, mode: str, **iam: Any) -> TestClient:
    storage = StorageSettings(
        mode="git",
        git=GitStorageSettings(
            provider="local", repo=f"file://{repo}", mode=mode, cache_dir=tmp_path / "cache"
        ),  # type: ignore[arg-type]
    )
    return make_client(make_services(tmp_path, iam=iam, storage=storage))


def deploy(c: TestClient, role: str, **body: Any) -> httpx2.Response:
    return c.put("/api/specs/simple", json={"spec": SIMPLE, **body}, headers=as_role(role))


def test_pr_mode_direct_push_needs_permission(tmp_path: Path, git_repo: Path) -> None:
    c = git_client(tmp_path, git_repo, "pr")
    me = c.get("/api/me", headers=as_role("Admin")).json()["deploy"]
    assert me == {
        "git_mode": "pr",
        "can_save": True,
        "can_deploy_live": True,
        "direct_push": True,
        "default_opens_pr": True,
        "require_approval": False,
        "git_token": "optional",
    }
    assert c.get("/api/me", headers=as_role("Deployer")).json()["deploy"]["direct_push"] is False

    # an author proposes: a PR branch, no deploy permission needed
    proposed = deploy(c, "Author")
    assert proposed.status_code == 200, proposed.text
    assert proposed.json()["branch"] == "autoair/simple"
    # ...but may not escalate with the flag
    denied = deploy(c, "Author", direct_push=True)
    assert denied.status_code == 403
    assert "deploy:direct_push" in denied.json()["detail"]
    assert deploy(c, "Deployer", direct_push=True).status_code == 403
    # a viewer cannot save at all
    assert deploy(c, "Viewer").status_code == 403

    # the admin pushes the pending version straight to main
    etag = c.get("/api/specs/simple", headers=as_role("Admin")).json()["etag"]
    pushed = deploy(c, "Admin", direct_push=True, base_etag=etag)
    assert pushed.status_code == 200, pushed.text
    assert pushed.json()["branch"] == "main"
    assert pushed.json()["pr_url"] is None
    audit = c.get("/api/specs/simple/audit", headers=as_role("Admin")).json()
    assert audit[0]["details"]["direct_push"] is True


def test_storage_direct_push_roles_override_iam(tmp_path: Path, git_repo: Path) -> None:
    storage = StorageSettings(
        mode="git",
        git=GitStorageSettings(
            provider="local",
            repo=f"file://{git_repo}",
            mode="pr",
            cache_dir=tmp_path / "cache",
            direct_push_roles=["Deployer"],
        ),
    )
    c = make_client(make_services(tmp_path, storage=storage))
    assert deploy(c, "Deployer", direct_push=True).json()["branch"] == "main"
    assert deploy(c, "Admin", direct_push=True).status_code == 403


def test_push_mode_can_force_prs(tmp_path: Path, git_repo: Path) -> None:
    c = git_client(tmp_path, git_repo, "push", force_pr_without_direct_push=True)
    assert c.get("/api/me", headers=as_role("Deployer")).json()["deploy"]["default_opens_pr"]
    forced = deploy(c, "Deployer")
    assert forced.status_code == 200, forced.text
    assert forced.json()["branch"] == "autoair/simple"
    # authors only ever open PRs, even when the PR is forced on them
    assert deploy(c, "Author").json()["branch"] == "autoair/simple"
    admin = deploy(c, "Admin")
    assert admin.json()["branch"] == "main"
    # anybody may ask for a PR instead of a push
    assert deploy(c, "Admin", direct_push=False).json()["branch"] == "autoair/simple"


def test_push_mode_default_needs_deploy(tmp_path: Path, git_repo: Path) -> None:
    c = git_client(tmp_path, git_repo, "push")
    assert deploy(c, "Author").status_code == 403  # goes live: needs 'deploy'
    assert deploy(c, "Author", direct_push=False).json()["branch"] == "autoair/simple"
    assert deploy(c, "Deployer").json()["branch"] == "main"


# --------------------------------------------------------------------------- approvals
def test_four_eyes_approval(tmp_path: Path) -> None:
    """require_approval: an unapproved live deploy becomes a proposal (202), a reviewer
    approves it, and anyone holding 'deploy' — its author included — deploys it."""
    services = make_services(tmp_path, iam={"require_approval": True})
    c = make_client(services)
    assert c.get("/api/me", headers=as_role("Deployer")).json()["deploy"]["require_approval"]
    proposed = deploy(c, "Deployer", message="first cut")
    assert proposed.status_code == 202, proposed.text
    body = proposed.json()
    assert body["status"] == "pending_approval"
    assert body["deployed"] is False
    etag = body["proposal_etag"]
    from autoair.spec import DagSpec
    from autoair.storage import spec_etag

    assert etag == spec_etag(DagSpec.model_validate(SIMPLE))
    # nothing went live
    assert c.get("/api/specs/simple", headers=as_role("Viewer")).status_code == 404
    assert not (tmp_path / "dags" / "simple.py").exists()

    listed = c.get("/api/specs/simple/proposals", headers=as_role("Viewer")).json()
    assert [(p["etag"], p["author"], p["approved"]) for p in listed] == [(etag, "deployer", False)]
    assert listed[0]["message"] == "first cut"
    # not approved yet: the proposal cannot be deployed
    url = f"/api/specs/simple/proposals/{etag}"
    assert c.post(f"{url}/deploy", headers=as_role("Deployer")).status_code == 403
    # proposing the same version again keeps the original proposal (and its author)
    again = deploy(c, "Admin", message="again")
    assert again.status_code == 202
    assert c.get(url, headers=as_role("Viewer")).json()["author"] == "deployer"

    review = {"etag": etag, "verdict": "approve", "comment": "LGTM"}
    # an author cannot approve at all; the proposal's author cannot approve their own
    assert (
        c.post("/api/specs/simple/reviews", json=review, headers=as_role("Author")).status_code
        == 403
    )
    own = c.post("/api/specs/simple/reviews", json=review, headers=as_role("Admin", "deployer"))
    assert own.status_code == 403
    assert "own" in own.json()["detail"]
    ok = c.post("/api/specs/simple/reviews", json=review, headers=as_role("Reviewer", "rita"))
    assert ok.status_code == 200
    assert ok.json()["user"] == "rita"
    reviews = c.get("/api/specs/simple/reviews", headers=as_role("Viewer")).json()
    assert [r["user"] for r in reviews] == ["rita"]

    detail = c.get(url, headers=as_role("Viewer")).json()
    assert detail["approved"] is True
    assert detail["approved_by"] == ["rita"]
    assert detail["spec"]["dag_id"] == "simple"
    assert detail["current_spec"] is None
    assert detail["stale"] is False
    # deploying needs 'deploy'
    assert c.post(f"{url}/deploy", headers=as_role("Reviewer", "rita")).status_code == 403
    # the author deploys it once someone else approved
    done = c.post(f"{url}/deploy", headers=as_role("Deployer"))
    assert done.status_code == 200, done.text
    assert done.json()["deployed"] is True
    assert done.json()["status"] == "deployed"
    assert (tmp_path / "dags" / "simple.py").exists()
    # the proposal is cleaned up
    assert c.get(url, headers=as_role("Viewer")).status_code == 404
    assert c.get("/api/specs/simple/proposals", headers=as_role("Viewer")).json() == []
    audit = c.get("/api/specs/simple/audit", headers=as_role("Admin")).json()
    assert audit[0]["action"] == "deploy"
    assert audit[0]["details"]["approved_by"] == ["rita"]
    assert audit[0]["details"]["proposed_by"] == "deployer"

    # a direct PUT of an approved version deploys; changes requested withdraw an approval
    v2 = {**SIMPLE, "description": "v2"}
    etag2 = spec_etag(DagSpec.model_validate(v2))
    stored = c.get("/api/specs/simple", headers=as_role("Viewer")).json()["etag"]

    def put(role: str, user: str | None = None) -> httpx2.Response:
        return c.put(
            "/api/specs/simple",
            json={"spec": v2, "base_etag": stored},
            headers=as_role(role, user),
        )

    assert put("Author", "alice").status_code == 202  # authors may propose (not deploy)
    approve2 = {"etag": etag2, "verdict": "approve"}
    c.post("/api/specs/simple/reviews", json=approve2, headers=as_role("Reviewer", "rita"))
    changes = {**approve2, "verdict": "request_changes"}
    c.post("/api/specs/simple/reviews", json=changes, headers=as_role("Reviewer", "rita"))
    assert put("Deployer", "bob").status_code == 202
    c.post("/api/specs/simple/reviews", json=approve2, headers=as_role("Reviewer", "rita"))
    assert put("Author", "alice").status_code == 403  # approved, but authors cannot deploy
    live = put("Deployer", "bob")
    assert live.status_code == 200, live.text
    assert live.json()["deployed"]
    assert c.get("/api/specs/simple/proposals", headers=as_role("Viewer")).json() == []


def test_approvals_off_keeps_direct_deploys(tmp_path: Path) -> None:
    c = make_client(make_services(tmp_path))
    resp = deploy(c, "Deployer")
    assert resp.status_code == 200
    assert resp.json()["status"] == "deployed"
    assert deploy(c, "Author").status_code == 403


def test_pr_mode_is_exempt_from_proposals(tmp_path: Path, git_repo: Path) -> None:
    c = git_client(tmp_path, git_repo, "pr", require_approval=True)
    pr = deploy(c, "Author")
    assert pr.status_code == 200, pr.text
    assert pr.json()["branch"] == "autoair/simple"
    # a direct push goes live: it needs an approval like any live deploy
    etag = c.get("/api/specs/simple", headers=as_role("Admin")).json()["etag"]
    pushed = deploy(c, "Admin", direct_push=True, base_etag=etag)
    assert pushed.status_code == 202
    assert pushed.json()["status"] == "pending_approval"


# --------------------------------------------------------------------------- sign-in
class FakeAirflow:
    def __init__(self) -> None:
        self.calls: list[httpx2.Request] = []
        self.token = jwt(sub="alice", role="AutoAir Author", exp=time.time() + 600)

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.calls.append(request)
        if request.url.path == "/auth/token":
            body = json.loads(request.content)
            if body == {"username": "alice", "password": "s3cret-pw"}:
                return httpx2.Response(201, json={"access_token": self.token})
            return httpx2.Response(401, json={"detail": "Invalid credentials"})
        if request.url.path == "/api/v2/auth/logout":
            return httpx2.Response(307, headers={"location": "/auth/login"})
        return airflow_ok(request)


@pytest.fixture
def login_client(tmp_path: Path) -> tuple[TestClient, FakeAirflow, Services]:
    fake = FakeAirflow()
    services = make_services(tmp_path, iam={"login_max_failures": 3})
    services.login = LoginService(
        AIRFLOW, services.settings.iam, transport=httpx2.MockTransport(fake)
    )
    return make_client(services), fake, services


def test_login_success_and_logout(login_client, caplog: pytest.LogCaptureFixture) -> None:
    c, fake, _ = login_client
    caplog.set_level(logging.DEBUG)
    resp = c.post("/api/auth/login", json={"username": "alice", "password": "s3cret-pw"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["access_token"] == fake.token
    assert body["token_type"] == "bearer"
    assert body["user"] == "alice"
    assert body["expires_at"]
    assert resp.headers["cache-control"] == "no-store"
    assert fake.calls[0].url == f"{AIRFLOW}/auth/token"
    headers = {"Authorization": f"Bearer {body['access_token']}"}
    me = c.get("/api/me", headers=headers).json()
    assert me["roles"] == ["Author"]
    assert me["login_enabled"] is True
    out = c.post("/api/auth/logout", headers=headers)
    # Airflow answered with a redirect (to its login page): that is not a revocation
    assert out.json() == {"logged_out": True, "revoked": False}
    assert fake.calls[-1].headers["authorization"] == headers["Authorization"]
    assert "s3cret-pw" not in caplog.text


def test_login_failures_are_rate_limited(login_client, caplog: pytest.LogCaptureFixture) -> None:
    c, fake, _ = login_client
    caplog.set_level(logging.DEBUG)
    for _ in range(3):
        bad = c.post("/api/auth/login", json={"username": "alice", "password": "wrong-pw"})
        assert bad.status_code == 401
        assert bad.json()["detail"] == "invalid username or password"
    calls = len(fake.calls)
    limited = c.post("/api/auth/login", json={"username": "alice", "password": "s3cret-pw"})
    assert limited.status_code == 429
    assert int(limited.headers["retry-after"]) > 0
    assert len(fake.calls) == calls  # Airflow is not even asked
    # the same client IP is limited for other usernames too
    assert c.post("/api/auth/login", json={"username": "bob", "password": "x"}).status_code == 429
    assert "wrong-pw" not in caplog.text


def test_login_validation_and_airflow_errors(tmp_path: Path) -> None:
    services = make_services(tmp_path)

    def down(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused")

    services.login = LoginService(
        AIRFLOW, services.settings.iam, transport=httpx2.MockTransport(down)
    )
    c = make_client(services)
    assert c.post("/api/auth/login", json={"username": "", "password": "x"}).status_code == 422
    assert c.post("/api/auth/login", json={"username": "a", "password": "x"}).status_code == 502


def test_login_can_be_disabled(tmp_path: Path) -> None:
    c = make_client(make_services(tmp_path, iam={"login_enabled": False}))
    assert c.post("/api/auth/login", json={"username": "a", "password": "b"}).status_code == 404
    assert c.get("/api/me", headers=as_role("Viewer")).json()["login_enabled"] is False
