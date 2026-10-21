#!/usr/bin/env python3
"""PreToolUse (Edit|Write|MultiEdit|NotebookEdit): refuse hand edits of generated files.

Generated files (the API client, the configuration reference, build output, golden
snapshots, lock files) are listed in ``_lib.GENERATED`` together with the command that
regenerates them; the denial reason names that command. Every other path is allowed
without output, so normal edits are never slowed down or blocked.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib import emit, fail_open, generated_reason, project_dir, read_event, relative  # noqa: E402


def main() -> None:
    event = read_event()
    tool_input = event.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return
    path = tool_input.get("file_path") or tool_input.get("notebook_path")
    if not isinstance(path, str) or not path:
        return
    rel = relative(path, project_dir(event))
    how = generated_reason(rel) if rel else None
    if how is None:
        return
    emit({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": f"AutoAir guard: {rel} is generated and must not be "
            f"edited by hand; {how}. (CLAUDE.md, 'Generated files')",
        }
    })


if __name__ == "__main__":
    fail_open(main)
