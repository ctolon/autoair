"""``/api/kb``: permissions, feature switch, search, sources, refresh and bundles."""

from __future__ import annotations

import io
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from autoair.iam import model as P
from autoair.kb.service import kb_for
from tests.integration.test_iam_api import as_role, make_client, make_services


def _docs(tmp_path: Path) -> Path:
    docs = tmp_path / "corp-docs"
    docs.mkdir(parents=True)
    (docs / "pools.md").write_text("# Pools\n\n## Limits\n\nPools limit task parallelism.\n")
    (docs / "xcom.md").write_text("# XCom\n\nXComs pass small values between tasks.\n")
    return docs


def _client(tmp_path: Path, **settings: Any) -> TestClient:
    kb = {
        "sources": [
            {
                "id": "corp",
                "kind": "path",
                "url": str(_docs(tmp_path)),
                "layout": "files",
                "public_base_url": "https://wiki.corp/airflow",
            }
        ],
        "refresh_interval_hours": 0,
        "refresh_on_start": False,
        **settings.pop("kb", {}),
    }
    services = make_services(tmp_path, kb=kb, **settings)
    client = make_client(services)
    client.services = services  # type: ignore[attr-defined]
    return client


def _wait(c: TestClient) -> dict[str, Any]:
    for _ in range(200):
        status = c.get("/api/kb/status", headers=as_role("Admin")).json()
        if status["run"]["state"] != "running":
            return status  # type: ignore[no-any-return]
        time.sleep(0.05)
    raise AssertionError("refresh did not finish")


def _has(role: str, *perms: str) -> bool:
    return set(perms) <= set(P.BUILTIN_ROLES[role])


@pytest.mark.parametrize("role", list(P.BUILTIN_ROLES))
def test_permissions(tmp_path: Path, role: str) -> None:
    c = _client(tmp_path)
    headers = as_role(role)
    read = c.get("/api/kb/status", headers=headers).status_code
    search = c.get("/api/kb/search", params={"q": "pools"}, headers=headers).status_code
    assert (read == 200) == _has(role, P.AI_USE)
    assert read in (200, 403)
    assert (search == 200) == _has(role, P.AI_USE)
    manage = c.post("/api/kb/refresh", json={}, headers=headers).status_code
    may_manage = _has(role, P.AI_ADMIN) or _has(role, P.ADMIN_CONFIG)
    assert (manage == 202) == may_manage, manage
    if may_manage:
        _wait(c)
    put = c.put("/api/kb/sources/corp", json={"enabled": True}, headers=headers).status_code
    assert (put == 200) == may_manage
    export = c.get("/api/kb/export", headers=headers).status_code
    assert (export == 200) == may_manage


def test_feature_off_answers_404(tmp_path: Path) -> None:
    c = _client(tmp_path, features={"kb": False})
    assert c.get("/api/kb/status", headers=as_role("Admin")).status_code == 404
    assert c.post("/api/kb/refresh", json={}, headers=as_role("Admin")).status_code == 404


def test_refresh_search_sources_and_bundles(tmp_path: Path) -> None:
    c = _client(tmp_path)
    admin = as_role("Admin")
    assert c.post("/api/kb/refresh", json={"sources": ["nope"]}, headers=admin).status_code == 422
    assert c.post("/api/kb/refresh", json={}, headers=admin).status_code == 202
    status = _wait(c)
    assert status["run"]["outcome"] == "ok"
    assert status["store"]["pages"] == 2
    assert status["mode"] == "lexical"
    assert status["sources"][0]["id"] == "corp"
    assert status["sources"][0]["pages"] == 2
    result = c.get("/api/kb/search", params={"q": "parallelism"}, headers=admin).json()
    assert result["mode"] == "lexical"
    hit = result["hits"][0]
    assert hit["url"] == "https://wiki.corp/airflow/pools.md#limits"
    assert hit["source"] == "corp"
    assert hit["component"] == "custom"
    filtered = c.get(
        "/api/kb/search", params={"q": "parallelism", "component": "core"}, headers=admin
    ).json()
    assert filtered["hits"] == []
    bad = c.get("/api/kb/search", params={"q": "x", "package": "a b"}, headers=admin)
    assert bad.status_code == 422
    # override + reset
    off = c.put("/api/kb/sources/corp", json={"enabled": False}, headers=admin).json()
    assert off["enabled"] is False
    assert off["overridden"] is True
    assert c.put("/api/kb/sources/nope", json={}, headers=admin).status_code == 404
    assert (
        c.put("/api/kb/sources/corp", json={"components": ["bogus"]}, headers=admin).status_code
        == 422
    )
    reset = c.delete("/api/kb/sources/corp/override", headers=admin).json()
    assert reset["enabled"] is True
    assert reset["overridden"] is False
    # export -> import into another installation
    export = c.get("/api/kb/export", headers=admin)
    assert export.headers["content-type"] == "application/zip"
    other = _client(tmp_path / "other")
    imported = other.post(
        "/api/kb/import",
        params={"source_id": "imported"},
        content=export.content,
        headers={**admin, "content-type": "application/zip"},
    )
    assert imported.status_code == 200, imported.text
    assert imported.json()["pages"] == 2
    hits = other.get("/api/kb/search", params={"q": "xcom"}, headers=admin).json()["hits"]
    assert hits[0]["source"] == "imported"
    garbage = other.post(
        "/api/kb/import", content=b"nope", headers={**admin, "content-type": "application/zip"}
    )
    assert garbage.status_code == 422
    assert io.BytesIO(export.content).read(2) == b"PK"


def test_refresh_conflict_while_running(tmp_path: Path) -> None:
    c = _client(tmp_path)
    kb = kb_for(c.services)  # type: ignore[attr-defined]
    owner = kb.state.new_owner()
    assert kb.state.acquire(owner, 600)
    try:
        resp = c.post("/api/kb/refresh", json={}, headers=as_role("Admin"))
        assert resp.status_code == 409
    finally:
        kb.state.release(owner)
