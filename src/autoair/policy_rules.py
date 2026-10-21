"""Declarative organisation policy rules (``policy.rules``) and the OPA client options.

A rule is data, never code: a ``match`` that selects the DAGs (environment, team, schedule,
tags, dag_id) and optionally the tasks (operator, parameters) it applies to, and a ``check``
(also accepted as ``assert``) from a closed set of assertions. Validation evaluates the rules
(:mod:`autoair.validation.policy_rules`) and reports each violation as a ``policy_rule``
problem in the Problems panel. See docs/policy.md for the language and examples::

    policy:
      rules:
        - id: no-bash-in-prod
          message: BashOperator is forbidden in prod
          match: {environments: [prod], operators: [BashOperator]}
          check: forbid
        - id: prod-retries
          match: {environments: [prod]}
          check: {type: min_retries, value: 1}

This module only depends on pydantic so the settings sections can import it.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)


class _Rule(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# --------------------------------------------------------------------------- match


class ParamCondition(_Rule):
    """A condition over one task parameter (operator ``params``, TaskFlow ``arguments`` or
    ``decorator_kwargs``). Without ``equals``/``one_of``/``pattern``/``present`` it only
    requires the parameter to be set."""

    name: str = Field(min_length=1, description="Parameter name, e.g. bash_command, conn_id.")
    present: bool | None = Field(
        default=None, description="true: the parameter must be set; false: it must be unset."
    )
    equals: Any = Field(default=None, description="The value must equal this (JSON).")
    one_of: list[Any] | None = Field(default=None, description="The value must be one of these.")
    pattern: str | None = Field(
        default=None, description="Regex searched in the value (strings; others as JSON)."
    )

    @field_validator("pattern")
    @classmethod
    def _regex(cls, value: str | None) -> str | None:
        if value:
            import re

            try:
                re.compile(value)
            except re.error as exc:
                raise ValueError(f"invalid regular expression {value!r}: {exc}") from exc
        return value


class RuleMatch(_Rule):
    """Which DAGs (and tasks) a rule applies to. Every given selector must match; a list
    matches when any of its entries does. Empty: everything."""

    environments: list[str] = Field(
        default_factory=list,
        description="fnmatch patterns of the target environment (dev, staging, prod…). A rule "
        "with environments only applies when validation knows the environment (deploy, "
        "the builder's environment selector or default_environment).",
    )
    teams: list[str] = Field(
        default_factory=list, description="fnmatch patterns of the DAG's team (multi-team)."
    )
    dag_ids: list[str] = Field(default_factory=list, description="fnmatch patterns of dag_id.")
    tags: list[str] = Field(
        default_factory=list, description="The DAG carries at least one of these tags."
    )
    schedule: list[str] = Field(
        default_factory=list,
        description="Schedules: a preset ('@daily', also matching the equivalent cron "
        "'0 0 * * *'), a cron expression, or a schedule type (none, preset, cron, interval, "
        "multi_cron, events, assets, assets_or_time, assets_and_time, partitioned_cron, "
        "partitioned_assets).",
    )
    operators: list[str] = Field(
        default_factory=list,
        description="Task selector. fnmatch patterns of the operator class path; a pattern "
        "without a dot matches the class name (BashOperator). TaskFlow tasks are '@task' or "
        "'@task.<decorator>' (e.g. @task.bash), extension nodes 'custom:<handler>'.",
    )
    params: list[ParamCondition] = Field(
        default_factory=list, description="Task selector: conditions on task parameters."
    )

    @property
    def selects_tasks(self) -> bool:
        return bool(self.operators or self.params)


# --------------------------------------------------------------------------- checks


class ForbidCheck(_Rule):
    """What the match selects is not allowed (the tasks when the match has task selectors,
    otherwise the DAG)."""

    type: Literal["forbid"] = "forbid"


class RequireOwnerCheck(_Rule):
    """Every selected task has an owner (its own or ``default_args.owner``)."""

    type: Literal["require_owner"] = "require_owner"


class MinRetriesCheck(_Rule):
    """Every selected task retries at least ``value`` times (task or ``default_args``)."""

    type: Literal["min_retries"] = "min_retries"
    value: int = Field(ge=0)


class CatchupCheck(_Rule):
    """The DAG's ``catchup`` must be ``value``."""

    type: Literal["catchup"] = "catchup"
    value: bool = False


class TagsIncludeCheck(_Rule):
    """The DAG's tags include all of ``tags`` (and its team when ``team`` is true)."""

    type: Literal["tags_include"] = "tags_include"
    tags: list[str] = Field(default_factory=list)
    team: bool = Field(
        default=False, description="Also require the DAG's team name as a tag (when it has one)."
    )


