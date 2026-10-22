"""State backends end to end: two replicas (two apps) sharing one store, the admin API and
the shared sign-in limiter. File + SQLite always, PostgreSQL with AUTOAIR_TEST_POSTGRES_URL."""

from __future__ import annotations

import base64
import csv
import io
import json
import time
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient

pytest.importorskip("sqlalchemy")

from autoair.api.app import Services, build_services, create_app
from autoair.auth import TokenVerifier, bearer_passthrough
from autoair.collab import collab_secret, issue_ticket, key_file, verify_ticket
from autoair.iam import model as P
from autoair.iam.config import IamSettings
from autoair.iam.login import LoginService
from autoair.settings import Settings
from tests.db_backends import BACKENDS, SQL_BACKENDS, database_settings, drop_tables

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


class FakeAirflow:
    def __init__(self) -> None:
        self.token = jwt(sub="alice", role="AutoAir Author", exp=time.time() + 600)

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/auth/token":
            body = json.loads(request.content)
            if body == {"username": "alice", "password": "s3cret-pw"}:
                return httpx2.Response(201, json={"access_token": self.token})
            return httpx2.Response(401, json={"detail": "Invalid credentials"})
        if request.url.path == "/api/v2/dags":
            return httpx2.Response(200, json={"dags": [], "total_entries": 0})
        return httpx2.Response(404, json={"detail": "not found"})


def make_settings(root: Path, database: dict[str, Any], **extra: Any) -> Settings:
    return Settings(  # type: ignore[call-arg]
        dags_folder=root / "dags",
        subfolder="",
        ai={"enabled": False},
        features={"mcp": False},
        airflow_url=AIRFLOW,
        expose_config="true",
        iam=IamSettings(strategies=["role_mapping"], login_max_failures=3),
        database=database,
        **extra,
    )


def replica(settings: Settings) -> tuple[TestClient, Services]:
    fake = FakeAirflow()
    verifier = TokenVerifier(AIRFLOW, transport=httpx2.MockTransport(fake))
    services = build_services(settings, bearer_passthrough(verifier))
    services.login = LoginService(AIRFLOW, settings.iam, transport=httpx2.MockTransport(fake))
    return TestClient(create_app(services), raise_server_exceptions=False), services


@pytest.fixture(params=BACKENDS)
def replicas(request: pytest.FixtureRequest, tmp_path: Path) -> Any:
    """Two replicas sharing one state store (same state folder / same database)."""
    database = database_settings(request.param, tmp_path)
    settings = make_settings(tmp_path, database)
    a, sa = replica(settings)
    b, sb = replica(settings)
    yield request.param, (a, sa), (b, sb)
    drop_tables(settings)


@pytest.fixture(params=SQL_BACKENDS)
def sql_replicas(request: pytest.FixtureRequest, tmp_path: Path) -> Any:
    database = database_settings(request.param, tmp_path)
    settings = make_settings(tmp_path, database)
    a, sa = replica(settings)
    b, sb = replica(settings)
    yield (a, sa), (b, sb)
    drop_tables(settings)


def _png() -> bytes:
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGBA", (8, 8), (255, 0, 0, 255)).save(out, format="PNG")
    return out.getvalue()


TEMPLATE = {
    "id": "nightly",
    "title": '=HYPERLINK("x")',
    "kind": "dag",
    "spec": {"dag_id": "nightly", "nodes": [], "edges": []},
}


