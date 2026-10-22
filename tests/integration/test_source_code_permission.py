"""Hand-written ``source.code`` in a spec is arbitrary Python a later deploy may write as the
DAG file: storing new or changed code needs ``code:raw`` at save time on every path (REST
deploy, code-mode apply, MCP save), not only when a deploy writes the file."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.integration.test_iam_api import SIMPLE, as_role, make_client, make_services

pytestmark = pytest.mark.airflow

ROLES = {
    # deploys, but may not store hand-written Python
    "Shipper": ["builder:use", "spec:*", "deploy"],
    # code mode without code:raw
    "Coder": ["builder:use", "spec:write", "code:execute"],
}
CODE = "from airflow.sdk import DAG\n\nwith DAG('simple'):\n    pass\n"


def _with_code(code: str | None, **extra: Any) -> dict[str, Any]:
    spec = copy.deepcopy(SIMPLE)
    if code is not None:
        spec["source"] = {"policy": "generate", "origin": "python", "code": code}
    spec.update(extra)
    return spec


def _client(tmp_path: Path) -> TestClient:
    return make_client(make_services(tmp_path, iam={"roles": ROLES}))


def _put(c: TestClient, role: str, spec: dict[str, Any], base_etag: str | None = None) -> Any:
    body: dict[str, Any] = {"spec": spec}
    if base_etag:
        body["base_etag"] = base_etag
    return c.put("/api/specs/simple", json=body, headers=as_role(role))


def _etag(c: TestClient) -> str:
    return str(c.get("/api/specs/simple", headers=as_role("Admin")).json()["etag"])


def test_rest_save_of_new_or_changed_code_needs_code_raw(tmp_path: Path) -> None:
    c = _client(tmp_path)
    refused = _put(c, "Shipper", _with_code(CODE))
    assert refused.status_code == 403, refused.text
    assert "code:raw" in refused.text
    assert not (tmp_path / "dags" / "simple.py").exists()
    assert _put(c, "Admin", _with_code(CODE)).status_code == 200
    # the stored code unchanged (another field edited): no code:raw needed
    kept = _put(c, "Shipper", _with_code(CODE, description="edited"), _etag(c))
    assert kept.status_code == 200, kept.text
    changed = _put(c, "Shipper", _with_code(CODE + "# more\n"), _etag(c))
    assert changed.status_code == 403, changed.text
    # removing the code is no new Python
    dropped = _put(c, "Shipper", _with_code(None), _etag(c))
    assert dropped.status_code == 200, dropped.text


def test_code_mode_apply_of_new_code_needs_code_raw(tmp_path: Path) -> None:
    c = _client(tmp_path)
    body = {"source": CODE, "current": _with_code(None)}
    refused = c.post("/api/interchange/python/sync", json=body, headers=as_role("Coder"))
    assert refused.status_code == 403, refused.text
    assert "code:raw" in refused.text
    # re-applying the code the document already carries adds no new Python
    same = {"source": CODE, "current": _with_code(CODE)}
    applied = c.post("/api/interchange/python/sync", json=same, headers=as_role("Coder"))
    assert applied.status_code == 200, applied.text


def test_mcp_save_of_new_code_needs_code_raw(tmp_path: Path) -> None:
    pytest.importorskip("fastmcp")
    c = _client(tmp_path)

    def save(role: str, spec: dict[str, Any]) -> dict[str, Any]:
        resp = c.post(
            "/mcp/",
            headers={
                **as_role(role),
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
                "mcp-protocol-version": "2025-06-18",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "save_dag_spec", "arguments": {"spec": spec, "confirm": True}},
            },
        )
        assert resp.status_code == 200, resp.text
        result = resp.json()["result"]
        assert not result["isError"], result
        out: dict[str, Any] = result["structuredContent"]
        return out

    refused = save("Shipper", _with_code(CODE))
    assert refused["saved"] is False
    assert "code:raw" in refused["message"]
    assert not (tmp_path / "dags" / "simple.py").exists()
    assert save("Admin", _with_code(CODE))["saved"] is True
