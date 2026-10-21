"""Code mode: a hand-edited DAG file synced back into the visual spec.

The edited Python is first parsed (syntax errors are reported with their position without
running anything), then imported with the Python importer (an isolated subprocess executes
it). What the code cannot carry — canvas positions, labels, icons, sticky notes, template
links, node ids, AutoAir-only DAG fields — is taken over from the current document, matched
by task / group id, so an apply only changes what the code changed.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, Field

from autoair.importer.python_dag import PythonImportResult, import_python_source
from autoair.merge import SpecDiff, diff_specs
from autoair.spec import DagSource, DagSpec

# Fields of a node / the DAG that only exist on the canvas or in AutoAir (never in the code).
_CANVAS_NODE_FIELDS = ("position", "label", "icon", "template_ref")
_AUTOAIR_DAG_FIELDS = ("output_mode", "team", "environments")


class CodeProblem(BaseModel):
    severity: Literal["error", "warning"]
    message: str
    line: int | None = Field(default=None, description="1-based line in the code, if known.")
    column: int | None = Field(default=None, description="1-based column, if known.")


class CodeSyncResult(BaseModel):
    ok: bool = Field(description="A spec could be built from the code (warnings allowed).")
    spec: DagSpec | None = None
    dag_ids: list[str] = Field(default_factory=list, description="DAGs the file defines.")
    problems: list[CodeProblem] = Field(default_factory=list)
    diff: SpecDiff | None = Field(
        default=None, description="Changes against the current document (when sent)."
    )


def syntax_problem(source: str, filename: str = "dag.py") -> CodeProblem | None:
    try:
        ast.parse(source, filename=filename)
    except SyntaxError as exc:
        return CodeProblem(
            severity="error",
            message=f"SyntaxError: {exc.msg}",
            line=exc.lineno,
            column=exc.offset,
        )
    return None


def error_line(message: str, filename: str) -> int | None:
    """Line of the last traceback frame in ``filename`` (the user's file), if any."""
    lines = re.findall(rf'File "[^"]*{re.escape(filename)}", line (\d+)', message)
    if lines:
        return int(lines[-1])
    m = re.search(r"\(\S*" + re.escape(filename) + r", line (\d+)\)", message)
    return int(m.group(1)) if m else None


def short_error(message: str) -> str:
    """The exception line of a traceback ("NameError: name 'x' is not defined")."""
    lines = [ln.strip() for ln in message.strip().splitlines() if ln.strip()]
    if len(lines) > 1 and lines[0].startswith("Traceback"):
        return lines[-1]
    return message.strip()


def _walk(value: Any, fn: Callable[[dict[str, Any]], None]) -> None:
    if isinstance(value, dict):
        fn(value)
        for v in value.values():
            _walk(v, fn)
    elif isinstance(value, list):
        for v in value:
            _walk(v, fn)


def _key(node: dict[str, Any]) -> str | None:
    if node.get("kind") == "task_group":
        return f"g:{node.get('group_id')}"
    if node.get("kind") == "note":
        return None
    return f"t:{node.get('task_id')}"


def strip_note_docs(doc_md: str | None, notes: list[str]) -> str | None:
    """``doc_md`` without the text of notes included in the DAG docs.

    Codegen appends each such note to ``doc_md`` (joined by blank lines, see
    ``codegen.builder._doc_md``). The notes stay on the canvas, so their text is removed
    again on import; otherwise every apply would add another copy.
    """
    if not doc_md or not notes:
        return doc_md
    tail = "\n\n".join(notes)
    if doc_md == tail:
        return None
    if doc_md.endswith("\n\n" + tail):
        return doc_md[: -len(tail) - 2] or None
    # Edited around the notes: drop each note's text where it is a whole paragraph block.
    sep = "\n\n"
    text = sep + doc_md + sep
    for note in notes:
        block = sep + note + sep
        if block in text:
            text = text.replace(block, sep, 1)
    result = text[len(sep) : -len(sep)] if text.startswith(sep) else text
    if result.endswith(sep):
        result = result[: -len(sep)]
    return result or None


def carry_canvas_state(imported: DagSpec, current: DagSpec | None) -> DagSpec:
    """``imported`` with the canvas-only state of ``current`` (see the module docstring)."""
    if current is None:
        return imported
    cur = current.model_dump(mode="json")
    new = imported.model_dump(mode="json")
    by_key = {k: n for n in cur["nodes"] if (k := _key(n))}
    taken = {n["id"] for n in cur["nodes"]}
    ids: dict[str, str] = {}
    for node in new["nodes"]:
        old = by_key.get(_key(node) or "")
        if old is None:
            continue
        ids[node["id"]] = old["id"]
        for field in _CANVAS_NODE_FIELDS:
            if field in old and old[field] is not None:
                node[field] = old[field]
    # Imported ids that collide with a kept id (of another node) get a fresh one.
    used = set(ids.values())
    for node in new["nodes"]:
        if node["id"] not in ids and node["id"] in used | taken:
            n = 2
            while f"{node['id']}_{n}" in used | taken:
                n += 1
            ids[node["id"]] = f"{node['id']}_{n}"
        used.add(ids.get(node["id"], node["id"]))

    def remap(d: dict[str, Any]) -> None:
        if d.get("$type") == "xcom" and isinstance(d.get("node"), str):
            d["node"] = ids.get(d["node"], d["node"])

    for node in new["nodes"]:
        node["id"] = ids.get(node["id"], node["id"])
        if node.get("parent_group"):
            node["parent_group"] = ids.get(node["parent_group"], node["parent_group"])
        _walk({k: v for k, v in node.items() if k not in ("id", "parent_group")}, remap)
    for edge in new["edges"]:
        edge["source"] = ids.get(edge["source"], edge["source"])
        edge["target"] = ids.get(edge["target"], edge["target"])
        edge["id"] = None
    # Sticky notes live on the canvas only.
    kept = {n["id"] for n in new["nodes"]}
    groups = {n["id"] for n in new["nodes"] if n.get("kind") == "task_group"}
    for note in cur["nodes"]:
        if note.get("kind") == "note" and note["id"] not in kept:
            if note.get("parent_group") not in groups:
                note["parent_group"] = None
            new["nodes"].append(note)
    for field in _AUTOAIR_DAG_FIELDS:
        new[field] = cur.get(field, new.get(field))
    doc_notes = [
        n["text"]
        for n in new["nodes"]
        if n.get("kind") == "note" and n.get("include_in_docs") and n.get("text", "").strip()
    ]
    new["doc_md"] = strip_note_docs(new.get("doc_md"), doc_notes)
    return DagSpec.model_validate(new)


def _pick(result: PythonImportResult, dag_id: str | None) -> tuple[Any, list[CodeProblem]]:
    problems: list[CodeProblem] = []
    if not result.dags:
        return None, problems
    chosen = next((d for d in result.dags if d.dag_id == dag_id), None)
    if chosen is None:
        chosen = result.dags[0]
        if dag_id and len(result.dags) > 1:
            problems.append(
                CodeProblem(
                    severity="warning",
                    message=f"the file defines {len(result.dags)} DAGs and none is {dag_id!r}: "
                    f"using {chosen.dag_id!r}",
                )
            )
    elif len(result.dags) > 1:
        others = ", ".join(d.dag_id for d in result.dags if d is not chosen)
        problems.append(
            CodeProblem(severity="warning", message=f"other DAGs in the file are ignored: {others}")
        )
    return chosen, problems


def sync_code(
    source: str,
    *,
    dag_id: str | None = None,
    current: DagSpec | None = None,
    filename: str = "dag.py",
    timeout: float = 90,
    importer: Callable[..., PythonImportResult] = import_python_source,
) -> CodeSyncResult:
    """Parse + import ``source`` and build the spec to apply (see the module docstring)."""
    syntax = syntax_problem(source, filename)
    if syntax is not None:
        return CodeSyncResult(ok=False, problems=[syntax])
    result = importer(source, filename, timeout=timeout)
    problems = [
        CodeProblem(severity="error", message=short_error(e), line=error_line(e, filename))
        for e in result.import_errors
    ]
    chosen, picked = _pick(result, dag_id)
    problems += picked
    dag_ids = [d.dag_id for d in result.dags]
    if chosen is None:
        if not problems:
            problems.append(CodeProblem(severity="error", message="no DAG is defined in the code"))
        return CodeSyncResult(ok=False, dag_ids=dag_ids, problems=problems)
    problems += [CodeProblem(severity="warning", message=w) for w in chosen.warnings]
    if chosen.spec is None:
        message = chosen.error or "the DAG cannot be converted"
        problems.append(CodeProblem(severity="error", message=message))
        return CodeSyncResult(ok=False, dag_ids=dag_ids, problems=problems)
    spec = carry_canvas_state(chosen.spec, current)
    diff = (
        diff_specs(
            current.model_copy(update={"source": None}), spec.model_copy(update={"source": None})
        )
        if current is not None
        else None
    )
    # The applied code is now the DAG's hand-written file: deploys preserve it (source
    # policy auto) instead of regenerating it. A file defining several DAGs cannot be kept
    # as the file of one of them.
    from autoair.preserve.service import mark_hand_written

    keep = source if len(result.dags) == 1 else None
    edited_from = current.source if current is not None and current.source else DagSource()
    notes: list[str] = []
    spec = mark_hand_written(spec, keep, notes, edited_from=edited_from)
    problems += [CodeProblem(severity="warning", message=n) for n in notes]
    ok = not any(p.severity == "error" for p in problems)
    return CodeSyncResult(ok=ok, spec=spec, dag_ids=dag_ids, problems=problems, diff=diff)


__all__ = [
    "CodeProblem",
    "CodeSyncResult",
    "carry_canvas_state",
    "error_line",
    "short_error",
    "strip_note_docs",
    "sync_code",
    "syntax_problem",
]
