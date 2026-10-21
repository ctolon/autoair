"""The hand-written DAG file AutoAir keeps stays protected on every write path: an adoption
over MCP records it like the REST deploy does, and deploys that skip the source plan (other
output modes, raw code) cannot replace it without ``deploy:overwrite_ejected``. Also: the MCP
save validates a preserved file with the same inputs as the REST deploy."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import anyio
import pytest

from tests.integration.test_iam_api import as_role, make_client, make_services
from tests.integration.test_preserve_api import HAND, deploy, imported
from tests.integration.test_preserve_rules import WRITER, _kept
from tests.optional import importorskip_mcp

pytestmark = pytest.mark.airflow


async def _mcp_adopt(services: Any) -> dict[str, Any]:
    importorskip_mcp()
    from fastmcp import Client

    from autoair.mcp import create_mcp_server

    async with Client(create_mcp_server(lambda: services)) as client:
        res = await client.call_tool("import_python_dag", {"source": HAND})
        assert res.structured_content is not None
        spec: dict[str, Any] = res.structured_content["dags"][0]["spec"]
        saved = await client.call_tool("save_dag_spec", {"spec": spec, "confirm": True})
        assert saved.structured_content is not None
        assert saved.structured_content["saved"], saved.structured_content
        return spec


def test_mcp_adoption_keeps_the_file_protected(tmp_path: Path) -> None:
    services = make_services(tmp_path, import_check_on_deploy=True, iam={"roles": WRITER})
    c = make_client(services)
    anyio.run(_mcp_adopt, services)
    path = tmp_path / "dags" / "hand_load.py"
    assert path.read_text() == HAND
    current = services.target.current_file("hand_load")
    assert current is not None
    assert current.hand_written  # recorded like a REST preserve deploy
    stored = c.get("/api/specs/hand_load", headers=as_role("Admin")).json()
    spec = copy.deepcopy(stored["spec"])
    spec["source"]["policy"] = "generate"
    blocked = deploy(c, spec, "Writer", base_etag=stored["etag"]).json()
    assert blocked["status"] == "ejected", blocked
    assert blocked["source"]["target_state"] == "hand_written"
    gone = c.delete("/api/specs/hand_load", headers=as_role("Writer"))
    assert gone.status_code == 409
    assert "deploy:overwrite_ejected" in gone.text
    assert path.read_text() == HAND


@pytest.mark.parametrize("output_mode", ["yaml", "loader"])
def test_other_output_modes_cannot_replace_the_kept_file(tmp_path: Path, output_mode: str) -> None:
    c = make_client(make_services(tmp_path, import_check_on_deploy=True, iam={"roles": WRITER}))
    stored = _kept(c)
    path = tmp_path / "dags" / "hand_load.py"
    spec = copy.deepcopy(stored["spec"])
    spec["output_mode"] = output_mode
    if output_mode == "yaml":  # YAML Dags take operators only: drop the TaskFlow task
        dropped = {n["id"] for n in spec["nodes"] if n.get("kind") == "python_task"}
        spec["nodes"] = [n for n in spec["nodes"] if n["id"] not in dropped]
        spec["edges"] = [
            e for e in spec["edges"] if e["source"] not in dropped and e["target"] not in dropped
        ]
    res = deploy(c, spec, "Writer", base_etag=stored["etag"])
    assert res.status_code == 200, res.text
    body = res.json()
    # reported like an ejected file, without the force choice (switch off by default)
    assert body["status"] == "ejected", body
    assert body["conflict"]["hand_written"] is True
    assert body["conflict"]["force_allowed"] is False
    assert "never overwrites a file it did not create" in body["message"]
    assert path.read_text() == HAND
    assert not (tmp_path / "dags" / "hand_load.yaml").exists()
    # force without the switch and the permission is dropped, so it does not help either
    forced = deploy(c, spec, "Writer", base_etag=stored["etag"], force=True).json()
    assert forced["status"] == "ejected", forced
    assert path.read_text() == HAND
    admin = deploy(c, spec, base_etag=stored["etag"], force=True).json()
    assert admin["status"] == "ejected", admin  # the admin holds the permission, not the switch
    assert path.read_text() == HAND


def _switch_on(tmp_path: Path) -> Any:
    return make_client(
        make_services(
            tmp_path,
            import_check_on_deploy=True,
            iam={"roles": WRITER},
            preserve={"allow_force_overwrite": True},
        )
    )


def test_force_never_replaces_a_kept_file_without_the_marker(tmp_path: Path) -> None:
    """A kept file written by a source-preserving deploy has no AutoAir marker: force is
    not offered for it even with the switch and the permission."""
    c = _switch_on(tmp_path)
    stored = _kept(c)
    spec = copy.deepcopy(stored["spec"])
    spec["output_mode"] = "loader"
    admin = deploy(c, spec, base_etag=stored["etag"]).json()
    assert admin["status"] == "ejected", admin
    assert admin["conflict"]["hand_written"] is True
    assert admin["conflict"]["force_allowed"] is False
    assert "never overwrites a file it did not create" in admin["message"]
    assert (tmp_path / "dags" / "hand_load.py").read_text() == HAND


def test_force_replaces_a_kept_raw_code_file_with_switch_and_permission(tmp_path: Path) -> None:
    c = _switch_on(tmp_path)
    path = tmp_path / "dags" / "hand_load.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(HAND)
    raw = {"dag_id": "hand_load", "raw_code": HAND, "nodes": [], "edges": []}
    first = deploy(c, raw, "Writer").json()  # taken over as a raw code DAG: kept
    assert first["deployed"], first
    taken = path.read_text()
    visual = imported(c)
    visual["output_mode"] = "loader"
    visual.pop("source", None)
    writer = deploy(c, visual, "Writer", base_etag=first["etag"]).json()
    assert writer["status"] == "ejected", writer
    assert writer["conflict"]["hand_written"] is True
    assert writer["conflict"]["force_allowed"] is False  # no deploy:overwrite_ejected
    assert "deploy:overwrite_ejected" in writer["message"]
    admin = deploy(c, visual, base_etag=first["etag"]).json()
    assert admin["status"] == "ejected", admin
    assert admin["conflict"]["force_allowed"] is True
    forced = deploy(c, visual, base_etag=first["etag"], force=True).json()
    assert forced.get("deployed"), forced
    assert path.read_text() != taken


def test_converting_to_raw_code_cannot_replace_the_kept_file(tmp_path: Path) -> None:
    c = make_client(make_services(tmp_path, import_check_on_deploy=True, iam={"roles": WRITER}))
    stored = _kept(c)
    raw = {
        "dag_id": "hand_load",
        "raw_code": HAND.replace("echo users", "echo replaced"),
        "nodes": [],
        "edges": [],
    }
    res = deploy(c, raw, "Writer", base_etag=stored["etag"]).json()
    assert res["status"] == "ejected", res
    assert res["conflict"]["hand_written"] is True
    assert not res["conflict"]["force_allowed"]
    assert (tmp_path / "dags" / "hand_load.py").read_text() == HAND


def test_a_raw_code_dag_still_edits_its_own_code(tmp_path: Path) -> None:
    """A file taken over as a raw code DAG is kept, but the raw code DAG replaces its own
    code (the editor shows all of it) without deploy:overwrite_ejected."""
    c = make_client(make_services(tmp_path, import_check_on_deploy=True, iam={"roles": WRITER}))
    path = tmp_path / "dags" / "hand_load.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(HAND)
    raw = {"dag_id": "hand_load", "raw_code": HAND, "nodes": [], "edges": []}
    first = deploy(c, raw, "Writer").json()
    assert first["deployed"], first
    edited = {**raw, "raw_code": HAND.replace("echo users", "echo edited")}
    second = deploy(c, edited, "Writer", base_etag=first["etag"]).json()
    assert second["deployed"], second
    assert "echo edited" in path.read_text()


async def _mcp_save_raw(services: Any, code: str) -> dict[str, Any]:
    importorskip_mcp()
    from fastmcp import Client

    from autoair.mcp import create_mcp_server

    async with Client(create_mcp_server(lambda: services)) as client:
        res = await client.call_tool(
            "save_raw_dag_code",
            {"dag_id": "hand_load", "code": code, "confirm": True},
            raise_on_error=False,
        )
        if res.is_error:
            return {"saved": False, "error": str(res.content)}
        assert res.structured_content is not None
        return res.structured_content


def test_mcp_raw_code_dag_edits_its_own_code(tmp_path: Path) -> None:
    """Like the REST deploy: a raw code DAG that took over a hand-written file replaces its
    own code over MCP without deploy:overwrite_ejected, while the file is unchanged."""
    services = make_services(tmp_path, import_check_on_deploy=True, iam={"roles": WRITER})
    c = make_client(services)
    path = tmp_path / "dags" / "hand_load.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(HAND)
    raw = {"dag_id": "hand_load", "raw_code": HAND, "nodes": [], "edges": []}
    first = deploy(c, raw, "Writer").json()
    assert first["deployed"], first
    current = services.target.current_file("hand_load")
    assert current is not None
    assert current.hand_written
    saved = anyio.run(_mcp_save_raw, services, HAND.replace("echo users", "echo mcp"))
    assert saved["saved"], saved
    assert "echo mcp" in path.read_text()
    after = services.target.current_file("hand_load")
    assert after is not None
    assert after.hand_written  # still kept as hand-written
    # changed outside AutoAir since: MCP has no force, so the file stays as it is
    path.write_text(path.read_text() + "# by hand\n")
    refused = anyio.run(_mcp_save_raw, services, HAND.replace("echo users", "echo again"))
    assert not refused["saved"], refused
    assert path.read_text().endswith("# by hand\n")


def test_mcp_raw_code_cannot_replace_a_kept_visual_dag_file(tmp_path: Path) -> None:
    """Only a raw code DAG edits its own code: a kept file of a visual DAG is not replaced."""
    services = make_services(tmp_path, import_check_on_deploy=True, iam={"roles": WRITER})
    c = make_client(services)
    _kept(c)
    refused = anyio.run(_mcp_save_raw, services, HAND.replace("echo users", "echo mcp"))
    assert not refused["saved"], refused
    assert (tmp_path / "dags" / "hand_load.py").read_text() == HAND


def test_mcp_preserved_file_is_validated_like_the_rest_deploy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import autoair.validation as validation

    services = make_services(tmp_path, import_check_on_deploy=True)
    original = validation.validate_spec
    seen: list[dict[str, Any]] = []

    def spy(spec: Any, **kwargs: Any) -> Any:
        if kwargs.get("source") is not None:
            seen.append(kwargs)
        return original(spec, **kwargs)

    monkeypatch.setattr(validation, "validate_spec", spy)
    c = make_client(services)
    spec = imported(c)
    anyio.run(_mcp_adopt, services)
    assert seen, "the preserved file was not validated"
    kwargs = seen[-1]
    assert kwargs["templates"] == services.templates.lock
    assert kwargs["target_version"] == services.settings.target_version_tuple()
    assert kwargs["context"]["action"] == "deploy"
    assert "user" in kwargs["context"]
    assert "environment" in kwargs
    assert spec["dag_id"] == "hand_load"
