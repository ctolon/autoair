"""On-demand validation of a code field in any language (the editor's "Validate" button).

Each language uses the best tool installed on the server:

========== ======================================================================
python     ``ast`` (syntax) + ruff (lint rules of the editor settings)
json       strict parse (line/column)
yaml       ``yaml.safe_load_all``
bash / sh  ``bash -n`` (syntax) + ShellCheck
sql        sqlglot (when installed)
go         ``gofmt -e``
java       ``javac`` stopped after parsing (syntax only), in a temporary directory
js / ts    ``tsc`` syntax diagnostics (TS1xxx) — esbuild when tsc is missing
markdown   built-in checks (unclosed code fences)
========== ======================================================================

External tools run as subprocesses with a timeout, AutoAir's secrets scrubbed from the
environment (:func:`autoair._subprocess.user_code_env`) and a temporary working directory;
the code is never executed. The response lists which tools ran and which were missing.
"""

from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
from collections.abc import Callable
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from autoair._subprocess import user_code_env
from autoair.lsp.shell import ShellLintRequest, lint_shell, mask_jinja, shellcheck_path

ValidateLanguage = Literal[
    "python",
    "json",
    "yaml",
    "bash",
    "sh",
    "sql",
    "go",
    "java",
    "javascript",
    "typescript",
    "markdown",
    "jinja",
    "text",
]
Severity = Literal["error", "warning", "info"]

# Schema ceiling; AUTOAIR_EDITOR__MAX_SOURCE_CHARS is the effective limit (API router).
MAX_CODE = 10_000_000


class ValidateRequest(BaseModel):
    language: ValidateLanguage
    code: str = Field(max_length=MAX_CODE)
    python_mode: Literal["module", "task_body"] = Field(
        default="module",
        description="python only: task_body = the body of a @task function (dedented and "
        "wrapped in a function before checking).",
    )


class ValidateDiagnostic(BaseModel):
    line: int = Field(ge=1, description="1-based line.")
    column: int | None = Field(default=None, description="1-based column.")
    end_line: int | None = None
    end_column: int | None = None
    severity: Severity
    message: str
    code: str | None = None
    source: str = Field(description="Tool that reported it (ast, ruff, shellcheck, javac…).")


class ToolRun(BaseModel):
    name: str
    available: bool = Field(description="False when the tool is not installed on the server.")
    ran: bool = Field(description="True when the tool checked the code.")
    note: str | None = Field(default=None, description="Why the tool did not run / failed.")


class ValidateResult(BaseModel):
    language: ValidateLanguage
    ok: bool = Field(description="No error-severity diagnostic.")
    validated: bool = Field(description="At least one tool checked the code.")
    tools: list[ToolRun] = Field(default_factory=list)
    diagnostics: list[ValidateDiagnostic] = Field(default_factory=list)


# --------------------------------------------------------------------------- helpers


def _settings() -> tuple[float, dict[str, list[str]]]:
    from autoair.sections import EditorSettings, active

    cfg = active(EditorSettings)
    return cfg.validate_timeout, dict(cfg.validate_commands)


def find_tool(name: str) -> list[str] | None:
    """Command of ``name``: the configured override (editor.validate_commands), PATH, or
    next to the running Python (wheels such as ruff / shellcheck-py install there)."""
    _, overrides = _settings()
    if overrides.get(name):
        command = list(overrides[name])
        return command if shutil.which(command[0]) or Path(command[0]).is_file() else None
    found = shutil.which(name)
    if found:
        return [found]
    local = Path(sys.executable).parent / name
    return [str(local)] if local.is_file() else None


def _run(
    command: list[str], *, stdin: str | None = None, cwd: str | None = None
) -> subprocess.CompletedProcess[str] | str:
    """Run a checker; a string is the reason it failed (timeout, OS error)."""
    timeout, _ = _settings()
    try:
        return subprocess.run(
            command,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            cwd=cwd,
            env=user_code_env(),
        )
    except subprocess.TimeoutExpired:
        return f"timed out after {timeout:g}s"
    except OSError as exc:
        return f"could not run: {exc}"


