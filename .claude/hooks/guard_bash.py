#!/usr/bin/env python3
"""PreToolUse (Bash): stop commands that harm the shared checkout, the shared demo or remotes.

The working tree is shared by several agents at once and the demo stack (scripts/demo.sh) is
shared by everyone, so the guard is about *other people's* state:

* deny: ``pkill``/``killall`` (they match other sessions' processes), ``kill`` or ``fuser -k``
  aimed at a shared demo port or at a process listening on one, ``git clean -f``,
  ``git reset --hard``, ``docker system|volume|image|container prune``, ``docker volume rm``,
  ``docker compose down -v``, ``airflow db reset|clean``, ``rm -r`` of the shared Airflow home or
  venvs, and shell writes into generated files;
* ask (the user confirms, which is how "unless the user asked" is enforced): ``git commit``,
  ``git push``, ``git stash``, ``git checkout``/``switch``/``restore`` (they rewrite files other
  agents are editing), ``git rebase``/``merge``, publishing (``npm publish``, ``uv publish``,
  ``twine upload``, ``gh release|pr create|pr merge``), ``docker rm|stop|kill``,
  ``docker compose down``, and starting the dev stack on the shared ports.

Decisions use the documented JSON protocol (``hookSpecificOutput.permissionDecision``) with
a reason. Unknown or unparsable input is allowed: the guard fails open.
"""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib import GENERATED, SHARED_PORTS, emit, fail_open, read_event  # noqa: E402

SEPARATORS = {";", ";;", "&&", "||", "|", "|&", "&", "(", ")", "{", "}", "!", "then", "do",
              "else", "elif"}
WRAPPERS = {"sudo", "env", "command", "builtin", "nohup", "nice", "time", "stdbuf", "exec",
            "xargs", "timeout", "setsid"}
SHARED_PATHS = (".airflow-dev", ".venv-allproviders", ".venv", ".airflow-fab", ".airflow-keycloak")
PORTS_RE = "|".join(str(p) for p in sorted(SHARED_PORTS))


HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
SHELLS = {"bash", "sh", "zsh", "dash", "ssh"}


def strip_heredocs(command: str) -> str:
    """Drop here-document bodies (data such as a Python script or file contents), except
    when the here-document feeds a shell, whose body is commands."""
    lines = command.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        i += 1
        for match in HEREDOC_RE.finditer(line):
            first = line.strip().split(" ", 1)[0]
            if Path(first).name in SHELLS:
                continue
            delimiter = match.group(2)
            while i < len(lines) and lines[i].strip() != delimiter:
                i += 1
            i += 1  # the delimiter line
    return "\n".join(out)


def segments(command: str) -> list[list[str]]:
    """Split a shell command into simple commands (best effort, no expansion)."""
    flat = command.replace("\\\n", " ").replace("\n", " ; ")
    lexer = shlex.shlex(flat.replace("$(", " ( ").replace("`", " ( "), posix=True,
                        punctuation_chars=";&|()")
    out: list[list[str]] = [[]]
    for token in lexer:
        if token in SEPARATORS:
            out.append([])
        else:
            out[-1].append(token)
    result: list[list[str]] = []
    for words in out:
        words = strip_prefix(words)
        if not words:
            continue
        # bash -c "…" / sh -c '…': inspect the inner script too.
        if Path(words[0]).name in {"bash", "sh", "zsh"} and "-c" in words:
            idx = words.index("-c")
            if idx + 1 < len(words):
                result.extend(segments(words[idx + 1]))
                continue
        result.append(words)
    return result


def strip_prefix(words: list[str]) -> list[str]:
    """Drop ``VAR=value`` assignments and wrappers such as ``sudo`` or ``timeout 5``."""
    i = 0
    while i < len(words):
        word = words[i]
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", word):
            i += 1
        elif Path(word).name in WRAPPERS:
            i += 1
            while i < len(words) and (words[i].startswith("-") or re.fullmatch(r"\d+[smhd]?", words[i])):
                i += 1
        else:
            break
    return words[i:]


def git_subcommand(words: list[str]) -> tuple[str, list[str]]:
    i = 1
    while i < len(words) and words[i].startswith("-"):
        i += 2 if words[i] in {"-C", "-c", "--git-dir", "--work-tree"} else 1
    return (words[i], words[i + 1:]) if i < len(words) else ("", [])


def listener_pids() -> set[str]:
    try:
        out = subprocess.run(["ss", "-Hltnp"], capture_output=True, text=True, timeout=2).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    pids: set[str] = set()
    for line in out.splitlines():
        cols = line.split()
        if len(cols) < 4:
            continue
        port = cols[3].rsplit(":", 1)[-1]
        if port.isdigit() and int(port) in SHARED_PORTS:
            pids.update(re.findall(r"pid=(\d+)", line))
    return pids


