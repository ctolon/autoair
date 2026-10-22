"""Regression tests for the IAM review findings (direct push, rate limiting, JWT parsing,
token cache, custom views, approvals index, import check without code:execute…)."""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from pathlib import Path
from typing import Any

import httpx2
import pytest

from autoair.api.app import create_app
from autoair.auth import TokenVerifier
from autoair.iam import model as P
from autoair.iam.config import IamSettings
from autoair.iam.deps import CustomViewCache
from autoair.iam.fab import sync_fab
from autoair.iam.login import LoginLimiter, LoginService
from autoair.iam.resolver import Identity, jwt_claims, resolve
from autoair.storage.config import GitStorageSettings, StorageSettings
from tests.integration.test_iam_api import (
    AIRFLOW,
    SIMPLE,
    airflow_ok,
    as_role,
    deploy,
    git_repo,  # noqa: F401 - fixture
    make_client,
    make_services,
)

pytestmark = pytest.mark.airflow

UNVERIFIED = {k: (True, False) for k in ("dag_read", "dag_edit", "configuration_edit")}


# --------------------------------------------------------------------------- 16: wildcards
def test_wildcard_role_never_grants_direct_push() -> None:
    s = IamSettings(roles={"Admin": ["*"]})
    untrusted = resolve(Identity(user="x", checks=UNVERIFIED), s, auth="bearer", mode="sa")
    assert not untrusted.has(P.DEPLOY_DIRECT_PUSH)
    anonymous = resolve(Identity(user=None, authenticated=False), s, auth="none", mode="sa")
    assert not anonymous.has(P.DEPLOY_DIRECT_PUSH)
    assert not anonymous.can_direct_push(["Admin"])
    verified = {k: (True, True) for k in UNVERIFIED}
    trusted = resolve(
        Identity(user="x", checks=verified), s, auth="t", mode="plugin", direct_push_roles=[]
    )
    assert not trusted.has(P.DEPLOY_DIRECT_PUSH)  # '*' is not an explicit grant


def test_explicit_direct_push_needs_a_trusted_grant() -> None:
    s = IamSettings(
        roles={"Pusher": ["deploy:direct_push", "deploy"]},
        default_roles=[],
        anonymous_roles=["Pusher"],
        strategies=["role_mapping"],
        role_mapping={"push": ["Pusher"]},
    )
    anonymous = resolve(Identity(user=None, authenticated=False), s, auth="none", mode="sa")
    assert anonymous.has(P.DEPLOY)
    assert not anonymous.has(P.DEPLOY_DIRECT_PUSH)
    mapped = resolve(
        Identity(user="u", airflow_roles=["push"]), s, auth="t", mode="sa", direct_push_roles=[]
    )
    assert mapped.has(P.DEPLOY_DIRECT_PUSH)
    # an unverifiable airflow_permissions check never grants its own roles...
    s2 = s.model_copy(
        update={
            "strategies": ["airflow_permissions"],
            "airflow_permission_roles": s.airflow_permission_roles.model_copy(
                update={"dag_edit": ["Pusher"]}
            ),
        }
    )
    unverified = resolve(
        Identity(user="u", checks=UNVERIFIED), s2, auth="t", mode="sa", direct_push_roles=[]
    )
    assert not unverified.has(P.DEPLOY)
    # ...and the role granted in its place (unverified) never brings direct push
    s3 = s2.model_copy(
        update={
            "airflow_permission_roles": s2.airflow_permission_roles.model_copy(
                update={"unverified": ["Pusher"]}
            ),
        }
    )
    fallback = resolve(
        Identity(user="u", checks=UNVERIFIED), s3, auth="t", mode="sa", direct_push_roles=[]
    )
    assert fallback.has(P.DEPLOY)
    assert not fallback.has(P.DEPLOY_DIRECT_PUSH)


# --------------------------------------------------------------------------- 18: JWT payloads
def _token(payload: bytes) -> str:
    return "Bearer a." + base64.urlsafe_b64encode(payload).decode().rstrip("=") + ".c"


def test_jwt_claims_never_raises() -> None:
    assert jwt_claims(_token(b"[" * 50_000)) == {}
    assert jwt_claims(_token(b"[" * 5_000)) == {}  # under the size cap, still too deep
    assert jwt_claims(_token(b"[1]")) == {}
    assert jwt_claims(_token(b'{"sub": "a"}')) == {"sub": "a"}


