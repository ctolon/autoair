"""Structured findings shared by review / leak scan / optimise / fix.

A finding points at a location the editor can reveal (``node_id`` + ``field``, the same
convention as validation problems) and may carry a ``patch``: RFC 6902 operations
(``add`` / ``replace`` / ``remove``) against the DagSpec JSON document. Pointers may address
nodes by id - ``/nodes/@extract/retries`` - which is stable across edits and far easier
for a model to produce than array indexes.
"""

from __future__ import annotations

import copy
import hashlib
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, Field

from autoair.ai.problems import SpecProblem, check_spec
from autoair.ai.usage import Usage
from autoair.catalog import OperatorCatalog
from autoair.registry import HandlerRegistry
from autoair.spec import DagSpec
from autoair.validation import DEFAULT_POLICY, Policy

Severity = Literal["critical", "high", "medium", "low", "info"]
Category = Literal[
    "secret",
    "security",
    "reliability",
    "performance",
    "cost",
    "correctness",
    "idempotency",
    "airflow3",
    "best_practice",
    "maintainability",
    "documentation",
]
SEVERITY_ORDER: dict[str, int] = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


class PatchOp(BaseModel):
    """One JSON Patch operation against the DagSpec document."""

    op: Literal["add", "replace", "remove"]
    path: str = Field(
        description="JSON pointer; address nodes as /nodes/@<node id>/... "
        "(e.g. /nodes/@load/retries, /default_args/retries)."
    )
    value: Any = Field(default=None, description="New value (omit for remove).")


class Finding(BaseModel):
    id: str = Field(description="Stable id (rule + location).")
    severity: Severity
    category: Category
    title: str = Field(description="One line, imperative or descriptive.")
    message: str = Field(description="Why it matters and what to do.")
    node_id: str | None = Field(default=None, description="Node to select in the editor.")
    field: str | None = Field(
        default=None, description="Field to focus: a node/DAG field name or an operator param."
    )
    path: str | None = Field(default=None, description="JSON pointer of the offending value.")
    rule: str | None = Field(default=None, description="Deterministic rule id, when not AI.")
    source: Literal["rule", "ai"] = "rule"
    evidence: str | None = Field(default=None, description="Offending snippet (secrets masked).")
    patch: list[PatchOp] = Field(default_factory=list)


class FindingsReport(BaseModel):
    kind: Literal["review", "leaks", "optimize", "fix"]
    summary: str = ""
    findings: list[Finding] = Field(default_factory=list)
    proposed_spec: DagSpec | None = Field(
        default=None, description="The spec with every finding's patch applied (when any)."
    )
    proposed_ok: bool | None = Field(
        default=None, description="Whether the proposed spec passes validation."
    )
    problems: list[SpecProblem] = Field(
        default_factory=list, description="Validation problems of the proposed spec."
    )
    skipped_patches: list[str] = Field(
        default_factory=list, description="Finding ids whose patch could not be applied."
    )
    ai_used: bool = False
    model: str | None = None
    usage: Usage | None = None
    notes: list[str] = Field(default_factory=list)


def finding_id(*parts: object) -> str:
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha1(raw.encode(), usedforsecurity=False).hexdigest()[:12]


def sort_findings(findings: Iterable[Finding]) -> list[Finding]:
    return sorted(findings, key=lambda f: (SEVERITY_ORDER[f.severity], f.node_id or "", f.title))


def dedupe(findings: Iterable[Finding]) -> list[Finding]:
    """Drop findings repeating an earlier one (same id, or same location + title)."""
    seen: set[str] = set()
    out: list[Finding] = []
    for f in findings:
        keys = {f.id, f"{f.node_id}|{f.field}|{f.title.lower()}"}
        if keys & seen:
            continue
        seen |= keys
        out.append(f)
    return out


# --------------------------------------------------------------------------- JSON patch


class PatchError(ValueError):
    pass


def spec_document(spec: DagSpec) -> dict[str, Any]:
    """The JSON document patches apply to."""
    return spec.model_dump(mode="json", by_alias=True, exclude_none=True)


