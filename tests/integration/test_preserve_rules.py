"""Permission and safety rules of source-preserving deploys through the API and MCP, against
a real Airflow importer: Deployer limits, deleting kept files, binding a deploy to its
preview, four-eyes proposals, admin switches at runtime, policy checks over MCP."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import anyio
import pytest
from fastapi.testclient import TestClient

from autoair.importer.python_dag import import_python_sources
from tests.integration.test_iam_api import as_role
from tests.integration.test_preserve_api import HAND, client, deploy, imported, task_node

pytestmark = pytest.mark.airflow

WRITER = {"Writer": ["builder:use", "spec:*", "deploy", "code:execute", "code:raw"]}


def _kept(c: TestClient) -> dict[str, Any]:
    """An imported hand-written DAG deployed once (its file is kept as written)."""
    spec = imported(c)
    res = deploy(c, spec).json()
    assert res["deployed"], res
    return c.get("/api/specs/hand_load", headers=as_role("Admin")).json()


def test_deployer_cannot_deploy_an_imported_python_dag(tmp_path: Path) -> None:
    """Documented: preserving needs code:execute (round trip) and, for the imported file
    itself, code:raw -- neither is in the Deployer role."""
    c = client(tmp_path)
    spec = imported(c)
    res = deploy(c, spec, "Deployer").json()
    assert res["status"] == "source_refused"
    assert "code:execute" in res["message"]
    assert "CodeDeployer" in res["message"]  # the role that can
    assert (tmp_path / "dags" / "hand_load.py").exists() is False
    generated = copy.deepcopy(spec)
    generated["source"]["policy"] = "generate"  # the other documented way out
    assert deploy(c, generated, "Deployer").json()["deployed"]


def test_code_deployer_deploys_an_imported_python_dag(tmp_path: Path) -> None:
    c = client(tmp_path)
    spec = imported(c)
    res = deploy(c, spec, "CodeDeployer").json()
    assert res["deployed"], res
    assert res["source"]["policy"] == "preserve"
    assert (tmp_path / "dags" / "hand_load.py").read_text() == HAND


def test_kept_hand_written_file_needs_the_permission_to_regenerate_or_delete(
    tmp_path: Path,
) -> None:
    c = client(tmp_path, iam={"roles": WRITER}, preserve={"allow_force_overwrite": True})
    stored = _kept(c)
    path = tmp_path / "dags" / "hand_load.py"
    spec = copy.deepcopy(stored["spec"])
    spec["source"]["policy"] = "generate"
    blocked = deploy(c, spec, "Writer", base_etag=stored["etag"]).json()
    assert blocked["status"] == "ejected"
    assert blocked["source"]["target_state"] == "hand_written"
    denied = deploy(c, spec, "Writer", base_etag=stored["etag"], source_resolution="overwrite")
    assert "deploy:overwrite_ejected" in denied.json()["message"]
    assert path.read_text() == HAND
    gone = c.delete("/api/specs/hand_load", headers=as_role("Writer"))
    assert gone.status_code == 409
    assert "deploy:overwrite_ejected" in gone.text
    assert path.read_text() == HAND
    regenerated = deploy(c, spec, base_etag=stored["etag"], source_resolution="overwrite").json()
    assert regenerated["deployed"], regenerated  # Admin holds the permission
    assert "# keep the defaults in one place" not in path.read_text()


def test_kept_hand_written_file_is_not_regenerated_without_allow_force_overwrite(
    tmp_path: Path,
) -> None:
    """Default preserve.allow_force_overwrite=false: no overwrite choice, also for an admin
    who holds deploy:overwrite_ejected."""
    c = client(tmp_path)
    stored = _kept(c)
    path = tmp_path / "dags" / "hand_load.py"
    spec = copy.deepcopy(stored["spec"])
    spec["source"]["policy"] = "generate"
    blocked = deploy(c, spec, base_etag=stored["etag"]).json()
    assert blocked["status"] == "ejected"
    assert "overwrite" not in blocked["source"]["options"]
    refused = deploy(c, spec, base_etag=stored["etag"], source_resolution="overwrite").json()
    assert not refused["deployed"]
    assert "preserve.allow_force_overwrite" in refused["message"]
    assert path.read_text() == HAND


def test_admin_can_delete_the_kept_file(tmp_path: Path) -> None:
    c = client(tmp_path)
    _kept(c)
    assert c.delete("/api/specs/hand_load", headers=as_role("Admin")).status_code == 200
    assert not (tmp_path / "dags" / "hand_load.py").exists()


def test_deploy_is_bound_to_the_previewed_file(tmp_path: Path) -> None:
    c = client(tmp_path)
    stored = _kept(c)
    changed = copy.deepcopy(stored["spec"])
    task_node(changed, "orders")["retries"] = 4
    preview = c.post(
        "/api/specs/hand_load/source-preview", json={"spec": changed}, headers=as_role("Admin")
    ).json()
    sha = preview["target_sha"]
    path = tmp_path / "dags" / "hand_load.py"
    path.write_text(HAND + "# changed meanwhile\n")
    stale = deploy(c, changed, base_etag=stored["etag"], source_target_sha=sha).json()
    assert stale["status"] == "source_refused"
    assert "changed since you previewed" in stale["message"]
    assert path.read_text().endswith("# changed meanwhile\n")


def test_proposals_check_code_raw(tmp_path: Path) -> None:
    roles = {"Proposer": ["builder:use", "spec:*", "deploy", "code:execute"]}
    c = client(tmp_path, iam={"roles": roles, "require_approval": True})
    spec = imported(c)
    res = deploy(c, spec, "Proposer")
    assert res.json()["status"] == "source_refused"
    assert "code:raw" in res.json()["message"]


def test_admin_switch_turns_preserve_off_at_runtime(tmp_path: Path) -> None:
    c = client(tmp_path)
    spec = imported(c)
    spec["source"]["policy"] = "preserve"
    off = c.put(
        "/api/admin/features/source_preserve",
        json={"value": False, "reason": "test"},
        headers=as_role("Admin"),
    )
    assert off.status_code == 200, off.text
    res = deploy(c, spec).json()
    assert res["status"] == "source_refused"
    assert "features.source_preserve" in res["message"]


def test_batch_imports_give_one_result_per_file() -> None:
    other = HAND.replace('dag_id="hand_load"', 'dag_id="other_load"')
    results = import_python_sources(
        [(HAND, "a.py"), (other, "b.py"), ("raise SystemExit(1)\n", "c.py")]
    )
    assert [d.dag_id for d in results[0].dags] == ["hand_load"]
    assert [d.dag_id for d in results[1].dags] == ["other_load"]
    assert results[2].import_errors or not results[2].dags


# A forbidden operator in the hand-written part the spec does not model (never called at
# parse time, so the imported spec does not have it).
SNEAKY = HAND.replace(
    "# keep the defaults in one place",
    "def _later():\n"
    "    from airflow.providers.common.sql.operators.sql import SQLExecuteQueryOperator\n\n"
    '    return SQLExecuteQueryOperator(task_id="x", sql="drop table users")\n\n\n'
    "# keep the defaults in one place",
)


def test_mcp_save_applies_policy_rules_to_the_kept_file(tmp_path: Path) -> None:
    from tests.optional import importorskip_mcp

    importorskip_mcp()
    from fastmcp import Client

    from autoair.api.app import build_services
    from autoair.auth import AuthBackend
    from autoair.mcp import create_mcp_server
    from autoair.settings import Settings

    services = build_services(
        Settings(
            dags_folder=tmp_path,
            subfolder="",
            forbidden_operators=["*.SQLExecuteQueryOperator"],
        ),
        AuthBackend(),
    )

    async def go() -> None:
        async with Client(create_mcp_server(lambda: services)) as mcp:
            imported_dag = await mcp.call_tool("import_python_dag", {"source": SNEAKY})
            assert imported_dag.structured_content is not None
            spec = imported_dag.structured_content["dags"][0]["spec"]
            saved = await mcp.call_tool("save_dag_spec", {"spec": spec, "confirm": True})
            assert saved.structured_content is not None
            assert saved.structured_content["saved"] is False
            problems = saved.structured_content["problems"]
            assert any("forbidden" in p["message"] for p in problems), problems

    anyio.run(go)
    assert not (tmp_path / "hand_load.py").exists()