def check(words: list[str], raw: str) -> tuple[str, str] | None:
    name = Path(words[0]).name
    args = words[1:]
    joined = " ".join(words)

    if name in {"pkill", "killall"}:
        return ("deny", f"`{name}` matches processes of other sessions and the shared demo. "
                "Kill only the PIDs you started yourself (`kill <pid>`).")
    if name == "fuser" and any(a.startswith("-k") for a in args):
        if re.search(rf"\b({PORTS_RE})/tcp\b", joined):
            return ("deny", "That port belongs to the shared demo stack (Airflow :8080, AutoAir "
                    ":8765, Vite :5173, docs :3000). Use your own port range.")
    if name == "kill":
        if re.search(rf"[:= ]({PORTS_RE})\b", raw) and re.search(r"\b(lsof|fuser|ss)\b", raw):
            return ("deny", "This kills whatever listens on a shared demo port. Never stop the "
                    "shared Airflow/AutoAir/Vite/docs servers; kill only PIDs you started.")
        pids = {a for a in args if a.isdigit()}
        if pids and pids & listener_pids():
            return ("deny", f"PID(s) {sorted(pids & listener_pids())} serve the shared demo "
                    "stack. Kill only the processes you started yourself.")

    if name == "git":
        sub, rest = git_subcommand(words)
        if sub == "clean" and any(re.fullmatch(r"-[a-zA-Z]*f[a-zA-Z]*|--force", a) for a in rest):
            return ("deny", "`git clean -f` deletes untracked files, which here includes other "
                    "agents' uncommitted work (the repository has untracked sources).")
        if sub == "reset" and "--hard" in rest:
            return ("deny", "`git reset --hard` discards other agents' uncommitted changes in "
                    "this shared working tree.")
        if sub in {"commit", "push"}:
            return ("ask", f"`git {sub}` only when the user explicitly asked for it.")
        if sub in {"stash", "checkout", "switch", "restore", "rebase", "merge", "cherry-pick",
                   "revert", "am"}:
            if sub == "stash" and rest[:1] in (["list"], ["show"]):
                return None
            return ("ask", f"`git {sub}` rewrites files in a working tree that other agents "
                    "are editing right now. Confirm with the user first.")

    if (name == "npm" and args[:1] == ["publish"]) or (name == "uv" and args[:1] == ["publish"]) \
            or (name == "twine" and args[:1] == ["upload"]) \
            or (name == "gh" and (args[:2] in (["release", "create"], ["pr", "create"],
                                               ["pr", "merge"]))):
        return ("ask", "Publishing (packages, releases, pull requests) only when the user asked.")

    if name == "docker" or name == "docker-compose":
        rest = args if name == "docker" else ["compose", *args]
        if rest[:1] == ["compose"]:
            # skip compose's global options and their values (-f FILE, -p NAME, --profile P)
            i = 1
            while i < len(rest) and rest[i].startswith("-"):
                takes_value = rest[i] in {"-f", "--file", "-p", "--project-name", "--profile",
                                          "--env-file", "--project-directory", "--ansi"}
                i += 2 if takes_value else 1
            rest = ["compose", *rest[i:]]
        sub = rest[:2]
        if "prune" in args or sub[:2] == ["volume", "rm"] or \
                (sub[:2] == ["compose", "down"] and ("-v" in args or "--volumes" in args)):
            return ("deny", "This deletes Docker data that this session did not create (the "
                    "shared demo stack's volumes and images). Ask the user to do it.")
        if sub[:1] in (["rm"], ["stop"], ["kill"], ["rmi"]) or \
                sub[:2] in (["container", "rm"], ["container", "stop"], ["network", "rm"]) or \
                sub[:2] == ["compose", "down"] or sub[:2] == ["compose", "stop"]:
            return ("ask", "Stopping or removing containers may hit the shared demo stack. "
                    "Confirm the containers were created by this session.")

    if name == "airflow" and args[:1] == ["db"] and args[1:2] and args[1] in {"reset", "clean"}:
        return ("deny", "`airflow db reset/clean` wipes an Airflow database; with the default "
                "AIRFLOW_HOME that is the shared demo's.")

    if name == "rm" and any(a.startswith("-") and "r" in a.lower() for a in args):
        for a in args:
            if a.startswith("-"):
                continue
            if Path(a.rstrip("/")).name in SHARED_PATHS:
                return ("deny", f"`{a}` holds the shared demo's Airflow home or virtualenv.")

    if name == "make" and any(a in {"dev-airflow", "demo", "demo-down", "image"} for a in args):
        return ("ask", "This make target starts or stops the shared dev stack or Docker "
                "resources. Confirm with the user first.")
    if any(Path(w).name in {"dev-airflow.sh", "demo.sh"} for w in words[:2]) and \
            "AIRFLOW_PORT=" not in raw and "AIRFLOW_HOME=" not in raw:
        return ("ask", "This starts Airflow on the shared port 8080 and the shared "
                ".airflow-dev home. Set AIRFLOW_HOME and AIRFLOW_PORT to private values, or "
                "confirm with the user.")
    return None


def check_generated_writes(raw: str) -> tuple[str, str] | None:
    for prefix, how in GENERATED:
        target = re.escape(prefix.rstrip("/"))
        if re.search(rf"(>>?|\btee\b(\s+-a)?|\bsed\s+-i\S*\b.*)\s*\S*{target}", raw):
            return ("deny", f"`{prefix}` is generated; do not write it by hand: {how}.")
    return None


def main() -> None:
    event = read_event()
    if event.get("tool_name") != "Bash":
        return
    tool_input = event.get("tool_input") or {}
    raw = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(raw, str) or not raw.strip():
        return
    raw = strip_heredocs(raw)
    decision = check_generated_writes(raw)
    if decision is None:
        try:
            parsed = segments(raw)
        except ValueError:  # unbalanced quotes: let the shell report it
            parsed = []
        order = {"deny": 0, "ask": 1}
        found = [d for words in parsed if (d := check(words, raw))]
        decision = min(found, key=lambda d: order[d[0]]) if found else None
    if decision is None:
        return
    emit({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision[0],
            "permissionDecisionReason": f"AutoAir guard: {decision[1]} (see CLAUDE.md)",
        }
    })


if __name__ == "__main__":
    fail_open(main)
