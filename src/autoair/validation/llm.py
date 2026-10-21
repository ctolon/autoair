"""Checks of apache-airflow-providers-common-ai tasks: structured outputs, toolsets and the
``@task.llm`` / ``@task.agent`` decorators."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ValidationError

from autoair.spec import DagSpec, OperatorNode, OutputModel, PythonTaskNode, ToolsetSpec
from autoair.spec.models import iter_tagged

if TYPE_CHECKING:
    from autoair.validation import Problem

LLM_DECORATORS = frozenset({"llm", "agent", "llm_branch", "llm_sql"})
_TAGGED: dict[str, type[BaseModel]] = {"output_model": OutputModel, "toolset": ToolsetSpec}


def _values(node: OperatorNode | PythonTaskNode) -> dict[str, Any]:
    if isinstance(node, OperatorNode):
        return dict(node.params)
    return {**node.arguments, **node.decorator_kwargs}


def llm_problems(spec: DagSpec) -> list[Problem]:
    from autoair.validation import _err

    problems: list[Problem] = []
    for n in spec.task_nodes():
        if not isinstance(n, (OperatorNode, PythonTaskNode)):
            continue
        for name, value in _values(n).items():
            for tag, model in _TAGGED.items():
                for raw in iter_tagged(value, tag):
                    try:
                        parsed = model.model_validate(raw)
                    except ValidationError as exc:
                        msg = exc.errors()[0]["msg"] if exc.errors() else str(exc)
                        problems.append(_err(tag, f"invalid {tag} in {name!r}: {msg}", n.id, name))
                        continue
                    if isinstance(parsed, ToolsetSpec) and parsed.allow_writes:
                        problems.append(
                            _err(
                                "toolset",
                                f"the SQL toolset on {parsed.conn_id!r} lets the agent modify "
                                "data (allow_writes)",
                                n.id,
                                name,
                                severity="warning",
                            )
                        )
        if (
            isinstance(n, PythonTaskNode)
            and n.decorator in LLM_DECORATORS
            and not n.decorator_kwargs.get("llm_conn_id")
        ):
            problems.append(
                _err(
                    "decorator",
                    f"@task.{n.decorator} needs llm_conn_id (the LLM connection)",
                    n.id,
                    "decorator_kwargs",
                )
            )
    return problems