def test_replicas_share_templates_icons_and_audit(replicas: Any) -> None:
    kind, (a, sa), (b, _sb) = replicas
    assert (sa.database is None) == (kind == "file")
    saved = a.post("/api/templates", json=TEMPLATE, headers=ADMIN)
    assert saved.status_code == 200, saved.text
    listed = b.get("/api/templates", headers=ADMIN).json()
    assert "nightly" in {t["id"] for t in listed}
    assert b.get("/api/templates/nightly", headers=ADMIN).json()["title"] == TEMPLATE["title"]

    up = a.post(
        "/api/icons",
        json={"name": "acme", "data": base64.b64encode(_png()).decode()},
        headers=ADMIN,
    )
    assert up.status_code == 200, up.text
    icon_id = up.json()["id"]
    assert b.get(f"/api/icons/{icon_id}").status_code == 200
    put = b.put("/api/icons/assignments", json={"provider": "acme", "icon": icon_id}, headers=ADMIN)
    assert put.status_code == 200, put.text
    assert a.get("/api/icons/assignments", headers=ADMIN).json()["providers"] == {"acme": icon_id}

    page = b.get("/api/admin/audit", params={"user": "root"}, headers=ADMIN).json()
    assert any(r["action"] == "template_save" for r in page["items"]), page


def test_deploy_target_choice_reaches_other_replicas(sql_replicas: Any, tmp_path: Path) -> None:
    (a, _sa), (b, sb) = sql_replicas
    target = {"mode": "local", "local_path": str(tmp_path / "elsewhere")}
    resp = a.put("/api/deploy-target", json=target, headers=ADMIN)
    assert resp.status_code == 200, resp.text
    assert sb.storage_source == "settings"
    sb.sync_shared_state(now=time.monotonic() + 3600)
    assert sb.storage_source == "ui"
    assert str(sb.settings.storage.local_path) == str(tmp_path / "elsewhere")
    response = a.delete("/api/deploy-target", headers=ADMIN)
    assert response.status_code == 200
    sb.sync_shared_state(now=time.monotonic() + 7200)
    assert sb.storage_source == "settings"
    assert (
        b.get("/api/admin/overview", headers=ADMIN).json()["backends"]["deploy_target_source"]
        == "settings"
    )


def test_login_limit_holds_across_replicas(sql_replicas: Any) -> None:
    (a, _), (b, _) = sql_replicas
    for _ in range(3):
        bad = a.post("/api/auth/login", json={"username": "alice", "password": "nope"})
        assert bad.status_code == 401
    limited = b.post("/api/auth/login", json={"username": "alice", "password": "s3cret-pw"})
    assert limited.status_code == 429
    assert int(limited.headers["retry-after"]) > 0


def test_admin_routes_need_the_admin_panel_permission(replicas: Any) -> None:
    _, (a, _), _ = replicas
    paths = [
        "/api/admin/overview",
        "/api/admin/audit",
        "/api/admin/audit.csv",
        "/api/admin/templates",
        "/api/admin/users",
        "/api/admin/ai-usage",
        "/api/admin/proposals",
        "/api/admin/database",
        "/api/admin/caches",
    ]
    for role in P.BUILTIN_ROLES:
        allowed = P.ADMIN_PANEL in P.BUILTIN_ROLES[role]
        for path in paths:
            status = a.get(path, headers=as_role(role)).status_code
            assert (status != 403) == allowed, (role, path, status)
    assert P.ADMIN_PANEL in P.BUILTIN_ROLES[P.ADMIN]
    assert (
        a.post("/api/admin/caches/marketplace/clear", headers=as_role("Viewer")).status_code == 403
    )