def test_request_user_on_non_object_payload() -> None:
    from autoair.audit import request_user

    class R:
        def __init__(self, payload: bytes) -> None:
            self.headers = {"authorization": _token(payload)}
            self.cookies: dict[str, str] = {}

    assert request_user(R(b"[1]")) is None  # type: ignore[arg-type]
    assert request_user(R(b"[" * 5000)) is None  # type: ignore[arg-type]


def test_nested_jwt_is_not_a_server_error(tmp_path: Path) -> None:
    c = make_client(make_services(tmp_path))
    deep = base64.urlsafe_b64encode(b"[" * 10_000).decode().rstrip("=")
    resp = c.get("/api/me", headers={"Authorization": f"Bearer a.{deep}.c"})
    assert resp.status_code < 500


# --------------------------------------------------------------------------- 17: login limiter
def test_concurrent_logins_cannot_bypass_the_limit(tmp_path: Path) -> None:
    calls = {"n": 0}

    async def slow_airflow(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/auth/token":
            calls["n"] += 1
            await asyncio.sleep(0.05)
            return httpx2.Response(401, json={"detail": "bad"})
        return airflow_ok(request)

    services = make_services(tmp_path, iam={"login_max_failures": 5})
    services.login = LoginService(
        AIRFLOW, services.settings.iam, transport=httpx2.MockTransport(slow_airflow)
    )
    app = create_app(services)

    async def main() -> list[int]:
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://t") as c:
            rs = await asyncio.gather(
                *[
                    c.post("/api/auth/login", json={"username": "victim", "password": f"p{i}"})
                    for i in range(30)
                ]
            )
        return [r.status_code for r in rs]

    statuses = asyncio.run(main())
    assert calls["n"] == 5
    assert statuses.count(401) == 5
    assert statuses.count(429) == 25


def test_login_limiter_refunds_and_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    limiter = LoginLimiter(max_failures=2, window=60, max_keys=3)
    first = limiter.reserve(["ip:a", "user:x"])
    assert isinstance(first, tuple)
    limiter.refund(first)  # e.g. Airflow unreachable or the sign-in succeeded
    assert len(limiter) == 0
    for _ in range(2):
        assert isinstance(limiter.reserve(["ip:a"]), tuple)
    assert isinstance(limiter.reserve(["ip:a"]), float)  # at the limit
    for i in range(10):  # a spray of keys never grows past the cap
        limiter.failed([f"user:{i}"])
    assert len(limiter) == 3


# --------------------------------------------------------------------------- 21: logout
def test_logout_counts_only_2xx_as_revoked(tmp_path: Path) -> None:
    status = {"code": 307}

    def airflow(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/api/v2/auth/logout":
            return httpx2.Response(status["code"], headers={"location": "/auth/login"})
        return airflow_ok(request)

    services = make_services(tmp_path)
    services.login = LoginService(
        AIRFLOW, services.settings.iam, transport=httpx2.MockTransport(airflow)
    )
    c = make_client(services)
    assert c.post("/api/auth/logout", headers=as_role("Viewer")).json()["revoked"] is False
    status["code"] = 204
    assert c.post("/api/auth/logout", headers=as_role("Viewer")).json()["revoked"] is True


# --------------------------------------------------------------------------- 24: token cache
def test_token_verifier_evicts_least_recently_used() -> None:
    calls: list[str] = []

    def airflow(request: httpx2.Request) -> httpx2.Response:
        calls.append(request.headers["authorization"])
        return httpx2.Response(200, json={"dags": []})

    verifier = TokenVerifier(AIRFLOW, transport=httpx2.MockTransport(airflow), max_entries=3)

    class Req:
        def __init__(self, token: str) -> None:
            self.headers = {"authorization": f"Bearer {token}"}
            self.cookies: dict[str, str] = {}

    async def check(*tokens: str) -> None:
        for token in tokens:
            await verifier(Req(token))  # type: ignore[arg-type]

    asyncio.run(check("a", "b", "c", "a", "d"))  # 'a' was used again: 'b' is evicted
    assert len(calls) == 4
    calls.clear()
    asyncio.run(check("a", "c", "d"))
    assert calls == []  # still cached (no burst of re-verification)
    asyncio.run(check("b"))
    assert calls == ["Bearer b"]


# --------------------------------------------------------------------------- 23: custom views
def test_custom_view_answers_are_cached_per_user_and_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asked: list[tuple[str, str]] = []

    def check(method: str, resource: str) -> bool:
        asked.append((method, resource))
        return resource.endswith("Deploy")

    cache = CustomViewCache(ttl=30)
    s = IamSettings(strategies=["custom_view"], default_roles=[])
    for _ in range(3):
        wrapped = cache.wrap(("u", "token-1"), check)
        p = resolve(Identity(user="u", custom_view=wrapped), s, auth="t", mode="plugin")
        assert p.has(P.DEPLOY)
    assert len(asked) == len(P.PERMISSIONS)  # asked once, not once per request
    resolve(
        Identity(user="u", custom_view=cache.wrap(("u", "token-2"), check)),
        s,
        auth="t",
        mode="plugin",
    )
    assert len(asked) == 2 * len(P.PERMISSIONS)  # another token: asked again
    now = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: now + 31)
    resolve(
        Identity(user="u", custom_view=cache.wrap(("u", "token-1"), check)),
        s,
        auth="t",
        mode="plugin",
    )
    assert len(asked) == 3 * len(P.PERMISSIONS)  # expired
    assert CustomViewCache(ttl=0).wrap(("u", "t"), check) is check


# --------------------------------------------------------------------------- 19: config checks
def test_iam_settings_validate_names(caplog: pytest.LogCaptureFixture) -> None:
    with pytest.raises(ValueError, match="unknown permission"):
        IamSettings(roles={"Auditor": ["spec:raed"]})
    with pytest.raises(ValueError, match="unknown permission"):
        IamSettings(roles={"Auditor": ["nothing:*"]})
    caplog.set_level(logging.WARNING)
    IamSettings(
        roles={"Auditor": ["spec:read", "audit:*"]},
        default_roles=["Auditor", "viewer"],
        role_mapping={"data-eng": ["Authr"]},
        anonymous_roles=["Ghost"],
    )
    assert "Authr" in caplog.text
    assert "Ghost" in caplog.text
    assert "Auditor" not in caplog.text
    assert "viewer" not in caplog.text


def test_sync_fab_refuses_to_touch_builtin_roles() -> None:
    with pytest.raises(ValueError, match="built-in roles"):
        sync_fab(object(), IamSettings(role_prefix=""), create_roles=True)


def test_standalone_airflow_permissions_with_role_mapping_restricts(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Standalone cannot check Airflow's permissions: airflow_permissions adds only the
    unverified roles (Viewer), so a combined role mapping really restricts."""
    caplog.set_level(logging.INFO)
    c = make_client(
        make_services(tmp_path, iam={"strategies": ["airflow_permissions", "role_mapping"]})
    )
    assert "airflow_permission_roles.unverified" in caplog.text
    me = c.get("/api/me", headers=as_role("Viewer")).json()
    assert "Admin" not in me["roles"]
    assert set(me["roles"]) == {"Authenticated", "Viewer"}
    deployer = c.get("/api/me", headers=as_role("Deployer")).json()
    assert "Deployer" in deployer["roles"]


# --------------------------------------------------------------------------- 15: [] = nobody
def test_storage_direct_push_roles_empty_forbids(tmp_path: Path, git_repo: Path) -> None:  # noqa: F811
    storage = StorageSettings(
        mode="git",
        git=GitStorageSettings(
            provider="local",
            repo=f"file://{git_repo}",
            mode="pr",
            cache_dir=tmp_path / "cache",
            direct_push_roles=[],
        ),
    )
    c = make_client(make_services(tmp_path, storage=storage))
    assert deploy(c, "Admin", direct_push=True).status_code == 403
    me = c.get("/api/me", headers=as_role("Admin")).json()
    assert me["deploy"]["direct_push"] is False
    assert P.DEPLOY_DIRECT_PUSH not in me["permissions"]
    assert GitStorageSettings().direct_push_roles is None  # unset: iam.direct_push_roles


def test_me_reports_direct_push_for_the_team_target(tmp_path: Path, git_repo: Path) -> None:  # noqa: F811
    team = StorageSettings(
        mode="git",
        git=GitStorageSettings(
            provider="local",
            repo=f"file://{git_repo}",
            mode="pr",
            cache_dir=tmp_path / "cache",
            direct_push_roles=["Deployer"],
        ),
    )
    services = make_services(tmp_path, team_storage={"blue": team}, team="blue")
    c = make_client(services)
    me = c.get("/api/me", headers=as_role("Deployer")).json()
    assert me["deploy"]["git_mode"] == "pr"
    assert me["deploy"]["direct_push"] is True
    assert P.DEPLOY_DIRECT_PUSH in me["permissions"]
    admin = c.get("/api/me", headers=as_role("Admin")).json()["deploy"]
    assert admin["direct_push"] is False
    # the default (local) target via ?team=
    other = c.get("/api/me", params={"team": "nope"}, headers=as_role("Deployer")).json()
    assert other["deploy"]["git_mode"] is None


# --------------------------------------------------------------------------- 22: approvals index
def test_approvals_do_not_depend_on_the_audit_log(tmp_path: Path) -> None:
    services = make_services(tmp_path, iam={"require_approval": True})
    c = make_client(services)
    proposed = c.put("/api/specs/simple", json={"spec": SIMPLE}, headers=as_role("Author", "alice"))
    etag = proposed.json()["proposal_etag"]
    review = {"etag": etag, "verdict": "approve"}
    assert (
        c.post("/api/specs/simple/reviews", json=review, headers=as_role("Reviewer", "rita"))
    ).status_code == 200

    def no_audit(*args: Any, **kwargs: Any) -> list[Any]:
        raise AssertionError("approvals must not scan the audit log")

    services.audit.tail = no_audit  # type: ignore[method-assign]
    resp = c.post(f"/api/specs/simple/proposals/{etag}/deploy", headers=as_role("Deployer"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["deployed"]


def test_proposals_expire(tmp_path: Path) -> None:
    services = make_services(tmp_path, iam={"require_approval": True, "proposal_ttl_seconds": 1})
    c = make_client(services)
    etag = deploy(c, "Deployer").json()["proposal_etag"]
    assert len(c.get("/api/specs/simple/proposals", headers=as_role("Viewer")).json()) == 1
    assert services.proposals is not None
    services.proposals.ttl = -1  # everything is older than the TTL
    assert c.get("/api/specs/simple/proposals", headers=as_role("Viewer")).json() == []
    assert (
        c.get(f"/api/specs/simple/proposals/{etag}", headers=as_role("Viewer")).status_code == 404
    )


# --------------------------------------------------------------------------- security #6
PYOP = "airflow.providers.standard.operators.python.PythonOperator"


def _rce_spec(marker: Path) -> dict[str, Any]:
    source = (
        f"@(lambda g: (__import__('pathlib').Path({str(marker)!r}).write_text('x'), g)[1])\n"
        "def f():\n    pass\n"
    )
    return {
        "dag_id": "evil",
        "nodes": [
            {
                "kind": "operator",
                "id": "a",
                "task_id": "a",
                "operator": PYOP,
                "params": {"python_callable": {"$type": "function", "source": source}},
            }
        ],
    }


def test_deploy_import_check_needs_code_execute(tmp_path: Path) -> None:
    services = make_services(tmp_path, import_check_on_deploy=True)
    c = make_client(services)
    marker = tmp_path / "ran"
    spec = _rce_spec(marker)
    resp = c.put("/api/specs/evil", json={"spec": spec}, headers=as_role("Deployer"))
    assert resp.status_code == 200, resp.text
    assert not marker.exists()  # no DAG code ran on the AutoAir server
    problems = resp.json()["report"]["problems"]
    skipped = [p for p in problems if p["code"] == "import_check_skipped"]
    assert skipped
    assert skipped[0]["severity"] == "info"
    assert skipped[0]["message"].startswith("import check skipped")


def test_deploy_import_check_runs_with_code_execute(tmp_path: Path) -> None:
    services = make_services(tmp_path, import_check_on_deploy=True)
    c = make_client(services)
    resp = c.put("/api/specs/simple", json={"spec": SIMPLE}, headers=as_role("Admin"))
    assert resp.status_code == 200, resp.text
    report = resp.json()["report"]
    assert report["import_check"] is not None
    assert not any(p["code"] == "import_check_skipped" for p in report["problems"])


def test_import_check_requirement_can_be_switched_off(tmp_path: Path) -> None:
    services = make_services(
        tmp_path,
        import_check_on_deploy=True,
        iam={"import_check_requires_code_execute": False},
    )
    c = make_client(services)
    resp = c.put("/api/specs/simple", json={"spec": SIMPLE}, headers=as_role("Deployer"))
    assert resp.json()["report"]["import_check"] is not None
