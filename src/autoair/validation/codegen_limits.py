"""Values the generated DAG file cannot represent: durations a ``datetime.timedelta`` cannot
hold and task function names that would shadow a module the file imports.

Both would otherwise produce a DAG that fails at parse time or at run time (``OverflowError``
from ``timedelta(seconds=inf)``, ``AttributeError`` on ``os.path`` once a task function named
``os`` replaced the module), so they are reported here with the node and field."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from autoair.codegen.context import PLAIN_MODULES, spec_inline_import_names, timedelta_problem
from autoair.codegen.timedeltas import requires_timedelta
from autoair.spec import DagSpec, OperatorNode, PythonTaskNode

if TYPE_CHECKING:
    from autoair.validation import Problem

# Node fields holding user values (operator arguments, handler config): only tagged
# ``timedelta`` values in them are durations, a ``seconds`` key may be anything.
_VALUE_FIELDS = frozenset(
    {"params", "arguments", "decorator_kwargs", "config", "executor_config", "mapping"}
)


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def duration_problems(spec: DagSpec) -> list[Problem]:
    from autoair.validation import Problem

    problems: list[Problem] = []

    def check(seconds: Any, path: str, node_id: str | None) -> None:
        if not _number(seconds):
            return
        reason = timedelta_problem(float(seconds))
        if reason is not None:
            problems.append(
                Problem(
                    severity="error",
                    code="duration",
                    message=f"{path}: {reason}",
                    node_id=node_id,
                    field=path,
                )
            )

    def values(value: Any, path: str, node_id: str | None) -> None:
        if isinstance(value, dict):
            if value.get("$type") == "timedelta":
                check(value.get("seconds"), path, node_id)
                return
            for k, v in value.items():
                values(v, f"{path}.{k}", node_id)
        elif isinstance(value, list):
            for i, v in enumerate(value):
                values(v, f"{path}[{i}]", node_id)

    def fields(value: Any, path: str, node_id: str | None) -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                sub = f"{path}.{k}" if path else str(k)
                if k == "seconds" or str(k).endswith("_seconds"):
                    check(v, sub, node_id)
                elif node_id is not None and not path and k in _VALUE_FIELDS:
                    values(v, sub, node_id)
                else:
                    fields(v, sub, node_id)
        elif isinstance(value, list):
            for i, v in enumerate(value):
                fields(v, f"{path}[{i}]", node_id)

    dump = spec.model_dump(by_alias=True, exclude={"nodes", "edges", "raw_code", "source"})
    fields(dump, "", None)
    for node in spec.nodes:
        fields(node.model_dump(by_alias=True), "", node.id)
        if isinstance(node, OperatorNode):
            for name, value in node.params.items():
                # A plain number for a timedelta-only argument is generated as a timedelta.
                if _number(value) and requires_timedelta(node.operator, name):
                    check(value, f"params.{name}", node.id)
    return problems


def function_name_problems(spec: DagSpec) -> list[Problem]:
    """A task function named like a module of the generated file is renamed there (the
    task id stays); say so, since the name in the code then differs from the inspector."""
    from autoair.validation import Problem

    taken = set(PLAIN_MODULES) | {"dag"} | spec_inline_import_names(spec)
    return [
        Problem(
            severity="warning",
            code="function_name_reserved",
            message=f"function name {n.function_name!r} is also a name the generated DAG file "
            "imports; the function is renamed in the generated code so it does not shadow the "
            "import (choose another name to keep it)",
            node_id=n.id,
            field="function_name",
        )
        for n in spec.nodes
        if isinstance(n, PythonTaskNode) and n.function_name in taken
    ]
