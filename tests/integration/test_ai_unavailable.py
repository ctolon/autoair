"""AutoAir without the ``autoair[ai]`` extra (no pydantic-ai).

The checks run in a fresh interpreter in which ``pydantic_ai`` cannot be imported
(``sys.modules["pydantic_ai"] = None``), so they work both in the development venv and in
an environment where pydantic-ai is really absent (``nox -s noai``). This module itself
imports neither pydantic-ai nor httpx.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

SCRIPT = textwrap.dedent(
    r"""
    import asyncio, json, os, sys
    sys.modules["pydantic_ai"] = None  # unimportable, like a missing extra

    os.environ.update(AUTOAIR_AI__PROVIDER="anthropic", AUTOAIR_AI__API_KEY="sk-test")
    out = {}

    import autoair.settings
    from autoair.ai.availability import ai_runtime_available
    from autoair.ai.llm import build_llm
    from autoair.api.app import build_services, create_app
    from autoair.auth import AuthBackend
    from autoair.mcp.server import create_mcp_server
    from autoair.settings import Settings
    from fastapi.testclient import TestClient

    out["available"] = ai_runtime_available()
    settings = Settings(dags_folder=sys.argv[1], subfolder="")
    out["build_llm"] = build_llm(settings) is None
    services = build_services(settings, AuthBackend())
    client = TestClient(create_app(services))
    meta = client.get("/api/meta").json()
    out["meta"] = {k: meta.get(k) for k in ("ai_enabled", "ai_unavailable_reason")}
    status = client.get("/api/ai/status")
    out["status"] = [status.status_code, status.json()]
    gen = client.post("/api/ai/generate", json={"prompt": "hello"})
    out["generate"] = gen.status_code
    review = client.post("/api/ai/review", json={"spec": {"dag_id": "d", "nodes": []}})
    out["review"] = review.status_code
    # the deterministic checks stay available without the AI runtime
    leaky = {
        "dag_id": "d",
        "nodes": [{
            "kind": "operator", "id": "t", "task_id": "t",
            "operator": "airflow.providers.standard.operators.bash.BashOperator",
            "params": {"bash_command": "curl -H 'Authorization: Bearer ghp_" + "a" * 36 + "' x"},
        }],
    }
    rules = client.post("/api/review/rules", json={"spec": leaky})
    out["review_rules"] = [rules.status_code, rules.json().get("kind"), rules.json().get("ai_used")]
    secrets = client.post("/api/lint/secrets", json={"spec": leaky})
    out["secrets"] = [
        secrets.status_code,
        sorted({f["category"] for f in secrets.json().get("findings", [])}),
    ]

    mcp = create_mcp_server(services)
    out["mcp_tools"] = sorted(t.name for t in asyncio.run(mcp.list_tools()))

    async def mcp_scan():
        from fastmcp import Client

        async with Client(mcp) as c:
            res = await c.call_tool("scan_dag_leaks", {"spec": leaky}, raise_on_error=False)
            ai = await c.call_tool(
                "review_dag_spec", {"spec": leaky, "use_ai": True}, raise_on_error=False
            )
            return [res.is_error, len((res.structured_content or {}).get("findings", [])),
                    ai.is_error]

    out["mcp_scan"] = asyncio.run(mcp_scan())

    from typer.testing import CliRunner
    from autoair.cli import app as cli

    result = CliRunner().invoke(cli, ["ai-serve"])
    out["ai_serve"] = [result.exit_code, result.output + str(getattr(result, "stderr", ""))]

    out["pydantic_ai_loaded"] = sorted(
        m for m in sys.modules if m.startswith("pydantic_ai.") or m == "autoair.ai.models"
    )
    print("RESULT" + json.dumps(out))
    """
)


def _run(tmp_path: Path) -> dict:  # type: ignore[type-arg]
    script = tmp_path / "noai.py"
    script.write_text(SCRIPT)
    state = tmp_path / "state"
    proc = subprocess.run(
        [sys.executable, str(script), str(tmp_path / "dags")],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        env={
            **os.environ,
            "AUTOAIR_STATE_DIR": str(state),
            "PYTHONWARNINGS": "ignore",
        },
    )
    line = next((x for x in proc.stdout.splitlines() if x.startswith("RESULT")), None)
    assert line is not None, proc.stdout[-3000:] + proc.stderr[-5000:]
    return json.loads(line[len("RESULT") :])  # type: ignore[no-any-return]


def test_autoair_runs_with_ai_off_without_the_extra(tmp_path: Path) -> None:
    (tmp_path / "dags").mkdir()
    out = _run(tmp_path)
    assert out["available"] is False
    assert out["build_llm"] is True  # no model without the runtime
    assert out["meta"]["ai_enabled"] is False
    assert "autoair[ai]" in out["meta"]["ai_unavailable_reason"]
    code, status = out["status"]
    assert code == 200
    assert status["enabled"] is False
    assert "autoair[ai]" in status["reason"]
    assert out["generate"] == 404  # the AI routers are not mounted
    assert out["review"] == 404
    assert out["review_rules"] == [200, "review", False]
    code, categories = out["secrets"]
    assert code == 200
    assert categories  # the hard-coded token is found
    tools = set(out["mcp_tools"])
    assert {"generate_dag_from_prompt", "fix_dag_spec", "write_code"}.isdisjoint(tools)
    assert {"validate_dag_spec", "get_dag_spec"} <= tools  # the deterministic tools stay
    assert {"review_dag_spec", "scan_dag_leaks"} <= tools  # rules need no AI
    scan_error, scan_findings, ai_error = out["mcp_scan"]
    assert scan_error is False
    assert scan_findings >= 1
    assert ai_error is True  # use_ai needs features.ai
    exit_code, output = out["ai_serve"]
    assert exit_code == 1
    assert "autoair[ai]" in output
    assert out["pydantic_ai_loaded"] == []  # nothing tried to load the AI runtime
