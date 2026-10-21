"""Python language services for task code (completion, hover, signature help) via jedi.

The user's function body is wrapped in the same signature the code generator emits, so
argument names (and the Task Loop ``loop`` context) resolve. Jedi analyses the code
statically against the server's environment — where Airflow and the providers are
installed — so ``airflow.sdk``, hooks and operators complete with their real docs.
Nothing is executed.
"""

from __future__ import annotations

import logging
import re
import textwrap
import threading
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

if TYPE_CHECKING:  # jedi is imported on first use: autoair.lsp.config must stay light
    import jedi

log = logging.getLogger(__name__)
_LOCK = threading.Lock()  # jedi's caches are not thread-safe
_INDENT = "    "
# Schema ceiling of code sent to the editor services; AUTOAIR_EDITOR__MAX_SOURCE_CHARS is the
# effective limit (checked by the API router).
MAX_CODE = 10_000_000


def _max_doc() -> int:
    from autoair.sections import EditorSettings, active

    return active(EditorSettings).max_doc_chars


class PythonContext(BaseModel):
    code: str = Field(max_length=MAX_CODE)
    line: int = Field(ge=1, description="1-based line inside the task body")
    column: int = Field(ge=0, description="0-based column inside the line")
    arguments: list[str] = Field(default_factory=list)
    uses_loop_context: bool = False
    function_name: str = "task_fn"
    mode: Literal["task_body", "module"] = Field(
        default="task_body",
        description="task_body: code is the body of a @task function (wrapped for analysis); "
        "module: code is a whole module (inline functions, .py script files).",
    )


ItemType = Literal[
    "function", "class", "module", "keyword", "variable", "property", "param", "text"
]


class CompletionItem(BaseModel):
    label: str
    type: ItemType
    detail: str | None = None
    info: str | None = None


class CompletionList(BaseModel):
    items: list[CompletionItem]
    prefix_length: int = 0


class HoverInfo(BaseModel):
    markdown: str | None


class SignatureInfo(BaseModel):
    label: str
    active_parameter: int | None = None
    doc: str | None = None


class SignatureList(BaseModel):
    signatures: list[SignatureInfo]


_TYPES: dict[str, ItemType] = {
    "function": "function",
    "class": "class",
    "module": "module",
    "keyword": "keyword",
    "instance": "variable",
    "statement": "variable",
    "param": "param",
    "property": "property",
    "path": "text",
}


CONTEXT_MODULE = "autoair.lsp.airflow_context"
_STAR = re.compile(r"^\*{1,2}[A-Za-z_]\w*$")


def wrapper_header(
    arguments: list[str],
    uses_loop_context: bool = False,
    function_name: str = "task_fn",
    context_module: str = CONTEXT_MODULE,
) -> str:
    """Lines placed before a task body (indented by four spaces) so it analyses like the
    generated function: Airflow's context names are importable (``ti``, ``ds``, ``params``,
    ``dag_run`` …) and parameters named after them default to the typed stub values (as
    keyword-only parameters, so any order stays valid Python)."""
    from autoair.lsp.airflow_context import __all__ as context_names

    positional: list[str] = []
    keyword: list[str] = []
    star: str | None = None
    double: str | None = None
    seen: set[str] = set()
    for raw in arguments:
        arg = raw.strip()
        if arg.isidentifier() and arg not in seen:
            seen.add(arg)
            if arg in context_names:
                keyword.append(f"{arg}=_autoair_ctx.{arg}")
            else:
                positional.append(arg)
        elif _STAR.match(arg):
            if arg.startswith("**"):
                double = arg
            else:
                star = arg
    if uses_loop_context and "loop" not in seen:
        keyword.append("loop=None")
    params = list(positional)
    if star or keyword:
        params.append(star or "*")
    params += keyword
    if double:
        params.append(double)
    name = function_name if function_name.isidentifier() else "task_fn"
    return (
        f"import {context_module} as _autoair_ctx\n"
        f"from {context_module} import *\n"
        f"def {name}({', '.join(params)}):\n"
    )


def _wrap(ctx: PythonContext) -> tuple[str, int, str]:
    """(wrapped source, lines before the body, the body as analysed)."""
    header = wrapper_header(ctx.arguments, ctx.uses_loop_context, ctx.function_name)
    from autoair.codegen.body import normalize_task_body  # light settings import

    code = normalize_task_body(ctx.code)
    body = textwrap.indent(code or "pass", _INDENT) or f"{_INDENT}pass"
    return header + body + "\n", header.count("\n"), code