def test_admin_overview_audit_csv_and_users(replicas: Any) -> None:
    kind, (a, _sa), (b, _) = replicas
    assert (
        a.post(
            "/api/templates", json=TEMPLATE, headers=as_role("TemplateManager", "tm")
        ).status_code
        == 200
    )
    a.get("/api/me", headers=as_role("Viewer", "vera"))
    overview = b.get("/api/admin/overview", headers=ADMIN)
    assert overview.status_code == 200, overview.text
    body = overview.json()
    assert body["backends"]["database"] == kind
    assert body["backends"]["collab_sync"] == ("memory" if kind == "file" else "database")
    assert body["features"]["collab"] is True
    assert body["user_templates"] == 1
    assert any(r["current"] for r in body["replicas"])
    if kind != "file":
        assert body["backends"]["schema_version"] == body["backends"]["schema_head"]
        assert (
            "***" in body["backends"]["database_url"] or "@" not in body["backends"]["database_url"]
        )

    filtered = b.get(
        "/api/admin/audit", params={"action": "template_save", "limit": 1}, headers=ADMIN
    ).json()
    assert [r["user"] for r in filtered["items"]] == ["tm"]
    assert (
        b.get("/api/admin/audit", params={"since": "not-a-date"}, headers=ADMIN).status_code == 422
    )
    exported = b.get("/api/admin/audit.csv", params={"action": "template_save"}, headers=ADMIN)
    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith("text/csv")
    assert "attachment" in exported.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(exported.text)))
    assert rows[0] == ["timestamp", "action", "user", "dag_id", "details"]
    assert rows[1][1] == "template_save"

    users = {u["user"]: u for u in b.get("/api/admin/users", headers=ADMIN).json()}
    assert users["vera"]["roles"] == ["Viewer"]
    assert users["tm"]["actions"] >= 1
    assert users["tm"]["last_action"] == "template_save"

    templates = b.get("/api/admin/templates", headers=ADMIN).json()
    assert templates[0]["id"] == "nightly"
    assert not templates[0]["builtin"]
    response = b.delete("/api/admin/templates/nightly", headers=ADMIN)
    assert response.status_code == 200
    response = b.delete("/api/admin/templates/nightly", headers=ADMIN)
    assert response.status_code == 404
    assert "nightly" not in {t["id"] for t in a.get("/api/templates", headers=ADMIN).json()}
    assert a.get("/api/config", headers=ADMIN).status_code == 200


def test_admin_ai_usage_proposals_database_and_caches(replicas: Any) -> None:
    from autoair.ai.audit_log import AiAuditRecord, audit_for, now_iso
    from autoair.ai.usage import Usage
    from autoair.iam.proposals import ReviewRecord, new_proposal
    from autoair.spec import DagSpec

    kind, (_a, sa), (b, sb) = replicas
    log = audit_for(sa)
    for user, cost in (("alice", 0.5), ("alice", 0.25), ("bob", None)):
        log.record(
            AiAuditRecord(
                timestamp=now_iso(),
                user=user,
                feature="explain",
                provider="anthropic",
                model="m1",
                usage=Usage(requests=1, input_tokens=100, output_tokens=50, cost_usd=cost),
            )
        )
    usage = {r["user"]: r for r in b.get("/api/admin/ai-usage", headers=ADMIN).json()}
    assert usage["alice"]["requests"] == 2
    assert usage["alice"]["cost_usd"] == pytest.approx(0.75)
    assert usage["alice"]["input_tokens"] == 200
    assert usage["bob"]["cost_usd"] is None

    spec = DagSpec.model_validate({"dag_id": "etl", "nodes": [], "edges": []})
    store = sa.proposals
    assert store is not None
    store.save(
        new_proposal(
            spec,
            "v1",
            author="alice",
            message="go",
            base_etag=None,
            environment=None,
            force=False,
            direct_push=None,
        )
    )
    store.add_review(
        "etl", ReviewRecord(timestamp=now_iso(), user="bob", etag="v1", verdict="approve")
    )
    proposals = b.get("/api/admin/proposals", headers=ADMIN).json()
    assert [(p["dag_id"], p["etag"], p["approvals"]) for p in proposals["proposals"]] == [
        ("etl", "v1", ["bob"])
    ]
    assert proposals["reviews"][0]["verdict"] == "approve"

    database = b.get("/api/admin/database", headers=ADMIN).json()
    assert database["backend"] == kind
    if kind == "file":
        assert database["current"] is None
        assert b.post("/api/admin/database/upgrade", headers=ADMIN).status_code == 409
    else:
        assert database["current"] == database["head"]
        assert database["pending"] == []
        assert database["tables"][f"{sb.database.prefix}ai_audit"] >= 3  # type: ignore[union-attr]
        up = b.post("/api/admin/database/upgrade", headers=ADMIN)
        assert up.status_code == 200, up.text

    caches = {c["name"] for c in b.get("/api/admin/caches", headers=ADMIN).json()}
    assert caches == {"catalog", "marketplace", "ai-index"}
    cleared = b.post("/api/admin/caches/marketplace/clear", headers=ADMIN)
    assert cleared.json() == {"cache": "marketplace", "ok": True, "detail": None}
    assert b.post("/api/admin/caches/ai-index/clear", headers=ADMIN).status_code == 404  # AI off
    assert b.post("/api/admin/caches/nope/clear", headers=ADMIN).status_code == 422


