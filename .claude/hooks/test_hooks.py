#!/usr/bin/env python3
"""Tests for the agent hooks: feed each script sample hook JSON on stdin.

    python3 .claude/hooks/test_hooks.py        # stdlib unittest, ~2 s, touches no repo file

Formatting tests run in a temporary project directory (CLAUDE_PROJECT_DIR), so no file of
the repository is formatted; they are skipped when .venv/ruff or frontend prettier is missing.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

HOOKS = Path(__file__).resolve().parent
ROOT = HOOKS.parents[1]
RUFF = ROOT / ".venv" / "bin" / "ruff"
PRETTIER = ROOT / "frontend" / "node_modules" / ".bin" / "prettier"


def run_hook(
    script: str,
    payload: dict[str, object],
    project: Path | None = None,
    env: dict[str, str] | None = None,
) -> tuple[int, dict[str, object] | None, str]:
    full_env = {**os.environ, "CLAUDE_PROJECT_DIR": str(project or ROOT), **(env or {})}
    proc = subprocess.run(
        [sys.executable, str(HOOKS / script)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        env=full_env,
        timeout=30,
        check=False,
    )
    out = json.loads(proc.stdout) if proc.stdout.strip() else None
    return proc.returncode, out, proc.stderr


def bash(command: str) -> dict[str, object]:
    return {
        "session_id": "t-" + uuid.uuid4().hex,
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
    }


def decision(command: str) -> str | None:
    code, out, _ = run_hook("guard_bash.py", bash(command))
    assert code == 0
    if out is None:
        return None
    spec = out["hookSpecificOutput"]
    assert isinstance(spec, dict) and spec["permissionDecisionReason"]
    return str(spec["permissionDecision"])


class GuardBash(unittest.TestCase):
    def test_allows_everyday_commands(self) -> None:
        for cmd in [
            "uv run ruff check src tests deploy",
            "uv run pytest -q tests/unit/test_codegen.py -k branch",
            "cd frontend && npm run typecheck && npm run lint",
            "git status --short && git diff --name-only",
            "git log --oneline -5 | head",
            "grep -rn 'git push' docs/",
            "echo 'pkill -f is forbidden'",
            "git stash list",
            "kill 999999",
            "rm -rf frontend/e2e/visual/.results",
            "docker ps",
            "npx playwright test e2e/undo.spec.ts",
            "cat frontend/openapi.json | head",
            "uv run autoair export-openapi --output frontend/openapi.json",
            "AIRFLOW_HOME=/tmp/x AIRFLOW_PORT=18080 ./scripts/dev-airflow.sh",
            "python3 - <<'EOF'\nprint('git push; pkill -f x')\nopen('x').write('> uv.lock')\nEOF\necho done",
            "cat > notes.txt <<EOF\ngit commit -m x\nEOF",
        ]:
            with self.subTest(cmd=cmd):
                self.assertIsNone(decision(cmd))

    def test_denies(self) -> None:
        for cmd in [
            "pkill -f 'autoair serve'",
            "sudo killall node",
            "kill $(lsof -t -i:8080)",
            "lsof -ti :5173 | xargs kill -9",
            "fuser -k 8765/tcp",
            "git clean -fdx",
            "git -C . reset --hard HEAD",
            "docker system prune -af",
            "docker volume rm compose_pg",
            "docker compose -f deploy/compose/docker-compose.demo.yml down -v",
            "airflow db reset -y",
            "rm -rf .airflow-dev",
            "rm -r ./.venv-allproviders/",
            "echo '{}' > frontend/openapi.json",
            "sed -i 's/a/b/' docs/configuration-reference.md",
            "bash -c 'pkill -f vite'",
            "true && \\\n  pkill -f vite",
            "bash <<'EOF'\npkill -f vite\nEOF",
            "python3 - <<'EOF'\nx = 1\nEOF\npkill -f vite",
            "cat > frontend/openapi.json <<'EOF'\n{}\nEOF",
        ]:
            with self.subTest(cmd=cmd):
                self.assertEqual(decision(cmd), "deny")

    def test_asks(self) -> None:
        for cmd in [
            "git commit -m 'Add x'",
            "git add -A && git commit -m x",
            "git push origin main",
            "git stash",
            "git checkout -- src/autoair/cli.py",
            "git switch -c feature",
            "npm publish",
            "uv publish",
            "gh pr create --fill",
            "docker rm -f autoair-smoke",
            "docker compose down",
            "./scripts/dev-airflow.sh",
            "make demo",
        ]:
            with self.subTest(cmd=cmd):
                self.assertEqual(decision(cmd), "ask")

    def test_deny_wins_over_ask(self) -> None:
        self.assertEqual(decision("git commit -m x; pkill -f x"), "deny")

    def test_kill_of_shared_listener_pid(self) -> None:
        out = subprocess.run(["ss", "-Hltnp"], capture_output=True, text=True).stdout
        for line in out.splitlines():
            if ":8080 " in line and "pid=" in line:
                pid = line.split("pid=", 1)[1].split(",", 1)[0]
                self.assertEqual(decision(f"kill {pid}"), "deny")
                return
        self.skipTest("nothing listens on :8080")

    def test_fails_open(self) -> None:
        for payload in [
            "",
            "not json",
            json.dumps({"tool_name": "Bash"}),
            json.dumps({"tool_name": "Bash", "tool_input": {"command": "echo 'x"}}),
        ]:
            proc = subprocess.run(
                [sys.executable, str(HOOKS / "guard_bash.py")],
                input=payload,
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), "")

    def test_is_fast(self) -> None:
        start = time.monotonic()
        decision("uv run pytest -q && git diff")
        self.assertLess(time.monotonic() - start, 1.0)


class GuardGenerated(unittest.TestCase):
    def payload(self, path: str, tool: str = "Edit") -> dict[str, object]:
        return {
            "session_id": "t",
            "hook_event_name": "PreToolUse",
            "tool_name": tool,
            "tool_input": {"file_path": path, "old_string": "a", "new_string": "b"},
        }

    def test_denies_generated(self) -> None:
        for rel in [
            "frontend/src/api/gen/types.gen.ts",
            "frontend/openapi.json",
            "docs/configuration-reference.md",
            "frontend/src/lib/generated/jinjaContext.gen.ts",
            "website/docs/reference/configuration.md",
            "tests/golden/expected/etl_full.py",
            "src/autoair/docs/pages.json",
            "uv.lock",
        ]:
            with self.subTest(rel=rel):
                code, out, _ = run_hook("guard_generated.py", self.payload(str(ROOT / rel)))
                self.assertEqual(code, 0)
                assert out is not None
                self.assertEqual(out["hookSpecificOutput"]["permissionDecision"], "deny")  # type: ignore[index]

    def test_allows_sources(self) -> None:
        for rel in [
            "src/autoair/settings.py",
            "frontend/src/api/setup.ts",
            "docs/configuration.md",
            "website/docs/intro.mdx",
            "CLAUDE.md",
            ".claude/rules/frontend.md",
            "/etc/hosts",
        ]:
            with self.subTest(rel=rel):
                path = rel if rel.startswith("/") else str(ROOT / rel)
                code, out, _ = run_hook("guard_generated.py", self.payload(path, "Write"))
                self.assertEqual((code, out), (0, None))


class FormatFile(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="hooktest-"))
        self.session = "t-" + uuid.uuid4().hex

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def post(self, path: Path) -> int:
        code, out, _ = run_hook(
            "format_file.py",
            {
                "session_id": self.session,
                "hook_event_name": "PostToolUse",
                "tool_name": "Write",
                "tool_input": {"file_path": str(path)},
            },
            project=self.tmp,
        )
        self.assertIsNone(out)
        return code

    @unittest.skipUnless(RUFF.exists(), "no .venv/bin/ruff")
    def test_python_formats_only_the_edited_file(self) -> None:
        edited, other = self.tmp / "a.py", self.tmp / "b.py"
        edited.write_text("import os\nimport sys\nx = {  'a':1 }\n")
        other.write_text("y = {  'b':2 }\n")
        self.assertEqual(self.post(edited), 0)
        text = edited.read_text()
        self.assertIn('x = {"a": 1}', text)
        self.assertIn("import os", text, "F401 must not be auto-fixed")
        self.assertEqual(other.read_text(), "y = {  'b':2 }\n")

    @unittest.skipUnless(PRETTIER.exists(), "no frontend prettier")
    def test_prettier_for_frontend_sources(self) -> None:
        src = self.tmp / "frontend" / "src"
        src.mkdir(parents=True)
        shutil.copy(ROOT / "frontend" / ".prettierrc.json", self.tmp / "frontend")
        edited = src / "x.ts"
        edited.write_text("const a = {b:1}\n")
        self.assertEqual(self.post(edited), 0)
        self.assertEqual(edited.read_text(), "const a = { b: 1 };\n")

    def test_leaves_other_files_alone(self) -> None:
        for rel in ["website/docs/page.md", "docs/x.md", "frontend/src/api/gen/types.gen.ts"]:
            path = self.tmp / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("const a = {b:1}\n")
            self.assertEqual(self.post(path), 0)
            self.assertEqual(path.read_text(), "const a = {b:1}\n")

    def test_outside_project_and_missing_file(self) -> None:
        self.assertEqual(self.post(Path("/etc/hostname")), 0)
        self.assertEqual(self.post(self.tmp / "missing.py"), 0)


class StopRegenCheck(unittest.TestCase):
    def test_reminds_once_for_settings_and_ui(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="hooktest-"))
        try:
            (tmp / "docs").mkdir()
            (tmp / "docs" / "configuration-reference.md").write_text("old")
            time.sleep(0.01)
            (tmp / "src" / "autoair").mkdir(parents=True)
            (tmp / "src" / "autoair" / "sections.py").write_text("x = 1\n")
            (tmp / "frontend" / "src" / "panels").mkdir(parents=True)
            (tmp / "frontend" / "src" / "panels" / "P.tsx").write_text("export {};\n")
            session = "t-" + uuid.uuid4().hex
            for rel in ["src/autoair/sections.py", "frontend/src/panels/P.tsx"]:
                run_hook(
                    "format_file.py",
                    {
                        "session_id": session,
                        "tool_name": "Edit",
                        "tool_input": {"file_path": str(tmp / rel)},
                    },
                    tmp,
                )
            stop = {"session_id": session, "hook_event_name": "Stop", "stop_hook_active": False}
            code, out, _ = run_hook("stop_regen_check.py", stop, tmp)
            self.assertEqual(code, 0)
            assert out is not None
            spec = out["hookSpecificOutput"]
            assert isinstance(spec, dict)
            self.assertEqual(spec["hookEventName"], "Stop")
            self.assertIn("gen_config_docs.py", str(spec["additionalContext"]))
            self.assertIn("light and dark", str(spec["additionalContext"]))
            self.assertEqual(run_hook("stop_regen_check.py", stop, tmp)[1], None, "only once")
            self.assertEqual(
                run_hook(
                    "stop_regen_check.py",
                    {**stop, "session_id": session, "stop_hook_active": True},
                    tmp,
                )[1],
                None,
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_subagent_edits_are_not_the_main_agents(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="hooktest-"))
        try:
            (tmp / "frontend" / "src" / "panels").mkdir(parents=True)
            (tmp / "frontend" / "src" / "panels" / "P.tsx").write_text("export {};\n")
            session = "t-" + uuid.uuid4().hex
            run_hook(
                "format_file.py",
                {
                    "session_id": session,
                    "agent_id": "sub1",
                    "tool_name": "Edit",
                    "tool_input": {"file_path": str(tmp / "frontend/src/panels/P.tsx")},
                },
                tmp,
            )
            main_stop = {"session_id": session, "hook_event_name": "Stop"}
            self.assertEqual(run_hook("stop_regen_check.py", main_stop, tmp)[1], None)
            sub_stop = {**main_stop, "hook_event_name": "SubagentStop", "agent_id": "sub1"}
            out = run_hook("stop_regen_check.py", sub_stop, tmp)[1]
            assert out is not None
            self.assertEqual(out["hookSpecificOutput"]["hookEventName"], "SubagentStop")  # type: ignore[index]
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_reminds_of_shared_constants(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="hooktest-"))
        try:
            (tmp / "src" / "autoair" / "validation").mkdir(parents=True)
            (tmp / "src" / "autoair" / "validation" / "jinja.py").write_text("X = 1\n")
            session = "t-" + uuid.uuid4().hex
            run_hook(
                "format_file.py",
                {
                    "session_id": session,
                    "tool_name": "Edit",
                    "tool_input": {"file_path": str(tmp / "src/autoair/validation/jinja.py")},
                },
                tmp,
            )
            stop = {"session_id": session, "hook_event_name": "Stop"}
            out = run_hook("stop_regen_check.py", stop, tmp)[1]
            assert out is not None
            self.assertIn("gen_shared_constants.py", str(out["hookSpecificOutput"]))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_silent_without_edits(self) -> None:
        code, out, _ = run_hook(
            "stop_regen_check.py",
            {"session_id": "t-" + uuid.uuid4().hex, "hook_event_name": "Stop"},
        )
        self.assertEqual((code, out), (0, None))


class SettingsWiring(unittest.TestCase):
    """Every hook command in .claude/settings.json names a script that exists here.

    A missing script makes each matching tool call fail with a hook error in every running
    session, so a move or rename must update settings.json in the same change."""

    def test_hook_scripts_exist(self) -> None:
        settings = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
        commands = [
            h["command"]
            for groups in settings["hooks"].values()
            for group in groups
            for h in group["hooks"]
        ]
        self.assertTrue(commands)
        for command in commands:
            with self.subTest(command=command):
                self.assertIn('"$CLAUDE_PROJECT_DIR"/.claude/hooks/', command)
                script = command.split('"$CLAUDE_PROJECT_DIR"/', 1)[1].split()[0]
                self.assertTrue((ROOT / script).is_file(), script)


def tearDownModule() -> None:
    """Remove the per-session state files the tests created."""
    folder = Path(tempfile.gettempdir()) / f"autoair-agent-hooks-{os.getuid()}"
    for path in folder.glob("t-*"):
        path.unlink(missing_ok=True)
    for path in folder.glob("t.*"):
        path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main(verbosity=1)