class RequireDescriptionCheck(_Rule):
    """The DAG has a description or doc_md."""

    type: Literal["require_description"] = "require_description"


class MaxTasksCheck(_Rule):
    """At most ``value`` (enabled) tasks."""

    type: Literal["max_tasks"] = "max_tasks"
    value: int = Field(ge=0)


class RequireParamCheck(_Rule):
    """Every selected task sets parameter ``name``."""

    type: Literal["require_param"] = "require_param"
    name: str = Field(min_length=1)


class ParamValueCheck(_Rule):
    """Every selected task's parameter satisfies the condition (``equals``/``one_of``/
    ``pattern``/``present``)."""

    type: Literal["param"] = "param"
    condition: ParamCondition


DagField = Literal[
    "description",
    "doc_md",
    "catchup",
    "max_active_runs",
    "max_active_tasks",
    "dagrun_timeout_seconds",
    "is_paused_upon_creation",
    "fail_fast",
    "max_consecutive_failed_dag_runs",
    "render_template_as_native_obj",
    "rerun_with_latest_version",
    "timezone",
    "start_date",
    "end_date",
    "team",
    "default_args.owner",
    "default_args.retries",
    "default_args.retry_delay_seconds",
    "default_args.execution_timeout_seconds",
    "default_args.email",
    "default_args.pool",
    "default_args.queue",
]

TaskField = Literal[
    "owner",
    "retries",
    "retry_delay_seconds",
    "execution_timeout_seconds",
    "pool",
    "pool_slots",
    "queue",
    "priority_weight",
    "doc_md",
    "executor",
    "max_active_tis_per_dagrun",
    "depends_on_past",
    "trigger_rule",
]


class DagFieldCheck(_Rule):
    """A DAG field must be set (``present``), equal ``equals`` or be at least/most a number."""

    type: Literal["dag_field"] = "dag_field"
    field: DagField
    present: bool | None = None
    equals: Any = None
    min: float | None = None
    max: float | None = None


class TaskFieldCheck(_Rule):
    """A field of every selected task (task value, else the DAG's default_args for owner,
    retries, retry_delay_seconds, execution_timeout_seconds, pool, queue, depends_on_past)."""

    type: Literal["task_field"] = "task_field"
    field: TaskField
    present: bool | None = None
    equals: Any = None
    min: float | None = None
    max: float | None = None


RuleCheck = Annotated[
    ForbidCheck
    | RequireOwnerCheck
    | MinRetriesCheck
    | CatchupCheck
    | TagsIncludeCheck
    | RequireDescriptionCheck
    | MaxTasksCheck
    | RequireParamCheck
    | ParamValueCheck
    | DagFieldCheck
    | TaskFieldCheck,
    Field(discriminator="type"),
]


class PolicyRule(_Rule):
    """One declarative rule: where it applies (``match``) and what must hold (``check``)."""

    id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,80}$", description="Unique rule id.")
    message: str | None = Field(
        default=None,
        max_length=500,
        description="Problem text (default: generated from the check).",
    )
    description: str | None = Field(default=None, max_length=2000, description="Documentation.")
    severity: Literal["error", "warning"] = Field(
        default="error", description="error blocks deploys; warning is reported only."
    )
    enabled: bool = True
    match: RuleMatch = Field(default_factory=RuleMatch)
    check: RuleCheck = Field(validation_alias=AliasChoices("check", "assert"))

    @field_validator("check", mode="before")
    @classmethod
    def _shorthand(cls, value: Any) -> Any:
        # ``check: forbid`` == ``check: {type: forbid}``
        return {"type": value} if isinstance(value, str) else value


def unique_rule_ids(rules: list[PolicyRule]) -> list[PolicyRule]:
    seen: set[str] = set()
    for rule in rules:
        if rule.id in seen:
            raise ValueError(f"duplicate policy rule id {rule.id!r}")
        seen.add(rule.id)
    return rules


# --------------------------------------------------------------------------- OPA


class OpaOptions(BaseModel):
    """How validation asks an Open Policy Agent (resolved from ``policy.opa_*``)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    url: str
    timeout: float = 2.0
    fail_mode: Literal["open", "closed"] = "open"
    token: SecretStr | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    cache_seconds: float = 5.0

    @model_validator(mode="after")
    def _http(self) -> OpaOptions:
        if not self.url.startswith(("http://", "https://")):
            raise ValueError("policy.opa_url must be an http(s) URL")
        return self
