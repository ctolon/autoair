"""Operator templates (blueprints): saving, versions, locked/required fields enforced by
validation through the node's ``template_ref``; specs without it are unaffected."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from autoair import sections
from autoair.api.app import build_services, create_app
from autoair.auth import AuthBackend
from autoair.settings import Settings
from autoair.spec import DagSpec
from autoair.templates import OperatorBlueprint, TemplateInput, TemplateStore
from autoair.validation import template_problems

pytestmark = pytest.mark.airflow

BASH = "airflow.providers.standard.operators.bash.BashOperator"


@pytest.fixture(autouse=True)
def _reset_sections():
    yield
    sections.reset()


def client(tmp_path: Path) -> TestClient:
    # AuthBackend(): anonymous callers, who get iam.anonymous_roles (Viewer by default)
    s = Settings(dags_folder=tmp_path, subfolder="", iam={"anonymous_roles": ["Admin"]})  # type: ignore[call-arg]
    return TestClient(create_app(build_services(s, AuthBackend())))


def _node(**over: Any) -> dict[str, Any]:
    node = {
        "id": "n1",
        "kind": "operator",
        "task_id": "notify",
        "operator": BASH,
        "params": {"bash_command": "echo hi", "env": {"A": "1"}},
        "retries": 3,
        "pool": "slack",
        "position": {"x": 40, "y": 80},
    }
    node.update(over)
    return node


def _template(**over: Any) -> dict[str, Any]:
    body = {
        "id": "notify-step",
        "title": "Notify step",
        "kind": "operator",
        "description": "Platform-approved notification",
        "owner_team": "platform",
        "tags": ["notify"],
        "icon": "builtin:slack",
        "blueprint": {
            "node": _node(),
            "locked": ["retries", "params.env"],
            "required": ["params.bash_command"],
        },
    }
    body.update(over)
    return body


def _spec(*nodes: dict[str, Any]) -> DagSpec:
    return DagSpec.model_validate({"dag_id": "d", "nodes": list(nodes), "edges": []})


def test_save_operator_template_bumps_version_and_strips_identity(tmp_path):
    c = client(tmp_path)
    first = c.post("/api/templates", json=_template())
    assert first.status_code == 200, first.text
    tpl = first.json()
    assert tpl["version"] == "1"
    assert tpl["operator_class"] == BASH
    assert tpl["blueprint"]["node"]["id"] == "template"
    assert tpl["blueprint"]["node"].get("template_ref") is None
    position = tpl["blueprint"]["node"]["position"]
    assert (position["x"], position["y"]) == (0, 0)
    assert c.post("/api/templates", json=_template()).json()["version"] == "2"
    assert c.post("/api/templates", json=_template(version="2025.1")).json()["version"] == "2025.1"

    listed = {t["id"]: t for t in c.get("/api/templates").json()}["notify-step"]
    assert listed["kind"] == "operator"
    assert listed["owner_team"] == "platform"
    assert listed["icon"] == "builtin:slack"
    assert listed["operator_class"] == BASH


def test_operator_template_needs_a_blueprint(tmp_path):
    c = client(tmp_path)
    body = _template()
    del body["blueprint"]
    assert c.post("/api/templates", json=body).status_code == 400
    # …and a fragment cannot carry one.
    frag = _template(kind="fragment", fragment={"nodes": [_node()]})
    assert c.post("/api/templates", json=frag).status_code == 400


@pytest.mark.parametrize(
    ("locked", "required"),
    [
        (["task_id"], []),  # identity is per task
        (["nope"], []),  # not a field
        (["retries.x"], []),  # retries has no entries
        (["retries"], ["retries"]),  # both
    ],
)
def test_blueprint_paths_are_checked(locked, required):
    with pytest.raises(ValidationError):
        OperatorBlueprint.model_validate({"node": _node(), "locked": locked, "required": required})


def _store(tmp_path: Path) -> TemplateStore:
    store = TemplateStore(str(tmp_path / "templates"))
    store.save(TemplateInput.model_validate(_template()))
    return store


def test_locked_values_are_enforced(tmp_path):
    store = _store(tmp_path)
    ref = {"id": "notify-step", "version": "1"}
    ok = _spec(_node(template_ref=ref))
    assert template_problems(ok, store.lock) == []

    changed = _spec(_node(template_ref=ref, retries=0, params={"bash_command": "x", "env": {}}))
    problems = template_problems(changed, store.lock)
    assert {(p.code, p.field, p.severity) for p in problems} == {
        ("template_locked", "retries", "error"),
        ("template_locked", "env", "error"),
    }

    other_type = _spec(_node(template_ref=ref, operator="a.b.EmptyOperator"))
    assert [p.code for p in template_problems(other_type, store.lock)] == ["template_locked"]


def test_required_inputs_and_updates(tmp_path):
    store = _store(tmp_path)
    empty = _spec(
        _node(template_ref={"id": "notify-step", "version": "1"}, params={"env": {"A": "1"}})
    )
    problems = template_problems(empty, store.lock)
    assert [(p.code, p.field) for p in problems] == [("template_required", "bash_command")]

    store.save(TemplateInput.model_validate(_template()))  # v2
    stale = _spec(_node(template_ref={"id": "notify-step", "version": "1"}))
    assert [(p.code, p.severity) for p in template_problems(stale, store.lock)] == [
        ("template_updated", "info")
    ]


def test_missing_template_is_a_warning(tmp_path):
    store = TemplateStore(str(tmp_path / "templates"))
    spec = _spec(_node(template_ref={"id": "gone"}))
    assert [(p.code, p.severity) for p in template_problems(spec, store.lock)] == [
        ("template_missing", "warning")
    ]


def test_validate_endpoint_reports_locked_fields(tmp_path):
    c = client(tmp_path)
    assert c.post("/api/templates", json=_template()).status_code == 200
    ref = {"id": "notify-step", "version": "1"}
    spec = {"dag_id": "d", "nodes": [_node(template_ref=ref, retries=1)], "edges": []}
    report = c.post("/api/validate", json=spec).json()
    locked = [p for p in report["problems"] if p["code"] == "template_locked"]
    assert [(p["node_id"], p["field"]) for p in locked] == [("n1", "retries")]
    assert report["ok"] is False


def test_specs_without_template_ref_are_unchanged(tmp_path):
    """The field is optional and not written when absent."""
    spec = _spec(_node())
    assert spec.nodes[0].template_ref is None  # type: ignore[union-attr]
    dumped = spec.model_dump(mode="json", exclude_none=True)
    assert "template_ref" not in dumped["nodes"][0]
    c = client(tmp_path)
    report = c.post("/api/validate", json=dumped).json()
    assert not [p for p in report["problems"] if p["code"].startswith("template_")]


def test_template_ref_is_not_rendered(tmp_path):
    from autoair.codegen import render

    with_ref = _spec(_node(template_ref={"id": "notify-step", "version": "1"}))
    assert render(with_ref, fmt=False) == render(_spec(_node()), fmt=False)