class _Collector:
    def __init__(self, language: ValidateLanguage, code: str) -> None:
        self.language = language
        self.lines = max(1, len(code.splitlines()))
        self.tools: list[ToolRun] = []
        self.diagnostics: list[ValidateDiagnostic] = []

    def tool(self, name: str, *, available: bool, ran: bool, note: str | None = None) -> None:
        self.tools.append(ToolRun(name=name, available=available, ran=ran, note=note))

    def add(
        self,
        source: str,
        line: int,
        message: str,
        *,
        column: int | None = None,
        severity: Severity = "error",
        code: str | None = None,
        end_line: int | None = None,
        end_column: int | None = None,
    ) -> None:
        item = ValidateDiagnostic(
            line=min(max(1, line), self.lines),  # "unexpected end of file" is one past
            column=column if column is None else max(1, column),
            end_line=end_line,
            end_column=end_column,
            severity=severity,
            message=message.strip() or "error",
            code=code,
            source=source,
        )
        if item not in self.diagnostics:  # tools repeat themselves (gofmt)
            self.diagnostics.append(item)

    def result(self) -> ValidateResult:
        self.diagnostics.sort(key=lambda d: (d.line, d.column or 0))
        return ValidateResult(
            language=self.language,
            ok=not any(d.severity == "error" for d in self.diagnostics),
            validated=any(t.ran for t in self.tools),
            tools=self.tools,
            diagnostics=self.diagnostics,
        )


def _missing(out: _Collector, name: str) -> None:
    out.tool(name, available=False, ran=False, note=f"{name} is not installed on the server")


# --------------------------------------------------------------------------- languages


def _python(req: ValidateRequest, out: _Collector) -> None:
    code, offset, shift = req.code, 0, 0
    if req.python_mode == "task_body":
        from autoair.codegen.body import normalize_task_body

        body = normalize_task_body(req.code) or "pass"
        code = "def task_fn(*args, **kwargs):\n" + textwrap.indent(body, "    ") + "\n"
        offset, shift = 1, 4
    try:
        ast.parse(code)
    except SyntaxError as exc:
        out.tool("ast", available=True, ran=True)
        out.add(
            "ast",
            (exc.lineno or 1) - offset,
            exc.msg,
            column=(exc.offset - shift) if exc.offset else None,
        )
        return  # ruff would only repeat the syntax error
    out.tool("ast", available=True, ran=True)
    from autoair.codegen.format import _ruff_command
    from autoair.sections import EditorSettings, active

    ruff = _ruff_command()
    if ruff is None:
        _missing(out, "ruff")
        return
    cfg = active(EditorSettings)
    select = ",".join([*cfg.python_lint_select, *cfg.python_lint_select_extra]) or "F"
    ignore = ",".join([*cfg.python_lint_ignore, *cfg.python_lint_ignore_extra])
    with tempfile.TemporaryDirectory(prefix="autoair-validate-") as tmp:
        proc = _run(
            [
                *ruff,
                "check",
                "--isolated",
                "--no-cache",
                "--select",
                select,
                *(("--ignore", ignore) if ignore else ()),
                "--output-format",
                "json",
                "--stdin-filename",
                "code.py",
                "-",
            ],
            stdin=code,
            cwd=tmp,
        )
    if isinstance(proc, str):
        out.tool("ruff", available=True, ran=False, note=proc)
        return
    try:
        items = json.loads(proc.stdout or "[]")
    except ValueError:
        out.tool("ruff", available=True, ran=False, note="unreadable ruff output")
        return
    out.tool("ruff", available=True, ran=True)
    for d in items:
        loc = d.get("location") or {}
        end = d.get("end_location") or {}
        rule = str(d.get("code") or "syntax")
        out.add(
            "ruff",
            int(loc.get("row", 1)) - offset,
            str(d.get("message", "")),
            column=int(loc.get("column", 1)) - shift,
            end_line=int(end["row"]) - offset if end.get("row") else None,
            end_column=int(end["column"]) - shift if end.get("column") else None,
            severity="error" if rule.startswith(("F82", "E9")) or rule == "syntax" else "warning",
            code=rule,
        )


def _json(req: ValidateRequest, out: _Collector) -> None:
    out.tool("json", available=True, ran=True)
    if not req.code.strip():
        return
    try:
        json.loads(req.code)
    except json.JSONDecodeError as exc:
        out.add("json", exc.lineno, exc.msg, column=exc.colno)


