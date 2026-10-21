"""Shared helpers for the agent hooks (stdlib only, Python 3.9+).

Every hook reads one JSON object on stdin (the Claude Code hook protocol) and must never
crash the agent's tool call: callers wrap their work in ``fail_open``.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Ports of the shared demo stack (scripts/demo.sh): Airflow API, Airflow log servers,
# standalone AutoAir, Vite dev server, Docusaurus. Agents must never stop what listens here.
SHARED_PORTS = frozenset(
    int(p)
    for p in os.environ.get("AUTOAIR_SHARED_PORTS", "8080,8765,5173,3000,8793,8794").split(",")
)

# Files produced by generators. The value says how to regenerate them.
GENERATED: tuple[tuple[str, str], ...] = (
    ("frontend/src/api/gen/", "run `cd frontend && npm run codegen`"),
    ("frontend/openapi.json", "run `cd frontend && npm run codegen`"),
    ("docs/configuration-reference.md", "run `uv run python scripts/gen_config_docs.py`"),
    ("frontend/src/lib/generated/", "run `uv run python scripts/gen_shared_constants.py`"),
    ("website/docs/reference/configuration.md", "it is copied by `cd website && npm run sync`"),
    ("website/static/openapi.json", "run `cd website && npm run export-api`"),
    ("website/static/schemas/", "run `cd website && npm run export-api`"),
    ("src/autoair/static/plugin/", "run `cd frontend && npm run build`"),
    ("src/autoair/static/standalone/", "run `cd frontend && npm run build`"),
    (
        "tests/golden/expected/",
        "run `AUTOAIR_UPDATE_GOLDEN=1 uv run pytest tests/integration/test_airflow_dags.py"
        " -k golden_source` and review the diff",
    ),
    ("uv.lock", "run `uv lock` or `uv add`"),
    ("frontend/package-lock.json", "run `npm install` in frontend/"),
    ("website/package-lock.json", "run `npm install` in website/"),
)


def read_event() -> dict[str, object]:
    raw = sys.stdin.read()
    data = json.loads(raw) if raw.strip() else {}
    return data if isinstance(data, dict) else {}


def project_dir(event: dict[str, object]) -> Path:
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    return Path(env).resolve() if env else ROOT


def relative(path: str, base: Path) -> str | None:
    """``path`` relative to ``base`` with forward slashes, or None when it is outside."""
    try:
        return Path(path).resolve().relative_to(base).as_posix()
    except (ValueError, OSError):
        return None


def generated_reason(rel: str) -> str | None:
    for prefix, how in GENERATED:
        if rel == prefix.rstrip("/") or (prefix.endswith("/") and rel.startswith(prefix)):
            return how
    return None


def state_file(event: dict[str, object], suffix: str) -> Path:
    """Per-session (and per-subagent) scratch file outside the repository.

    Subagents share their parent's ``session_id``; their events carry an ``agent_id``, so
    each subagent gets its own record and the main agent is not reminded of their edits.
    """
    session = str(event.get("session_id") or "nosession")
    if agent := event.get("agent_id"):
        session = f"{session}-{agent}"
    safe = "".join(c for c in session if c.isalnum() or c in "-_")[:80] or "nosession"
    folder = Path(tempfile.gettempdir()) / f"autoair-agent-hooks-{os.getuid()}"
    folder.mkdir(mode=0o700, exist_ok=True)
    return folder / f"{safe}.{suffix}"


def emit(payload: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(payload))
    sys.stdout.flush()


def fail_open(main: Callable[[], None]) -> None:
    """Run ``main``; any unexpected error lets the tool call proceed (exit 0)."""
    try:
        main()
    except Exception as exc:  # a broken hook must never block the agent
        print(f"autoair hook skipped: {exc!r}", file=sys.stderr)
    sys.exit(0)
