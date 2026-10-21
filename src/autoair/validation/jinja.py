"""Static validation of Jinja templates in templated operator fields.

Templates are parsed (never rendered) with a sandboxed environment. We report syntax
errors and names that are not part of Airflow's task context, and check
``params.X`` references against the DAG's declared params.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from jinja2 import TemplateSyntaxError, nodes
from jinja2.sandbox import SandboxedEnvironment

# Airflow 3 task context (templates-ref) + common Jinja globals.
CONTEXT_NAMES = frozenset(
    {
        "conn",
        "dag",
        "dag_run",
        "data_interval_end",
        "data_interval_start",
        "ds",
        "ds_nodash",
        "expanded_ti_count",
        "inlet_events",
        "inlets",
        "logical_date",
        "macros",
        "map_index_template",
        "outlet_events",
        "outlets",
        "params",
        "prev_data_interval_end_success",
        "prev_data_interval_start_success",
        "prev_end_date_success",
        "prev_start_date_success",
        "run_id",
        "start_date",
        "task",
        "task_instance",
        "task_instance_key_str",
        "test_mode",
        "ti",
        "triggering_asset_events",
        "ts",
        "ts_nodash",
        "ts_nodash_with_tz",
        "var",
        "partition_key",
        "partition_date",
        "deadline",
        "task_reschedule_count",
        # jinja builtins commonly used
        "range",
        "dict",
        "lipsum",
        "cycler",
        "joiner",
        "namespace",
        "loop",
        "true",
        "false",
        "none",
        "True",
        "False",
        "None",
    }
)
# Context keys Airflow 3 sets only when the run has a logical date. The task runner adds them
# in one block guarded by ``if logical_date := coerce_datetime(dag_run.logical_date)``
# (``airflow/sdk/execution_time/task_runner.py``, ``RuntimeTaskInstance.get_template_context``,
# identical in 3.0-3.4). Without a logical date (asset-triggered runs, runs triggered with
# ``logical_date: null``) the keys are missing, so a template using them fails under the DAG's
# default ``StrictUndefined``. ``task_instance_key_str``, ``prev_start_date_success`` and
# ``prev_end_date_success`` are set for every run and are not listed. The frontend reads this
# list from ``frontend/src/lib/generated/jinjaContext.gen.ts``, written by
# ``scripts/gen_shared_constants.py``.
LOGICAL_DATE_NAMES: tuple[str, ...] = (
    "logical_date",
    "ds",
    "ds_nodash",
    "ts",
    "ts_nodash",
    "ts_nodash_with_tz",
    "data_interval_start",
    "data_interval_end",
    "prev_data_interval_start_success",
    "prev_data_interval_end_success",
)
TIME_NAMES = frozenset(LOGICAL_DATE_NAMES)
# ``dag_run`` attributes that are None without a logical date (``{{ dag_run.logical_date }}``).
DAG_RUN_LOGICAL_DATE_ATTRS: tuple[str, ...] = (
    "logical_date",
    "data_interval_start",
    "data_interval_end",
)

_env = SandboxedEnvironment()


@dataclass(frozen=True)
class TemplateIssue:
    severity: str  # "error" | "warning"
    message: str


def looks_like_template(value: str) -> bool:
    return "{{" in value or "{%" in value or "{#" in value


def check_template(
    source: str,
    *,
    declared_params: Iterable[str] = (),
    time_available: bool = True,
    extra_names: Iterable[str] = (),
) -> list[TemplateIssue]:
    """Syntax errors and unknown variables of a Jinja template. ``extra_names`` are known
    besides Airflow's context (``AUTOAIR_POLICY__JINJA_NAMES_EXTRA``: user macros…)."""
    try:
        tree = _env.parse(source)
    except TemplateSyntaxError as exc:
        return [TemplateIssue("error", f"template syntax error (line {exc.lineno}): {exc.message}")]
    issues: list[TemplateIssue] = []
    params = set(declared_params)
    assigned = {n.name for n in tree.find_all(nodes.Name) if n.ctx in ("store", "param")}
    assigned.update(extra_names)
    for name_node in tree.find_all(nodes.Name):
        if name_node.ctx != "load":
            continue
        name = name_node.name
        if name in assigned or name in CONTEXT_NAMES:
            if name in TIME_NAMES and not time_available and name not in assigned:
                issues.append(
                    TemplateIssue(
                        "warning",
                        f"'{name}' is undefined for runs without a logical date "
                        "(asset-triggered or manual runs in Airflow 3)",
                    )
                )
            continue
        issues.append(TemplateIssue("warning", f"unknown template variable '{name}'"))
    for attr in tree.find_all(nodes.Getattr):
        if (
            not time_available
            and isinstance(attr.node, nodes.Name)
            and attr.node.name == "dag_run"
            and "dag_run" not in assigned
            and attr.attr in DAG_RUN_LOGICAL_DATE_ATTRS
        ):
            issues.append(
                TemplateIssue(
                    "warning",
                    f"'dag_run.{attr.attr}' is None for runs without a logical date "
                    "(asset-triggered or manual runs in Airflow 3)",
                )
            )
        if (
            isinstance(attr.node, nodes.Name)
            and attr.node.name == "params"
            and params
            and attr.attr not in params
        ):
            issues.append(TemplateIssue("error", f"params.{attr.attr} is not a declared DAG param"))
    # params['x'] form
    for item in tree.find_all(nodes.Getitem):
        if (
            isinstance(item.node, nodes.Name)
            and item.node.name == "params"
            and isinstance(item.arg, nodes.Const)
            and isinstance(item.arg.value, str)
            and params
            and item.arg.value not in params
        ):
            issues.append(
                TemplateIssue("error", f"params['{item.arg.value}'] is not a declared DAG param")
            )
    # de-duplicate, keep order
    seen: set[tuple[str, str]] = set()
    unique: list[TemplateIssue] = []
    for i in issues:
        if (i.severity, i.message) not in seen:
            seen.add((i.severity, i.message))
            unique.append(i)
    return unique