def _yaml(req: ValidateRequest, out: _Collector) -> None:
    out.tool("yaml", available=True, ran=True)
    try:
        for _ in yaml.safe_load_all(req.code):
            pass
    except yaml.MarkedYAMLError as exc:
        mark = exc.problem_mark or exc.context_mark
        message = " ".join(str(p) for p in (exc.context, exc.problem) if p) or str(exc)
        out.add(
            "yaml",
            (mark.line + 1) if mark else 1,
            message,
            column=(mark.column + 1) if mark else None,
        )
    except yaml.YAMLError as exc:
        out.add("yaml", 1, str(exc))


_BASH_ERROR = re.compile(r"line (\d+): (.*)")


def _shell(req: ValidateRequest, out: _Collector) -> None:
    shell = "sh" if req.language == "sh" else "bash"
    source = mask_jinja(req.code)
    binary = find_tool(shell)
    if binary is None:
        _missing(out, f"{shell} -n")
    else:
        proc = _run([*binary, "-n"], stdin=source)
        if isinstance(proc, str):
            out.tool(f"{shell} -n", available=True, ran=False, note=proc)
        else:
            out.tool(f"{shell} -n", available=True, ran=True)
            for raw in proc.stderr.splitlines():
                m = _BASH_ERROR.search(raw)
                if m:
                    out.add(f"{shell} -n", int(m.group(1)), m.group(2))
    if shellcheck_path() is None:
        _missing(out, "shellcheck")
        return
    lint = lint_shell(ShellLintRequest(code=req.code, shell=shell))
    out.tool("shellcheck", available=lint.available, ran=lint.available)
    for d in lint.diagnostics:
        out.add(
            "shellcheck",
            d.line,
            d.message,
            column=d.column,
            end_line=d.end_line,
            end_column=d.end_column,
            severity=d.severity,
            code=d.code,
        )


def _sql(req: ValidateRequest, out: _Collector) -> None:
    try:
        import sqlglot  # type: ignore[import-not-found,unused-ignore]
        from sqlglot.errors import ParseError  # type: ignore[import-not-found,unused-ignore]
    except ImportError:
        out.tool("sqlglot", available=False, ran=False, note="pip install sqlglot to validate SQL")
        return
    out.tool("sqlglot", available=True, ran=True)
    try:
        sqlglot.parse(mask_jinja(req.code))
    except ParseError as exc:
        for err in exc.errors or [{"description": str(exc), "line": 1, "col": None}]:
            out.add(
                "sqlglot",
                int(err.get("line") or 1),
                str(err.get("description") or exc),
                column=err.get("col"),
            )
    except Exception as exc:  # tokenizer errors and friends
        out.add("sqlglot", 1, str(exc))


_GOFMT = re.compile(r"^<standard input>:(\d+):(\d+): (.*)$")


def _go(req: ValidateRequest, out: _Collector) -> None:
    binary = find_tool("gofmt")
    if binary is None:
        _missing(out, "gofmt")
        return
    proc = _run([*binary, "-e"], stdin=req.code)
    if isinstance(proc, str):
        out.tool("gofmt", available=True, ran=False, note=proc)
        return
    out.tool("gofmt", available=True, ran=True)
    for raw in proc.stderr.splitlines():
        m = _GOFMT.match(raw)
        if m:
            out.add("gofmt", int(m.group(1)), m.group(3), column=int(m.group(2)))


_JAVA_CLASS = re.compile(
    r"^\s*public\s+(?:(?:final|abstract|sealed|static)\s+)*(?:class|interface|enum|record)\s+"
    r"([A-Za-z_]\w*)",
    re.M,
)
_JAVAC = re.compile(r"^.*?\.java:(\d+): (error|warning): (.*)$")


def _java(req: ValidateRequest, out: _Collector) -> None:
    binary = find_tool("javac")
    if binary is None:
        _missing(out, "javac")
        return
    m = _JAVA_CLASS.search(req.code)
    name = f"{m.group(1) if m else 'Main'}.java"
    with tempfile.TemporaryDirectory(prefix="autoair-validate-") as tmp:
        (Path(tmp) / name).write_text(req.code, encoding="utf-8")
        proc = _run(
            [
                *binary,
                # Stop after parsing: syntax only (unknown classes/imports are not errors).
                "-XDshould-stop.ifError=PARSE",
                "-XDshould-stop.ifNoError=PARSE",
                "-proc:none",
                "-implicit:none",
                "-encoding",
                "UTF-8",
                "-d",
                str(Path(tmp) / "out"),
                name,
            ],
            cwd=tmp,
        )
    if isinstance(proc, str):
        out.tool("javac", available=True, ran=False, note=proc)
        return
    out.tool("javac", available=True, ran=True)
    lines = (proc.stderr or proc.stdout).splitlines()
    for i, raw in enumerate(lines):
        hit = _JAVAC.match(raw)
        if not hit:
            continue
        column = None
        # javac prints the source line, then a caret under the column.
        if i + 2 < len(lines) and lines[i + 2].strip() == "^":
            column = lines[i + 2].index("^") + 1
        out.add(
            "javac",
            int(hit.group(1)),
            hit.group(3),
            column=column,
            severity="error" if hit.group(2) == "error" else "warning",
        )


