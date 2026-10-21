"""MCP tools follow the source policy: preview_dag_source and a save_dag_spec that patches a
hand-written file instead of regenerating it."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import anyio
import pytest

from tests.optional import importorskip_mcp

importorskip_mcp()

from fastmcp import Client  # noqa: E402

from autoair.api.app import build_services  # noqa: E402
from autoair.auth import AuthBackend  # noqa: E402
from autoair.mcp import create_mcp_server  # noqa: E402
from autoair.mcp.iam import TOOL_FEATURES, TOOL_PERMISSIONS  # noqa: E402
from autoair.settings import Settings  # noqa: E402
from tests.integration.test_preserve_api import HAND  # noqa: E402

pytestmark = pytest.mark.airflow


def test_preview_tool_is_classified() -> None:
    assert TOOL_PERMISSIONS["preview_dag_source"] == ("spec:write",)
    assert TOOL_FEATURES["preview_dag_source"] == ("deploy",)


def test_save_patches_the_hand_written_file(tmp_path: Path) -> None:
    services = build_services(Settings(dags_folder=tmp_path, subfolder=""), AuthBackend())
    path = tmp_path / "hand_load.py"

    async def go() -> None:
        async with Client(create_mcp_server(lambda: services)) as client:
            imported = await client.call_tool("import_python_dag", {"source": HAND})
            assert imported.structured_content is not None
            spec: dict[str, Any] = imported.structured_content["dags"][0]["spec"]
            assert spec["source"]["origin"] == "python"
            assert spec["source"]["code"] == HAND
            first = await client.call_tool("save_dag_spec", {"spec": spec, "confirm": True})
            assert first.structured_content is not None
            assert first.structured_content["saved"], first.structured_content
            assert path.read_text() == HAND

            changed = copy.deepcopy(spec)
            orders = next(n for n in changed["nodes"] if n.get("task_id") == "orders")
            orders["retries"] = 9
            preview = await client.call_tool("preview_dag_source", {"spec": changed})
            assert preview.structured_content is not None
            assert preview.structured_content["action"] == "preserve"
            assert (
                "+            retries=9,  # the orders API is flaky"
                in (preview.structured_content["diff"])
            )
            dry = await client.call_tool("save_dag_spec", {"spec": changed})
            assert dry.structured_content is not None
            assert dry.structured_content["source"]["action"] == "preserve"
            assert path.read_text() == HAND
            saved = await client.call_tool("save_dag_spec", {"spec": changed, "confirm": True})
            assert saved.structured_content is not None
            assert saved.structured_content["saved"], saved.structured_content
            code = await client.call_tool("get_dag_code", {"dag_id": "hand_load"})
            assert code.structured_content is not None
            assert "# keep the defaults in one place" in code.structured_content["code"]

    anyio.run(go)
    written = path.read_text()
    assert "retries=9,  # the orders API is flaky" in written
    assert "# keep the defaults in one place" in written
