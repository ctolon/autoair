#!/usr/bin/env python3
"""PostToolUse (Edit|Write|MultiEdit): format the one file the tool just wrote.

* ``*.py``: ``ruff format`` then ``ruff check --fix`` with the repository's configuration.
  ``F401`` (unused import) is not auto-fixed: an agent often adds an import one edit before
  the code that uses it, and removing it in between breaks the next edit.
* ``frontend/src/**`` and ``frontend/e2e/**`` (ts, tsx, js, json, css, md): the frontend's
  prettier, the same paths ``npm run format`` covers.

Anything else (website/, docs/, generated files, files outside the repository) is left
alone. The hook records the path for the Stop hook, never blocks and always exits 0.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib import ROOT, fail_open, generated_reason, project_dir, read_event, relative, state_file  # noqa: E402

TIMEOUT = 8  # seconds per formatter call; the settings.json hook timeout is the hard cap
PRETTIER_DIRS = ("frontend/src/", "frontend/e2e/")
PRETTIER_EXT = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".json", ".css", ".md"}


def run(cmd: list[str], cwd: Path) -> None:
    subprocess.run(cmd, cwd=cwd, timeout=TIMEOUT, capture_output=True, check=False)


def main() -> None:
    event = read_event()
    tool_input = event.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return
    path = tool_input.get("file_path")
    if not isinstance(path, str) or not path:
        return
    root = project_dir(event)
    rel = relative(path, root)
    if rel is None or not Path(path).is_file():
        return
    with state_file(event, "touched").open("a", encoding="utf-8") as fh:
        fh.write(rel + "\n")
    if generated_reason(rel):
        return
    suffix = Path(rel).suffix
    if suffix == ".py":
        ruff = ROOT / ".venv" / "bin" / "ruff"
        if not ruff.exists():
            return
        # --force-exclude applies pyproject's extend-exclude (golden snapshots, scaffold
        # templates) to an explicitly named file too.
        run([str(ruff), "format", "--force-exclude", "--quiet", rel], root)
        run(
            [str(ruff), "check", "--fix", "--force-exclude", "--quiet", "--exit-zero",
             "--unfixable", "F401", rel],
            root,
        )
    elif suffix in PRETTIER_EXT and rel.startswith(PRETTIER_DIRS):
        frontend = root / "frontend"
        prettier = ROOT / "frontend" / "node_modules" / ".bin" / "prettier"
        if not prettier.exists():
            return
        # Run from frontend/ so .prettierrc.json and .prettierignore (src/api/gen) apply.
        run(
            [str(prettier), "--write", "--ignore-unknown", "--log-level", "silent",
             rel.removeprefix("frontend/")],
            frontend,
        )


if __name__ == "__main__":
    fail_open(main)