_TSC = re.compile(r"^.*?\((\d+),(\d+)\): (error|warning) (TS\d+): (.*)$")
_ESBUILD = re.compile(r"^\s*<stdin>:(\d+):(\d+):\s*$")


def _script(req: ValidateRequest, out: _Collector) -> None:
    typescript = req.language == "typescript"
    tsc = find_tool("tsc")
    if tsc is not None:
        name = "code.ts" if typescript else "code.js"
        with tempfile.TemporaryDirectory(prefix="autoair-validate-") as tmp:
            (Path(tmp) / name).write_text(req.code, encoding="utf-8")
            proc = _run(
                [
                    *tsc,
                    "--noEmit",
                    "--noResolve",
                    "--noLib",
                    "--pretty",
                    "false",
                    "--allowJs",
                    "--target",
                    "esnext",
                    *(() if typescript else ("--checkJs", "false")),
                    name,
                ],
                cwd=tmp,
            )
        if isinstance(proc, str):
            out.tool("tsc", available=True, ran=False, note=proc)
            return
        out.tool("tsc", available=True, ran=True)
        for raw in proc.stdout.splitlines():
            m = _TSC.match(raw)
            # TS1xxx are syntax errors; the rest need types/libraries we do not load.
            if m and m.group(4).startswith("TS1") and len(m.group(4)) == 6:
                out.add("tsc", int(m.group(1)), m.group(5), column=int(m.group(2)), code=m.group(4))
        return
    esbuild = find_tool("esbuild")
    if esbuild is None:
        _missing(out, "tsc")
        _missing(out, "esbuild")
        return
    proc = _run(
        [
            *esbuild,
            f"--loader={'ts' if typescript else 'js'}",
            "--log-level=error",
            "--color=false",
        ],
        stdin=req.code,
    )
    if isinstance(proc, str):
        out.tool("esbuild", available=True, ran=False, note=proc)
        return
    out.tool("esbuild", available=True, ran=True)
    message = ""
    for raw in proc.stderr.splitlines():
        if "[ERROR]" in raw:
            message = raw.split("[ERROR]", 1)[1].strip()
            continue
        m = _ESBUILD.match(raw)
        if m and message:
            out.add("esbuild", int(m.group(1)), message, column=int(m.group(2)) + 1)
            message = ""


_FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")


def _markdown(req: ValidateRequest, out: _Collector) -> None:
    out.tool("markdown", available=True, ran=True)
    opened: tuple[int, str] | None = None
    for i, raw in enumerate(req.code.splitlines(), start=1):
        m = _FENCE.match(raw)
        if not m:
            continue
        fence = m.group(1)
        if opened is None:
            opened = (i, fence)
        elif fence[0] == opened[1][0] and len(fence) >= len(opened[1]):
            opened = None
    if opened is not None:
        out.add(
            "markdown",
            opened[0],
            "code block is never closed",
            severity="warning",
            code="unclosed-fence",
        )


_CHECKERS: dict[str, Callable[[ValidateRequest, _Collector], None]] = {
    "python": _python,
    "json": _json,
    "yaml": _yaml,
    "bash": _shell,
    "sh": _shell,
    "sql": _sql,
    "go": _go,
    "java": _java,
    "javascript": _script,
    "typescript": _script,
    "markdown": _markdown,
}


def validate_code(req: ValidateRequest) -> ValidateResult:
    """Diagnostics of ``req.code`` (see the module docstring for the tools)."""
    out = _Collector(req.language, req.code)
    checker = _CHECKERS.get(req.language)
    if checker is None:
        out.tool(req.language, available=False, ran=False, note="no validator for this language")
    else:
        checker(req, out)
    return out.result()


__all__ = [
    "ToolRun",
    "ValidateDiagnostic",
    "ValidateLanguage",
    "ValidateRequest",
    "ValidateResult",
    "find_tool",
    "validate_code",
]