def _unescape(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


def _tokens(path: str) -> list[str]:
    if path in ("", "/"):
        return []
    if not path.startswith("/"):
        raise PatchError(f"invalid JSON pointer {path!r}")
    return [_unescape(t) for t in path[1:].split("/")]


def _resolve_index(container: list[Any], token: str, *, allow_end: bool) -> int:
    if token.startswith("@"):
        node_id = token[1:]
        for i, item in enumerate(container):
            if isinstance(item, Mapping) and item.get("id") == node_id:
                return i
        raise PatchError(f"no node with id {node_id!r}")
    if token == "-" and allow_end:  # noqa: S105 - JSON pointer "end of array"
        return len(container)
    if not token.isdigit():
        raise PatchError(f"invalid array index {token!r}")
    index = int(token)
    if index > len(container) or (index == len(container) and not allow_end):
        raise PatchError(f"array index {index} out of range")
    return index


def resolve_pointer(document: Mapping[str, Any], path: str) -> str:
    """Rewrite ``@node_id`` segments into numeric indexes (standard JSON pointer)."""
    out: list[str] = []
    current: Any = document
    for token in _tokens(path):
        if isinstance(current, list):
            index = _resolve_index(current, token, allow_end=True)
            out.append(str(index))
            current = current[index] if index < len(current) else None
        elif isinstance(current, Mapping):
            out.append(token.replace("~", "~0").replace("/", "~1"))
            current = current.get(token)
        else:
            out.append(token.replace("~", "~0").replace("/", "~1"))
            current = None
    return "/" + "/".join(out) if out else "/"


def apply_patch(document: Mapping[str, Any], ops: Sequence[PatchOp]) -> dict[str, Any]:
    """Apply ``ops`` to a deep copy of ``document`` (all-or-nothing)."""
    doc: dict[str, Any] = copy.deepcopy(dict(document))
    for op in ops:
        tokens = _tokens(op.path)
        if not tokens:
            raise PatchError("cannot replace the whole document")
        parent: Any = doc
        for token in tokens[:-1]:
            if isinstance(parent, list):
                parent = parent[_resolve_index(parent, token, allow_end=False)]
            elif isinstance(parent, dict):
                if token not in parent:
                    if op.op == "add":
                        parent[token] = {}
                    else:
                        raise PatchError(f"path {op.path!r} does not exist")
                parent = parent[token]
            else:
                raise PatchError(f"path {op.path!r} traverses a scalar")
        last = tokens[-1]
        if isinstance(parent, list):
            index = _resolve_index(parent, last, allow_end=op.op == "add")
            if op.op == "add":
                parent.insert(index, copy.deepcopy(op.value))
            elif op.op == "replace":
                parent[index] = copy.deepcopy(op.value)
            else:
                del parent[index]
        elif isinstance(parent, dict):
            if op.op == "remove":
                if last not in parent:
                    raise PatchError(f"path {op.path!r} does not exist")
                del parent[last]
            elif op.op == "replace" and last not in parent:
                parent[last] = copy.deepcopy(op.value)  # lenient: replace == add for objects
            else:
                parent[last] = copy.deepcopy(op.value)
        else:
            raise PatchError(f"path {op.path!r} traverses a scalar")
    return doc


class AppliedPatches(BaseModel):
    spec: DagSpec | None
    ok: bool
    problems: list[SpecProblem]
    applied: list[str]
    skipped: list[str]


def apply_findings(
    spec: DagSpec,
    findings: Sequence[Finding],
    *,
    catalog: OperatorCatalog | None,
    registry: HandlerRegistry | None,
    policy: Policy = DEFAULT_POLICY,
    only: Sequence[str] | None = None,
) -> AppliedPatches:
    """Apply the patches of ``findings`` (or those listed in ``only``) one finding at a
    time; a finding whose patch fails or breaks the document schema is skipped."""
    wanted = set(only) if only is not None else None
    document = spec_document(spec)
    applied: list[str] = []
    skipped: list[str] = []
    for finding in findings:
        if not finding.patch or (wanted is not None and finding.id not in wanted):
            continue
        try:
            candidate = apply_patch(document, finding.patch)
            DagSpec.model_validate(candidate)
        except (PatchError, ValueError, TypeError, KeyError, IndexError):
            skipped.append(finding.id)
            continue
        document = candidate
        applied.append(finding.id)
    check = check_spec(document, catalog=catalog, registry=registry, policy=policy)
    return AppliedPatches(
        spec=check.spec, ok=check.ok, problems=check.problems, applied=applied, skipped=skipped
    )


def attach_proposal(
    report: FindingsReport,
    spec: DagSpec,
    *,
    catalog: OperatorCatalog | None,
    registry: HandlerRegistry | None,
    policy: Policy = DEFAULT_POLICY,
) -> FindingsReport:
    if not any(f.patch for f in report.findings):
        return report
    result = apply_findings(
        spec, report.findings, catalog=catalog, registry=registry, policy=policy
    )
    report.proposed_spec = result.spec
    report.proposed_ok = result.ok
    report.problems = result.problems
    report.skipped_patches = result.skipped
    return report
