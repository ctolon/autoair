#!/usr/bin/env python3
"""Stop: remind the agent of regeneration steps its own edits require.

Reads the files this session edited (recorded by ``format_file.py``), not ``git diff``: the
working tree is shared with other agents, whose changes are not this session's to finish.

* a settings module changed after ``docs/configuration-reference.md`` -> gen_config_docs.py;
* an API router/model changed after ``frontend/openapi.json`` -> ``npm run codegen``;
* a module mirrored into ``frontend/src/lib/generated/`` changed -> gen_shared_constants.py;
* a UI file under ``frontend/src`` changed -> screenshots light/dark, plugin/standalone;
* a locale file of one language changed but not the other -> i18n parity.

It continues the turn at most once per distinct reminder (``stop_hook_active`` and a per-session
record prevent loops), and the reason tells the agent to say so when a step does not apply.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib import emit, fail_open, project_dir, read_event, state_file  # noqa: E402

SETTINGS_RE = re.compile(
    r"^src/autoair/(settings|sections|config_sources|config_reference)\.py$"
    r"|^src/autoair/[a-z_]+/config\.py$"
)
API_RE = re.compile(r"^src/autoair/(api/|server\.py$|spec/)")
SHARED_RE = re.compile(r"^src/autoair/validation/jinja\.py$")
SHARED_OUT = "frontend/src/lib/generated/jinjaContext.gen.ts"
UI_RE = re.compile(r"^frontend/src/(?!api/gen/).*\.(tsx|css)$")
LOCALE_RE = re.compile(r"^frontend/src/i18n/locales/(en|tr)/([a-z]+)\.json$")


def newer(paths: list[str], root: Path, output: str) -> bool:
    out = root / output
    if not out.exists():
        return True
    mtime = out.stat().st_mtime
    return any((root / p).exists() and (root / p).stat().st_mtime > mtime for p in paths)


def main() -> None:
    event = read_event()
    if event.get("stop_hook_active"):
        return
    touched_file = state_file(event, "touched")
    if not touched_file.exists():
        return
    touched = sorted(set(touched_file.read_text(encoding="utf-8").split()))
    root = project_dir(event)
    notes: list[str] = []

    settings = [p for p in touched if SETTINGS_RE.match(p)]
    if settings and newer(settings, root, "docs/configuration-reference.md"):
        notes.append("Settings changed (" + ", ".join(settings) + "): run "
                     "`uv run python scripts/gen_config_docs.py` and "
                     "`uv run python scripts/scan_hardcoded.py --check`.")
    api = [p for p in touched if API_RE.match(p)]
    if api and newer(api, root, "frontend/openapi.json"):
        notes.append("Backend API or spec code changed: if the OpenAPI schema changed, run "
                     "`cd frontend && npm run codegen` and keep openapi.json + src/api/gen.")
    shared = [p for p in touched if SHARED_RE.match(p)]
    if shared and newer(shared, root, SHARED_OUT):
        notes.append("A module with constants mirrored in the frontend changed: run "
                     "`uv run python scripts/gen_shared_constants.py` (CI runs --check).")
    if any(UI_RE.match(p) for p in touched):
        notes.append("UI changed: take screenshots in light and dark mode, in plugin and "
                     "standalone mode (skill verify-ui-screenshots), unless the change is "
                     "not visible.")
    locales: dict[str, set[str]] = {}
    for p in touched:
        if m := LOCALE_RE.match(p):
            locales.setdefault(m.group(2), set()).add(m.group(1))
    one_sided = sorted(ns for ns, langs in locales.items() if langs != {"en", "tr"})
    if one_sided:
        notes.append("Locale namespace(s) " + ", ".join(one_sided) + " edited in one language "
                     "only: add the key to both en and tr.")
    if not notes:
        return

    key = "\n".join(notes)
    seen_file = state_file(event, "reminded")
    seen = seen_file.read_text(encoding="utf-8") if seen_file.exists() else ""
    if key in seen:
        return
    with seen_file.open("a", encoding="utf-8") as fh:
        fh.write(key + "\n\x00\n")
    # additionalContext, not decision "block": the hook is working as designed, so the
    # transcript shows "Stop hook feedback" instead of a hook error (hooks reference, Stop
    # decision control). The same loop guards (stop_hook_active) apply.
    event_name = "SubagentStop" if event.get("hook_event_name") == "SubagentStop" else "Stop"
    emit({
        "hookSpecificOutput": {
            "hookEventName": event_name,
            "additionalContext": "Before finishing, check these follow-ups from your own "
            "edits. Do the ones that apply; for any that do not, say why in one line.\n- "
            + "\n- ".join(notes),
        }
    })


if __name__ == "__main__":
    fail_open(main)
