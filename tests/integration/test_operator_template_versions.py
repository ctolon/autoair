"""Operator templates created and edited without a DAG: version history, etags (If-Match /
If-None-Match, 409 on conflict), owner-team rules, audit, and the MCP tools."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from autoair import sections
from autoair.api.app import build_services, create_app
from autoair.auth import AuthBackend
from autoair.settings import Settings
from autoair.templates import (
    OperatorBlueprint,
    TemplateConflict,
    TemplateError,
    TemplateInput,
    TemplateStore,
    blueprint_problems,
    blueprint_spec,
)
from autoair.validation import Problem
from tests.optional import requires_mcp

pytestmark = pytest.mark.airflow

BASH = "airflow.providers.standard.operators.bash.BashOperator"


@pytest.fixture(autouse=True)
def _reset_sections():
    yield
    sections.reset()


def _node(**over: Any) -> dict[str, Any]:
    node = {
        "id": "n1",
        "kind": "operator",
        "task_id": "notify",
        "operator": BASH,
        "params": {"bash_command": "echo hi", "env": {"A": "1"}},
        "retries": 3,
    }
    node.update(over)
    return node


def _template(**over: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": "notify-step",
        "title": "Notify step",
        "kind": "operator",
        "owner_team": "platform",
        "docs_url": "https://example.com/notify",
        "blueprint": {"node": _node(), "locked": ["retries"], "required": []},
    }
    body.update(over)
    return body


def _services(tmp_path: Path, **settings: Any):
    # AuthBackend(): anonymous callers, who get iam.anonymous_roles (Viewer by default)
    settings.setdefault("iam", {"anonymous_roles": ["Admin"]})
    s = Settings(dags_folder=tmp_path, subfolder="", **settings)  # type: ignore[call-arg]
    return build_services(s, AuthBackend())


def _client(tmp_path: Path, **settings: Any) -> TestClient:
    return TestClient(create_app(_services(tmp_path, **settings)))


# --------------------------------------------------------------------------- store


def test_each_save_is_a_new_version_and_old_ones_stay_readable(tmp_path):
    store = TemplateStore(str(tmp_path / "templates"))
    v1 = store.save(TemplateInput.model_validate(_template()), user="alice")
    assert (v1.version, v1.updated_by) == ("1", "alice")
    assert v1.etag
    assert v1.docs_url == "https://example.com/notify"
    edited = _template(blueprint={"node": _node(retries=5), "locked": ["retries"]})
    v2 = store.save(TemplateInput.model_validate(edited), user="bob", if_match=v1.etag)
    assert v2.version == "2"
    assert v2.etag != v1.etag

    versions = store.versions("notify-step")
    assert versions is not None
    assert [(v.version, v.current, v.updated_by) for v in versions] == [
        ("2", True, "bob"),
        ("1", False, "alice"),
    ]
    old = store.get_version("notify-step", "1")
    assert old is not None
    assert old.blueprint is not None
    assert old.blueprint.node.retries == 3  # type: ignore[union-attr]
    assert store.get_version("notify-step", "9") is None
    assert store.lock("notify-step").locked == {"retries": 5}  # type: ignore[union-attr]


def test_conditional_writes_refuse_stale_etags(tmp_path):
    store = TemplateStore(str(tmp_path / "templates"))
    v1 = store.save(TemplateInput.model_validate(_template()))
    store.save(TemplateInput.model_validate(_template()), if_match=v1.etag)
    with pytest.raises(TemplateConflict) as stale:
        store.save(TemplateInput.model_validate(_template()), if_match=v1.etag)
    assert stale.value.current is not None
    assert stale.value.current.version == "2"
    with pytest.raises(TemplateConflict):
        store.save(TemplateInput.model_validate(_template()), create_only=True)
    with pytest.raises(TemplateConflict):
        store.delete("notify-step", if_match=v1.etag)
    assert store.get("notify-step") is not None


def test_delete_removes_the_history(tmp_path):
    store = TemplateStore(str(tmp_path / "templates"))
    store.save(TemplateInput.model_validate(_template()))
    store.save(TemplateInput.model_validate(_template()))
    current = store.get("notify-step")
    assert current is not None
    deleted = store.delete("notify-step", if_match=current.etag)
    assert deleted
    assert store.versions("notify-step") is None
    store.save(TemplateInput.model_validate(_template()))  # same id again: starts over
    assert [v.version for v in store.versions("notify-step") or []] == ["1"]
    deleted = store.delete("missing")
    assert deleted is False


def test_history_is_bounded_and_keeps_versions_without_history(tmp_path):
    root = tmp_path / "templates"
    root.mkdir()
    # A template file placed by hand (no history entry).
    placed = {**_template(), "version": "7", "builtin": False}
    placed["blueprint"]["node"]["id"] = "template"
    (root / "notify-step.json").write_text(json.dumps(placed))
    store = TemplateStore(str(root), history_limit=3)
    for _ in range(4):
        store.save(TemplateInput.model_validate(_template()))
    assert [v.version for v in store.versions("notify-step") or []] == ["11", "10", "9"]


def test_blueprint_validation_ignores_required_and_dag_level_findings():
    bp = OperatorBlueprint.model_validate(
        {"node": _node(params={}), "required": ["params.bash_command"]}
    )
    spec = blueprint_spec(bp)
    assert [n.id for n in spec.nodes] == ["template"]
    problems = [
        Problem(
            severity="error", code="param", message="x", node_id="template", field="bash_command"
        ),
        Problem(severity="error", code="param", message="y", node_id="template", field="cwd"),
        Problem(severity="warning", code="dag_tags", message="z"),
    ]
    assert [p.message for p in blueprint_problems(problems, bp)] == ["y"]


# --------------------------------------------------------------------------- REST


def test_api_versions_etags_and_conflicts(tmp_path):
    c = _client(tmp_path)
    created = c.post("/api/templates", json=_template(), headers={"If-None-Match": "*"})
    assert created.status_code == 200, created.text
    etag = created.json()["etag"]
    assert created.headers["etag"] == f'"{etag}"'
    taken = c.post("/api/templates", json=_template(), headers={"If-None-Match": "*"})
    assert taken.status_code == 409
    assert taken.json()["detail"]["current_version"] == "1"

    got = c.get("/api/templates/notify-step")
    assert got.json()["etag"] == etag
    assert got.headers["etag"] == f'"{etag}"'
    v2 = c.post("/api/templates", json=_template(), headers={"If-Match": f'"{etag}"'})
    assert v2.status_code == 200
    assert v2.json()["version"] == "2"
    stale = c.post("/api/templates", json=_template(), headers={"If-Match": etag})
    assert stale.status_code == 409
    assert stale.json()["detail"]["current_etag"] == v2.json()["etag"]

    versions = c.get("/api/templates/notify-step/versions").json()
    assert [(v["version"], v["current"]) for v in versions] == [("2", True), ("1", False)]
    assert c.get("/api/templates/notify-step/versions/1").json()["version"] == "1"
    assert c.get("/api/templates/notify-step/versions/5").status_code == 404
    assert c.get("/api/templates/nope/versions").status_code == 404

    assert (
        c.request("DELETE", "/api/templates/notify-step", headers={"If-Match": etag}).status_code
        == 409
    )
    gone = c.request(
        "DELETE", "/api/templates/notify-step", headers={"If-Match": v2.json()["etag"]}
    )
    assert gone.json() == {"deleted": True}
    assert c.get("/api/templates/notify-step/versions").status_code == 404


def test_api_audits_create_update_delete(tmp_path):
    services = _services(tmp_path)
    c = TestClient(create_app(services))
    c.post("/api/templates", json=_template())
    c.post("/api/templates", json=_template())
    c.delete("/api/templates/notify-step")
    records = [r for r in services.audit.query(limit=10) if r.action.startswith("template_")]
    details = [
        (r.action, r.details.get("version"), r.details.get("created")) for r in reversed(records)
    ]
    assert details == [
        ("template_save", "1", True),
        ("template_save", "2", False),
        ("template_delete", "2", None),
    ]


def _iam_client(tmp_path: Path, **settings: Any) -> TestClient:
    import httpx2

    from autoair.auth import TokenVerifier, bearer_passthrough
    from autoair.iam.config import IamSettings

    airflow = "http://airflow.test"
    s = Settings(
        dags_folder=tmp_path / "dags",
        subfolder="",
        airflow_url=airflow,
        ai={"enabled": False},
        iam=IamSettings(strategies=["role_mapping"]),
        **settings,
    )
    verifier = TokenVerifier(
        airflow,
        transport=httpx2.MockTransport(
            lambda r: httpx2.Response(200, json={"dags": [], "total_entries": 0})
        ),
    )
    return TestClient(
        create_app(build_services(s, bearer_passthrough(verifier))), raise_server_exceptions=False
    )


def _as(role: str) -> dict[str, str]:
    import base64
    import time

    def enc(o: Any) -> str:
        return base64.urlsafe_b64encode(json.dumps(o).encode()).decode().rstrip("=")

    claims = {"sub": role.lower(), "role": role, "exp": time.time() + 3600}
    return {"Authorization": f"Bearer {enc({'alg': 'HS512'})}.{enc(claims)}.sig"}


def test_template_write_and_owner_team_rules(tmp_path):
    c = _iam_client(tmp_path, team="data")
    viewer, manager, admin = _as("Viewer"), _as("Op"), _as("Admin")
    assert c.post("/api/templates", json=_template(), headers=viewer).status_code == 403
    assert c.get("/api/templates", headers=viewer).status_code == 200

    # TemplateManager: its own team's and unowned templates...
    ours = _template(id="ours", owner_team="data")
    assert c.post("/api/templates", json=ours, headers=manager).status_code == 200
    shared = _template(id="shared", owner_team=None)
    assert c.post("/api/templates", json=shared, headers=manager).status_code == 200
    # ...but not another team's (create, update or delete).
    theirs = c.post("/api/templates", json=_template(), headers=manager)
    assert theirs.status_code == 403
    assert "platform" in theirs.json()["detail"]
    assert c.post("/api/templates", json=_template(), headers=admin).status_code == 200
    assert c.post("/api/templates", json=_template(), headers=manager).status_code == 403
    takeover = _template(owner_team="data")  # re-owning it is a change of their template
    assert c.post("/api/templates", json=takeover, headers=manager).status_code == 403
    response = c.delete("/api/templates/notify-step", headers=manager)
    assert response.status_code == 403
    response = c.delete("/api/admin/templates/notify-step", headers=manager)
    assert response.status_code == 403
    response = c.delete("/api/templates/notify-step", headers=admin)
    assert response.json() == {"deleted": True}


@requires_mcp
def test_mcp_tools_create_version_and_delete(tmp_path):
    from fastmcp import Client

    from autoair.mcp.server import create_mcp_server

    services = _services(tmp_path, ai={"enabled": False})
    mcp = create_mcp_server(services)
    body = _template()

    async def run() -> None:
        async with Client(mcp) as client:
            dry = (await client.call_tool("save_operator_template", {"template": body})).data
            assert dry.dry_run
            assert not dry.saved
            assert services.templates.get("notify-step") is None

            bad = _template(blueprint={"node": _node(params={"bash_command": "x", "nope": 1})})
            refused = await client.call_tool(
                "save_operator_template", {"template": bad, "confirm": True}
            )
            assert not refused.data.saved
            assert any(p.severity == "error" for p in refused.data.problems)

            saved = await client.call_tool(
                "save_operator_template", {"template": body, "confirm": True}
            )
            assert saved.data.saved
            assert saved.data.template.version == "1"

            # An existing id needs the etag (no silent overwrite).
            again = await client.call_tool(
                "save_operator_template",
                {"template": body, "confirm": True},
                raise_on_error=False,
            )
            assert again.is_error
            assert "if_match" in again.content[0].text

            detail = (
                await client.call_tool("get_operator_template", {"template_id": "notify-step"})
            ).data
            v2 = await client.call_tool(
                "save_operator_template",
                {"template": body, "if_match": detail.template.etag, "confirm": True},
            )
            assert v2.data.template.version == "2"

            listed = (await client.call_tool("list_operator_templates", {"query": "notify"})).data
            assert [t.id for t in listed.templates] == ["notify-step"]
            old = await client.call_tool(
                "get_operator_template", {"template_id": "notify-step", "version": "1"}
            )
            assert old.data.template.version == "1"
            assert [v.version for v in old.data.versions] == ["2", "1"]

            gone = await client.call_tool(
                "delete_operator_template", {"template_id": "notify-step", "confirm": True}
            )
            assert gone.data.deleted

    asyncio.run(run())
    actions = [r.action for r in services.audit.query(limit=10)]
    assert actions.count("template_save") == 2
    assert "template_delete" in actions


@requires_mcp
def test_mcp_template_tools_follow_template_write(tmp_path):
    c = _iam_client(tmp_path)
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "mcp-protocol-version": "2025-06-18",
    }

    def call(role: str, name: str, args: dict[str, Any]) -> Any:
        resp = c.post(
            "/mcp/",
            headers={**headers, **_as(role)},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": args},
            },
        )
        assert resp.status_code == 200, resp.text
        return resp.json()["result"]

    args = {"template": _template(), "confirm": True}
    denied = call("Viewer", "save_operator_template", args)
    assert denied["isError"]
    assert "template:write" in denied["content"][0]["text"]
    assert not call("Viewer", "list_operator_templates", {})["isError"]
    assert not call("Op", "save_operator_template", args)["isError"]
    assert call("Viewer", "delete_operator_template", {"template_id": "notify-step"})["isError"]


def test_published_versions_are_immutable(tmp_path):
    store = TemplateStore(str(tmp_path / "templates"))
    store.save(TemplateInput.model_validate(_template(version="2025.1")))
    store.save(TemplateInput.model_validate(_template()))  # non-numeric before: restarts at 1
    with pytest.raises(TemplateError, match="exists already"):
        store.save(TemplateInput.model_validate(_template(version="2025.1")))
    with pytest.raises(TemplateError, match="exists already"):
        store.save(TemplateInput.model_validate(_template(version="1")))
    assert [v.version for v in store.versions("notify-step") or []] == ["1", "2025.1"]