def _prepare(ctx: PythonContext) -> tuple[str, int, int]:
    """(analysed source, 1-based line, 0-based column) for the cursor of ``ctx``."""
    if ctx.mode == "module":
        source, line, shift = ctx.code, ctx.line, 0
    else:
        source, offset, body = _wrap(ctx)
        line, shift = ctx.line + offset, len(_INDENT)
        # A pasted body may have been dedented: the cursor moves left with its line.
        original = ctx.code.split("\n")
        analysed = body.split("\n")
        index = ctx.line - 1
        if 0 <= index < min(len(original), len(analysed)):
            shift -= len(original[index]) - len(analysed[index])
    lines = source.splitlines() or [""]
    line = min(max(line, 1), len(lines))
    text = lines[line - 1]
    column = max(0, min(ctx.column + shift, len(text)))
    return source, line, column


def _script(ctx: PythonContext) -> tuple[jedi.Script, int, int]:
    import jedi

    source, line, column = _prepare(ctx)
    return jedi.Script(source, path="autoair_task.py"), line, column


def _typed_prefix(script_line: str, column: int) -> str:
    match = re.search(r"[A-Za-z_]\w*$", script_line[:column])
    return match.group(0) if match else ""


def _rank(name: str, kind: str, prefix: str) -> tuple[int, int]:
    """Keywords and exact-prefix matches first, then case-insensitive prefixes; private
    names after public ones (jedi's own order breaks ties)."""
    if prefix and name.startswith(prefix):
        group = 0 if kind == "keyword" else 1
    elif prefix and name.lower().startswith(prefix.lower()):
        group = 2
    else:
        group = 3 if prefix else 1
    return group, 1 if name.startswith("_") and not prefix.startswith("_") else 0


def complete(ctx: PythonContext, limit: int | None = None) -> CompletionList:
    if limit is None:
        from autoair.sections import EditorSettings, active

        limit = active(EditorSettings).max_completions
    with _LOCK:
        script, line, column = _script(ctx)
        try:
            completions = script.complete(line, column)
        except Exception:
            return CompletionList(items=[])
        lines = _prepare(ctx)[0].splitlines()
        typed = _typed_prefix(lines[line - 1] if line <= len(lines) else "", column)
        ordered = sorted(
            (c for c in completions if c.name != "_autoair_ctx"),
            key=lambda c: _rank(c.name, c.type, typed),
        )  # stable: jedi's order within a group
        items: list[CompletionItem] = []
        prefix = len(typed)
        for c in ordered[:limit]:
            prefix = len(c.name) - len(c.complete or "")
            detail: str | None = None
            info: str | None = None
            if len(items) < 30:  # docs are expensive; resolve only the top results
                try:
                    sigs = c.get_signatures()
                    detail = sigs[0].to_string() if sigs else (c.description or None)
                    doc = c.docstring(raw=True)
                    info = doc[: _max_doc()] if doc else None
                except Exception:
                    detail = c.description or None
            items.append(
                CompletionItem(
                    label=c.name,
                    type=_TYPES.get(c.type, "text"),
                    detail=detail,
                    info=info,
                )
            )
        return CompletionList(items=items, prefix_length=prefix)


def hover(ctx: PythonContext) -> HoverInfo:
    with _LOCK:
        script, line, column = _script(ctx)
        try:
            names = script.help(line, column) or script.infer(line, column)
        except Exception:
            return HoverInfo(markdown=None)
        for name in names:
            try:
                sigs = name.get_signatures()
                head = sigs[0].to_string() if sigs else (name.full_name or name.name)
                doc = name.docstring(raw=True)
            except Exception:  # jedi can fail on exotic objects: try the next candidate
                log.debug("hover: cannot describe %s", getattr(name, "name", name), exc_info=True)
                continue
            if not head and not doc:
                continue
            parts = [f"```python\n{head}\n```"] if head else []
            if doc:
                parts.append(doc[: _max_doc()])
            module = getattr(name, "module_name", None)
            if module and module != "autoair_task":
                parts.append(f"*{module}*")
            return HoverInfo(markdown="\n\n".join(parts))
        return HoverInfo(markdown=None)


def signatures(ctx: PythonContext) -> SignatureList:
    with _LOCK:
        script, line, column = _script(ctx)
        try:
            sigs = script.get_signatures(line, column)
        except Exception:
            return SignatureList(signatures=[])
        return SignatureList(
            signatures=[
                SignatureInfo(
                    label=s.to_string(),
                    active_parameter=s.index,
                    doc=(s.docstring(raw=True) or None) and s.docstring(raw=True)[: _max_doc()],
                )
                for s in sigs
            ]
        )