def test_collab_tickets_validate_on_every_replica(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without AUTOAIR_COLLAB_SECRET and Airflow's jwt_secret the ticket key comes from the
    shared store (state folder key file / database), never from the process."""
    import airflow.configuration as af_conf

    real_get = af_conf.conf.get

    def no_jwt(section: str, key: str, *args: Any, **kwargs: Any) -> Any:
        if (section, key) == ("api_auth", "jwt_secret"):
            return None
        return real_get(section, key, *args, **kwargs)

    monkeypatch.setattr(af_conf.conf, "get", no_jwt)
    for database in (
        {"backend": "file"},
        {"backend": "sqlite", "url": f"sqlite:///{tmp_path / 'c.db'}"},
    ):
        settings = make_settings(tmp_path / database["backend"], database)
        _, one = replica(settings)
        _, two = replica(settings)
        assert one.collab_secret == two.collab_secret
        ticket = issue_ticket(one.collab_secret, "etl", user="alice")
        assert verify_ticket(two.collab_secret, ticket, "etl") is not None
        drop_tables(settings)
    # The per-process fallback only remains when nothing shared exists at all.
    assert collab_secret(None, fallback=None) != collab_secret(None, fallback=None)
    path = tmp_path / "k" / "collab.key"
    assert key_file(path) == key_file(path)
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert collab_secret("configured") == b"configured"


def test_sql_backend_is_used_by_ai_memory_and_knowledge(sql_replicas: Any) -> None:
    from autoair.ai.knowledge import knowledge_for
    from autoair.ai.memory import memory_for
    from autoair.persistence.sql import SqlDocumentStore, SqlMemoryRepository

    (_, sa), (_, sb) = sql_replicas
    memory = memory_for(sa)
    assert isinstance(memory.repository, SqlMemoryRepository)
    item = memory.add("Owner is data-eng@", created_by="alice")
    assert [i.id for i in memory_for(sb).list()[0]] == [item.id]
    kb = knowledge_for(sa)
    assert isinstance(kb.docs, SqlDocumentStore)
    kb.put("acme-conventions", "# Acme\n\nUse the acme pool.")
    entry = next(d for d in knowledge_for(sb).entries() if d.id == "acme-conventions")
    assert entry.origin == "custom"
    assert entry.updated_at
    kb.set_enabled("acme-conventions", False)
    assert not next(d for d in knowledge_for(sb).entries() if d.id == "acme-conventions").enabled


def test_sql_user_llm_keys_are_shared_and_encrypted(sql_replicas: Any) -> None:
    from pydantic import SecretStr

    from autoair.ai.user_llm import UserLlmUpdate, store_for

    (_, sa), (_, sb) = sql_replicas
    first = store_for(sa)
    first.put("alice", UserLlmUpdate(provider="anthropic", api_key=SecretStr("sk-ant-0123456789")))
    second = store_for(sb)
    stored = second.get("alice")
    assert stored is not None
    assert stored.api_key_enc
    assert "0123456789" not in stored.api_key_enc
    assert second.api_key(stored) == "sk-ant-0123456789"
    raw = sa.database.tables.documents  # type: ignore[union-attr]
    from sqlalchemy import select

    with sa.database.begin() as conn:  # type: ignore[union-attr]
        rows = conn.execute(select(raw.c.data).where(raw.c.collection == "ai_user_llm")).all()
    assert rows
    assert all(b"sk-ant-0123456789" not in bytes(r.data) for r in rows)
