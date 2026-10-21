"""Spec checking shared by the AI generator and the MCP server.

Wraps Pydantic parsing + :func:`autoair.validation.validate_spec` and attaches a
JSON-pointer ``path`` (e.g. ``/nodes/2/params/bash_command``) to every problem so
LLM agents can locate and fix the offending value.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ValidationError

from autoair.catalog import OperatorCatalog
from autoair.registry import HandlerRegistry
from autoair.spec import DagSpec, OperatorNode
from autoair.validation import DEFAULT_POLICY, Policy, Problem, validate_spec
from autoair.validation.dagbag_check import ImportCheckResult

_NODE_KINDS = frozenset({"operator", "python_task", "task_group", "custom", "note"})
_TAGGED_PARENTS = frozenset({"schedule", "time", "expression", "items"})


class SpecProblem(Problem):
    """A validation problem plus a JSON pointer into the submitted spec document."""

    path: str | None = None


class SpecCheck(BaseModel):
    ok: bool
    spec: DagSpec | None = None
    problems: list[SpecProblem]
    import_check: ImportCheckResult | None = None


def _pointer(parts: Sequence[str | int]) -> str:
    return "/" + "/".join(str(p).replace("~", "~0").replace("/", "~1") for p in parts)


def _clean_loc(loc: Sequence[str | int]) -> list[str | int]:
    """Drop Pydantic's union-tag / validator segments so the loc mirrors the document."""
    out: list[str | int] = []
    for i, part in enumerate(loc):
        if isinstance(part, str):
            if "[" in part or part.startswith("function-"):
                continue
            prev = loc[i - 1] if i else None
            if (
                isinstance(prev, int)
                and len(out) >= 2
                and out[-2] == "nodes"
                and part in _NODE_KINDS
            ):
                continue
            tagged = prev in _TAGGED_PARENTS or (
                isinstance(prev, int) and part in ("asset", "alias")
            )
            # discriminator values of schedule / asset-expression unions
            if tagged and part not in ("expression", "time", "items", "asset", "name", "uri"):
                continue
        out.append(part)
    return out


def pydantic_problems(exc: ValidationError, data: object = None) -> list[SpecProblem]:
    nodes = data.get("nodes") if isinstance(data, Mapping) else None
    problems: list[SpecProblem] = []
    for err in exc.errors(include_url=False):
        loc = _clean_loc(list(err["loc"]))
        node_id: str | None = None
        if (
            len(loc) >= 2
            and loc[0] == "nodes"
            and isinstance(loc[1], int)
            and isinstance(nodes, list)
            and 0 <= loc[1] < len(nodes)
        ):
            raw = nodes[loc[1]]
            if isinstance(raw, Mapping) and isinstance(raw.get("id"), str):
                node_id = raw["id"]
        field = next((str(p) for p in reversed(loc) if isinstance(p, str)), None)
        problems.append(
            SpecProblem(
                severity="error",
                code="schema",
                message=f"{_pointer(loc) if loc else '/'}: {err['msg']}",
                node_id=node_id,
                field=field,
                path=_pointer(loc) if loc else "/",
            )
        )
    return problems


def with_paths(spec: DagSpec, problems: Sequence[Problem]) -> list[SpecProblem]:
    """Attach JSON pointers to :class:`Problem` objects produced by ``validate_spec``."""
    index = {n.id: i for i, n in enumerate(spec.nodes)}
    out: list[SpecProblem] = []
    for p in problems:
        path: str | None = None
        if p.node_id is not None and p.node_id in index:
            i = index[p.node_id]
            node = spec.nodes[i]
            if p.field is None:
                path = _pointer(["nodes", i])
            elif p.code == "reserved_param" or (
                p.code == "param" and isinstance(node, OperatorNode)
            ):
                path = _pointer(["nodes", i, "params", p.field])
            else:
                path = _pointer(["nodes", i, *p.field.split(".")])
        elif p.field is not None:
            path = _pointer(p.field.split("."))
        out.append(SpecProblem(**p.model_dump(), path=path))
    return out


def check_spec(
    data: Mapping[str, Any] | DagSpec,
    *,
    catalog: OperatorCatalog | None,
    registry: HandlerRegistry | None,
    policy: Policy = DEFAULT_POLICY,
    import_check: bool = False,
) -> SpecCheck:
    """Parse (if needed) and validate a spec; never raises for invalid input."""
    if isinstance(data, DagSpec):
        spec = data
    else:
        try:
            spec = DagSpec.model_validate(dict(data))
        except ValidationError as exc:
            return SpecCheck(ok=False, problems=pydantic_problems(exc, data))
    report = validate_spec(
        spec, catalog=catalog, registry=registry, policy=policy, import_check=import_check
    )
    return SpecCheck(
        ok=report.ok,
        spec=spec,
        problems=with_paths(spec, report.problems),
        import_check=report.import_check,
    )


def format_problems(problems: Sequence[Problem], limit: int = 40) -> str:
    lines = []
    for p in problems[:limit]:
        where = getattr(p, "path", None) or (f"node {p.node_id}" if p.node_id else "spec")
        lines.append(f"- [{p.severity}/{p.code}] {where}: {p.message}")
    if len(problems) > limit:
        lines.append(f"- ... and {len(problems) - limit} more")
    return "\n".join(lines)
