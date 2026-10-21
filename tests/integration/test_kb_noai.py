"""The knowledge base without the ``autoair[ai]`` extra: keyword search still works.

Runs in a fresh interpreter in which ``pydantic_ai`` cannot be imported (also part of
``nox -s noai``, where it is really absent).
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
    import asyncio, json, sys
    sys.modules["pydantic_ai"] = None  # unimportable, like a missing extra
    from pathlib import Path
    from autoair.api.app import build_services, create_app
    from autoair.auth import AuthBackend
    from autoair.settings import Settings
    from fastapi.testclient import TestClient

    root = Path(sys.argv[1])
    (root / "docs").mkdir()
    (root / "docs" / "pools.md").write_text("# Pools\n\nPools limit task parallelism.\n")
    settings = Settings(
        dags_folder=root / "dags",
        ai={"embedding": {"provider": "openai", "model": "text-embedding-3-small"}},
        kb={
            "sources": [{"id": "corp", "kind": "path", "url": str(root / "docs")}],
            "refresh_interval_hours": 0, "refresh_on_start": False,
        },
    )
    out = {"features": settings.effective_features()}
    services = build_services(settings, AuthBackend())
    from autoair.kb.service import kb_for
    out["refresh"] = kb_for(services).refresh().outcome
    client = TestClient(create_app(services))
    res = client.get("/api/kb/search", params={"q": "parallelism"})
    out["search"] = [res.status_code, res.json().get("mode"), len(res.json().get("hits", []))]
    out["status_mode"] = client.get("/api/kb/status").json()["mode"]
    from typer.testing import CliRunner
    from autoair.cli import app as cli
    run = CliRunner().invoke(cli, ["kb", "search", "parallelism", "--json"],
                             env={"AUTOAIR_DAGS_FOLDER": str(root / "dags"),
                                  "AUTOAIR_KB__SOURCES": json.dumps(
                                      [{"id": "corp", "kind": "path",
                                        "url": str(root / "docs")}])})
    out["cli"] = [run.exit_code, json.loads(run.output).get("mode") if run.exit_code == 0
                  else run.output]
    out["pydantic_ai_loaded"] = sorted(m for m in sys.modules if m.startswith("pydantic_ai."))
    print("RESULT" + json.dumps(out))
    """
)


def test_kb_works_without_the_ai_extra(tmp_path: Path) -> None:
    script = tmp_path / "kb_noai.py"
    script.write_text(SCRIPT)
    work = tmp_path / "work"
    work.mkdir()
    proc = subprocess.run(
        [sys.executable, str(script), str(work)],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        env={**os.environ, "PYTHONWARNINGS": "ignore"},
    )
    line = next((x for x in proc.stdout.splitlines() if x.startswith("RESULT")), None)
    assert line is not None, proc.stdout[-3000:] + proc.stderr[-5000:]
    out = json.loads(line[len("RESULT") :])
    assert out["features"]["ai"] is False
    assert out["features"]["kb"] is True
    assert out["refresh"] == "ok"
    assert out["search"] == [200, "lexical", 1]
    assert out["status_mode"] == "lexical"
    assert out["cli"] == [0, "lexical"]
    assert out["pydantic_ai_loaded"] == []
