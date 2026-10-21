"""Versioned, strictly-typed DAG specification — the single source of truth.

The canvas (frontend), code generator, runtime loader and exporters all consume
:class:`DagSpec`. Every model forbids unknown fields so typos never pass silently.
"""

from __future__ import annotations

import datetime
import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SPEC_VERSION: Literal[3] = 3

# Airflow's KEY_REGEX for dag_id / task_id / group_id.
KEY_PATTERN = r"^[A-Za-z0-9_.-]+$"
KEY_RE = re.compile(KEY_PATTERN)
# Python identifier-ish, used for keyword arguments.
IDENT_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*$"
# Fully-qualified dotted class path.
FQN_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)+$"

Key = Annotated[str, Field(pattern=KEY_PATTERN, min_length=1, max_length=250)]
NodeId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]+$", min_length=1, max_length=100)]
Identifier = Annotated[str, Field(pattern=IDENT_PATTERN, max_length=100)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=False, populate_by_name=True)


# --------------------------------------------------------------------------- values
# Parameter values are JSON plus a few tagged, explicitly-typed objects that cannot
# be expressed in JSON. Tagged values use a ``$type`` discriminator.


class XComRef(StrictModel):
    """Reference to the output (XCom) of another node — becomes ``XComArg``."""

    type: Literal["xcom"] = Field(default="xcom", alias="$type")
    node: NodeId
    key: str | None = Field(default=None, description="Index into the output, e.g. a dict key.")


class TimedeltaValue(StrictModel):
    type: Literal["timedelta"] = Field(default="timedelta", alias="$type")
    seconds: float = Field(ge=0)


class DatetimeValue(StrictModel):
    type: Literal["datetime"] = Field(default="datetime", alias="$type")
    value: datetime.datetime


PYTHON_PATH_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)+$"


class PythonRef(StrictModel):
    """An importable Python object (function, class, constant): ``pkg.module.name``.

    Used for parameters that take Python objects (``python_callable``, ``producer_function``,
    ``stub_class``…). The generated code imports the name and passes it; running referenced
    code is privileged, like Python task nodes (``allow_python_code``).
    """

    type: Literal["import"] = Field(default="import", alias="$type")
    path: str = Field(pattern=PYTHON_PATH_PATTERN, max_length=500)

    @property
    def module(self) -> str:
        return self.path.rpartition(".")[0]

    @property
    def name(self) -> str:
        return self.path.rpartition(".")[2]


class InlineFunction(StrictModel):
    """A Python function written in the builder (``def …``), passed as the parameter.

    Top-level ``import`` statements are allowed before the ``def``; the function is emitted
    at module level of the generated DAG (privileged, like Python task nodes).
    """

    type: Literal["function"] = Field(default="function", alias="$type")
    source: str = Field(max_length=200_000)


FILE_NAME_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,120}$"


class FileAsset(StrictModel):
    """A script written in the builder (PySpark job, shell script, SQL…). The generated DAG
    embeds it and writes it to a content-addressed temp file when the DAG is parsed (on
    every worker), passing the file's path — or ``<run_with> <path>`` — as the value."""

    type: Literal["file"] = Field(default="file", alias="$type")
    name: str = Field(pattern=FILE_NAME_PATTERN, examples=["job.py", "load.sh"])
    content: str = Field(max_length=1_000_000)
    run_with: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9_./-]{1,40}$",
        description="Interpreter to prefix, e.g. bash → value 'bash /tmp/…/load.sh '.",
    )


OutputFieldType = Literal[
    "str", "int", "float", "bool", "list[str]", "list[int]", "list[float]", "dict"
]


class OutputField(StrictModel):
    """One field of a structured LLM output (``OutputModel``)."""

    name: Identifier
    type: OutputFieldType = "str"
    description: str | None = Field(
        default=None, max_length=2000, description="Shown to the model as the field's meaning."
    )
    required: bool = Field(default=True, description="false: optional (defaults to None).")
    choices: list[str] = Field(
        default_factory=list,
        max_length=200,
        description="Allowed values of a str field (becomes typing.Literal[...]).",
    )

    @model_validator(mode="after")
    def _choices_on_str(self) -> OutputField:
        if self.choices and self.type != "str":
            raise ValueError("choices are only allowed on str fields")
        return self


class OutputModel(StrictModel):
    """Structured output of a common.ai LLM / agent task (``output_type``): the generated
    DAG defines a pydantic ``BaseModel`` with these fields at module level and passes the
    class. Field types come from a closed set, so the class contains no user code."""

    type: Literal["output_model"] = Field(default="output_model", alias="$type")
    name: str = Field(pattern=r"^[A-Z][A-Za-z0-9_]{0,99}$", examples=["Sentiment"])
    description: str | None = Field(default=None, max_length=2000)
    fields: list[OutputField] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def _unique(self) -> OutputModel:
        names = [f.name for f in self.fields]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"duplicate output fields: {', '.join(dupes)}")
        return self


class ToolsetSpec(StrictModel):
    """A common.ai agent toolset bound to an Airflow connection: ``SQLToolset`` (kind sql)
    or ``MCPToolset`` (kind mcp). The connection is only resolved when the task runs."""

    type: Literal["toolset"] = Field(default="toolset", alias="$type")
    kind: Literal["sql", "mcp"]
    conn_id: Key = Field(description="db_conn_id (sql) or mcp_conn_id (mcp).")
    allowed_tables: list[str] | None = Field(default=None, description="sql: table allowlist.")
    db_schema: str | None = Field(default=None, description="sql: default schema.")
    allow_writes: bool = Field(default=False, description="sql: allow INSERT/UPDATE/DELETE.")
    max_rows: int | None = Field(default=None, ge=1, description="sql: rows per query result.")
    tool_prefix: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9_]{1,40}$", description="mcp: tool name prefix."
    )

    @model_validator(mode="after")
    def _kind_fields(self) -> ToolsetSpec:
        sql_only = (
            self.allowed_tables is not None
            or self.db_schema is not None
            or self.allow_writes
            or self.max_rows is not None
        )
        if self.kind == "mcp" and sql_only:
            raise ValueError("allowed_tables/db_schema/allow_writes/max_rows are sql options")
        if self.kind == "sql" and self.tool_prefix is not None:
            raise ValueError("tool_prefix is an mcp option")
        return self


TaggedValue = Annotated[
    XComRef
    | TimedeltaValue
    | DatetimeValue
    | PythonRef
    | InlineFunction
    | FileAsset
    | OutputModel
    | ToolsetSpec,
    Field(discriminator="type"),
]
# Recursive JSON value; tagged dicts are recognised by the codegen / loader.
Value = Any


# --------------------------------------------------------------------------- assets


class AssetWatcherSpec(StrictModel):
    """Event-driven scheduling: ``AssetWatcher(trigger=MessageQueueTrigger(...))``."""

    name: Key
    scheme: str = Field(
        pattern=r"^[a-z0-9+.-]+$",
        examples=["sqs", "kafka", "redis+pubsub", "google+pubsub", "azure+servicebus", "kinesis"],
    )
    kwargs: dict[Identifier, Value] = Field(
        default_factory=dict, description="Provider specific MessageQueueTrigger kwargs."
    )


class AssetRef(StrictModel):
    name: str | None = None
    uri: str | None = None
    group: str | None = None
    extra: dict[str, Value] = Field(default_factory=dict)
    watchers: list[AssetWatcherSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def _name_or_uri(self) -> AssetRef:
        if not self.name and not self.uri:
            raise ValueError("an asset needs at least a name or a uri")
        return self


class AssetLeaf(StrictModel):
    op: Literal["asset"]
    asset: AssetRef


class AssetAliasLeaf(StrictModel):
    op: Literal["alias"]
    name: str = Field(min_length=1)


class AssetCombination(StrictModel):
    op: Literal["all", "any"]
    items: list[AssetExpr] = Field(min_length=1)


AssetExpr = Annotated[AssetLeaf | AssetAliasLeaf | AssetCombination, Field(discriminator="op")]
AssetCombination.model_rebuild()


# --------------------------------------------------------------------------- schedule


class NoSchedule(StrictModel):
    type: Literal["none"]


class PresetSchedule(StrictModel):
    type: Literal["preset"]
    preset: Literal[
        "@once", "@continuous", "@hourly", "@daily", "@weekly", "@monthly", "@quarterly", "@yearly"
    ]


CronExpr = Annotated[str, Field(pattern=r"^\S+( \S+){4,5}$", examples=["0 6 * * 1-5"])]


class CronSchedule(StrictModel):
    """``CronTriggerTimetable`` (default) or ``CronDataIntervalTimetable``."""

    type: Literal["cron"]
    expression: CronExpr
    timezone: str | None = Field(default=None, examples=["UTC", "Europe/Istanbul"])
    data_interval: bool = Field(
        default=False, description="Airflow 2 semantics: logical date = start of the interval."
    )
    interval_seconds: float | None = Field(
        default=None, ge=0, description="CronTriggerTimetable(interval=...) data interval length."
    )
    run_immediately: bool = False


class IntervalSchedule(StrictModel):
    """``DeltaTriggerTimetable`` (default) or ``DeltaDataIntervalTimetable``."""

    type: Literal["interval"]
    seconds: int = Field(gt=0)
    data_interval: bool = False


class MultiCronSchedule(StrictModel):
    type: Literal["multi_cron"]
    expressions: list[CronExpr] = Field(min_length=1)
    timezone: str | None = None


class EventsSchedule(StrictModel):
    type: Literal["events"]
    dates: list[datetime.datetime] = Field(min_length=1)
    restrict_to_events: bool = False
    description: str | None = None


TimeSchedule = Annotated[
    PresetSchedule | CronSchedule | IntervalSchedule | MultiCronSchedule | EventsSchedule,
    Field(discriminator="type"),
]


class AssetSchedule(StrictModel):
    type: Literal["assets"]
    expression: AssetExpr
    batch_asset_events: bool | None = Field(
        default=None, description="Airflow >= 3.4: batch events into one run (default per event)."
    )


class AssetOrTimeScheduleSpec(StrictModel):
    """Run on the time schedule *or* when the assets update."""

    type: Literal["assets_or_time"]
    expression: AssetExpr
    time: TimeSchedule


class AssetAndTimeScheduleSpec(StrictModel):
    """Airflow >= 3.4: scheduled run happens only once the assets were updated."""

    type: Literal["assets_and_time"]
    expression: AssetExpr
    time: TimeSchedule


PartitionMapperName = Literal[
    "identity",
    "start_of_hour",
    "start_of_day",
    "start_of_week",
    "start_of_month",
    "start_of_quarter",
    "start_of_year",
]


class PartitionedCronSchedule(StrictModel):
    """Airflow >= 3.2 ``CronPartitionTimetable`` — produces partitioned runs."""

    type: Literal["partitioned_cron"]
    expression: CronExpr
    timezone: str = "UTC"
    key_format: str | None = None


class PartitionedAssetSchedule(StrictModel):
    """Airflow >= 3.2 ``PartitionedAssetTimetable`` — consume partitioned asset events."""

    type: Literal["partitioned_assets"]
    expression: AssetExpr
    mapper: PartitionMapperName = "identity"
    timezone: str | None = None


Schedule = Annotated[
    NoSchedule
    | PresetSchedule
    | CronSchedule
    | IntervalSchedule
    | MultiCronSchedule
    | EventsSchedule
    | AssetSchedule
    | AssetOrTimeScheduleSpec
    | AssetAndTimeScheduleSpec
    | PartitionedCronSchedule
    | PartitionedAssetSchedule,
    Field(discriminator="type"),
]

TriggerRule = Literal[
    "all_success",
    "all_failed",
    "all_done",
    "all_done_min_one_success",
    "all_skipped",
    "one_success",
    "one_done",
    "one_failed",
    "none_failed",
    "none_failed_min_one_success",
    "none_skipped",
    "always",
    "all_done_setup_success",
]


# --------------------------------------------------------------------------- behaviours


class CallbackRef(StrictModel):
    """A notifier instance (``BaseNotifier`` subclass from the catalog)."""

    notifier: str = Field(
        pattern=FQN_PATTERN,
        examples=["airflow.providers.slack.notifications.slack.SlackNotifier"],
    )
    params: dict[Identifier, Value] = Field(default_factory=dict)


class Callbacks(StrictModel):
    on_success: list[CallbackRef] = Field(default_factory=list)
    on_failure: list[CallbackRef] = Field(default_factory=list)
    on_retry: list[CallbackRef] = Field(default_factory=list)
    on_execute: list[CallbackRef] = Field(default_factory=list)
    on_skipped: list[CallbackRef] = Field(default_factory=list)

    def is_empty(self) -> bool:
        return not any(getattr(self, f) for f in type(self).model_fields)


class RetryRuleSpec(StrictModel):
    exception: str = Field(
        pattern=FQN_PATTERN,
        description="Dotted exception class path, e.g. builtins.ValueError or "
        "requests.exceptions.HTTPError",
    )
    action: Literal["retry", "fail", "default"] = "retry"
    retry_delay_seconds: float | None = Field(default=None, ge=0)
    reason: str | None = None
    match_subclasses: bool = True


class RetryPolicySpec(StrictModel):
    """Airflow >= 3.3 ``ExceptionRetryPolicy``."""

    rules: list[RetryRuleSpec] = Field(min_length=1)
    default: Literal["retry", "fail", "default"] = "default"


class DeadlineSpec(StrictModel):
    """Airflow >= 3.1 ``DeadlineAlert``."""

    name: str | None = None
    reference: Literal[
        "dagrun_queued_at", "dagrun_logical_date", "fixed_datetime", "average_runtime"
    ] = "dagrun_queued_at"
    interval_seconds: float
    fixed_datetime: datetime.datetime | None = None
    average_max_runs: int | None = Field(default=None, ge=1)
    callback: CallbackRef
    synchronous: bool = False

    @model_validator(mode="after")
    def _fixed(self) -> DeadlineSpec:
        if self.reference == "fixed_datetime" and self.fixed_datetime is None:
            raise ValueError("fixed_datetime reference needs a fixed_datetime")
        return self


# --------------------------------------------------------------------------- nodes


class Position(StrictModel):
    """Canvas geometry (relative to the parent group). Ignored by code generation."""

    x: float = 0
    y: float = 0
    width: float | None = Field(default=None, gt=0)
    height: float | None = Field(default=None, gt=0)


class MappingSpec(StrictModel):
    """Dynamic task mapping.

    * ``expand``: ``Op.partial(...).expand(**expand)`` — values are lists or XCom refs;
      those argument names are removed from the node's regular params.
    * ``expand_kwargs``: ``Op.partial(...).expand_kwargs(<xcom ref to a list of dicts>)``.
    """

    expand: dict[Identifier, Value] = Field(default_factory=dict)
    expand_kwargs: Value = None
    map_index_template: str | None = None

    @model_validator(mode="after")
    def _one_mode(self) -> MappingSpec:
        if bool(self.expand) == (self.expand_kwargs is not None):
            raise ValueError("mapping needs exactly one of `expand` or `expand_kwargs`")
        return self


class _NodeBase(StrictModel):
    id: NodeId
    label: str | None = Field(default=None, max_length=200, description="Canvas label only.")
    icon: str | None = Field(
        default=None,
        pattern=r"^(builtin|custom|package):[a-z0-9][a-z0-9-]{0,80}$",
        description="Canvas icon only (builtin:<logo> or custom:<name>); overrides the default.",
    )
    parent_group: NodeId | None = Field(default=None, description="Id of a task_group node.")
    position: Position = Field(default_factory=Position)


class TemplateRef(StrictModel):
    """The operator template (blueprint) a task was inserted from."""

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,80}$")
    version: str | None = Field(default=None, max_length=40)


class _TaskBase(_NodeBase):
    task_id: Key
    trigger_rule: TriggerRule | None = None
    retries: int | None = Field(default=None, ge=0)
    retry_delay_seconds: float | None = Field(default=None, ge=0)
    execution_timeout_seconds: float | None = Field(default=None, gt=0)
    pool: str | None = None
    pool_slots: int | None = Field(default=None, ge=1)
    queue: str | None = None
    owner: str | None = None
    doc_md: str | None = None
    outlets: list[AssetRef] = Field(default_factory=list)
    inlets: list[AssetRef] = Field(default_factory=list)
    mapping: MappingSpec | None = None
    # behaviour
    is_setup: bool = False
    is_teardown: bool = False
    callbacks: Callbacks = Field(default_factory=Callbacks)
    retry_policy: RetryPolicySpec | None = None
    retry_exponential_backoff: float | None = Field(default=None, ge=0)
    # advanced
    priority_weight: int | None = None
    weight_rule: Literal["downstream", "upstream", "absolute"] | None = None
    task_display_name: str | None = None
    executor: str | None = None
    executor_config: dict[str, Value] | None = None
    max_active_tis_per_dagrun: int | None = Field(default=None, ge=1)
    depends_on_past: bool | None = None
    disabled: bool = Field(
        default=False, description="Excluded from the generated DAG (kept on the canvas)."
    )
    lineage: Literal["inherit", "enabled", "disabled"] = Field(
        default="inherit",
        description="OpenLineage selective enable/disable for this task.",
    )
    template_ref: TemplateRef | None = Field(
        default=None,
        description="Operator template (blueprint) this task was created from; validation "
        "enforces the template's locked fields. Not part of the generated DAG.",
    )

    @model_validator(mode="after")
    def _setup_teardown(self) -> _TaskBase:
        if self.is_setup and self.is_teardown:
            raise ValueError("a task cannot be both setup and teardown")
        return self


class OperatorNode(_TaskBase):
    """Any ``BaseOperator`` subclass discovered by the operator catalog."""

    kind: Literal["operator"]
    operator: str = Field(
        pattern=FQN_PATTERN, examples=["airflow.providers.standard.operators.bash.BashOperator"]
    )
    params: dict[Identifier, Value] = Field(default_factory=dict)


PythonDecorator = Literal[
    "task",
    "branch",
    "short_circuit",
    "virtualenv",
    "external_python",
    "branch_virtualenv",
    "branch_external_python",
    "sensor",
    "bash",
    "docker",
    "kubernetes",
    "stub",
    # apache-airflow-providers-common-ai: the function returns the prompt
    "llm",
    "agent",
    "llm_branch",
    "llm_sql",
]


class PythonTaskNode(_TaskBase):
    """A TaskFlow task whose body is user-supplied Python (privileged).

    ``decorator`` selects ``@task``, ``@task.branch`` (return the downstream task id(s)
    to follow), ``@task.short_circuit`` (return falsy to skip downstream),
    ``@task.virtualenv``, ``@task.sensor`` (return bool / PokeReturnValue),
    ``@task.bash`` (return a command) and friends; ``decorator_kwargs`` are passed to it.
    """

    kind: Literal["python_task"]
    decorator: PythonDecorator = "task"
    decorator_kwargs: dict[Identifier, Value] = Field(default_factory=dict)
    function_name: Identifier
    arguments: dict[Identifier, Value] = Field(
        default_factory=dict, description="Keyword arguments; values may be XCom references."
    )
    extra_parameters: list[Identifier] = Field(
        default_factory=list,
        description="Additional function parameters without a bound value "
        "(e.g. keys provided by expand_kwargs).",
    )
    code: str = Field(description="Function body (statements). Use `return` to push XCom.")
    multiple_outputs: bool = False
    is_result: bool = Field(
        default=False,
        description="Airflow >= 3.3: mark as the DAG result (`@result`), returned by the "
        "dagRuns/{id}/wait API.",
    )
    uses_loop_context: bool = Field(
        default=False,
        description="Inside a loop group: receive the `loop` context (index, previous, "
        "result, max_iterations) as a keyword-only parameter.",
    )


class LoopSpec(StrictModel):
    """Airflow >= 3.4 Task Loop: repeat the group's tasks (``@task_group ... .loop()``).

    The body must have exactly one terminal task; its return value is ``loop.result`` for
    the ``until`` condition and ``loop.previous`` for the next iteration.
    """

    max_iterations: int = Field(ge=1, le=10_000)
    until_code: str | None = Field(
        default=None,
        description="Body of the stop condition (`loop`, `ti`, `params`, ... are available); "
        "return True to stop. Omit for a fixed-count loop.",
    )
    until_name: Identifier = "until_done"


class TaskGroupNode(_NodeBase):
    kind: Literal["task_group"]
    group_id: Key
    tooltip: str | None = None
    prefix_group_id: bool = True
    loop: LoopSpec | None = None


class CustomNode(_TaskBase):
    """Node provided by an extension handler (entry point group ``autoair.node_kinds``)."""

    kind: Literal["custom"]
    handler: str = Field(pattern=r"^[a-z0-9_.-]+$")
    config: dict[str, Value] = Field(default_factory=dict)


BLUEPRINT_NAME_PATTERN = r"^[a-z][a-z0-9_]*$"


class BlueprintNode(_NodeBase):
    """A step built from an Astronomer Blueprint template (``airflow-blueprint``).

    A blueprint is a reusable ``Blueprint[Config]`` class with a Pydantic config model whose
    ``render()`` returns a task or a TaskGroup. The generated DAG imports ``class_path``, sets
    ``step_id`` on an instance and calls ``render(Config(**config))``, the way the blueprint
    project documents its use in Python DAGs. Edges to and from the node depend on the whole
    rendered step. The Airflow environment needs ``airflow-blueprint`` and the template.
    """

    kind: Literal["blueprint"]
    step_id: Key = Field(description="Task or group id the blueprint renders under.")
    blueprint: str = Field(
        pattern=BLUEPRINT_NAME_PATTERN,
        max_length=100,
        description="Registered blueprint name (snake_case), as `blueprint list` shows it.",
    )
    version: int = Field(default=1, ge=1, description="Blueprint version the step is pinned to.")
    class_path: str = Field(
        pattern=FQN_PATTERN,
        max_length=300,
        description="Importable path of the Blueprint class (module.Class).",
    )
    config_class: Identifier | None = Field(
        default=None,
        description="Name of the config model in the blueprint's module; omitted: the "
        "generated code asks the class for it (get_config_type()).",
    )
    config: dict[str, Value] = Field(
        default_factory=dict,
        description="Config values, validated against the blueprint's config schema. Plain "
        "JSON values only (no XCom references or tagged values).",
    )
    disabled: bool = Field(
        default=False, description="Excluded from the generated DAG (kept on the canvas)."
    )


class NoteNode(_NodeBase):
    """Sticky note on the canvas. Not a task; optionally appended to the DAG docs."""

    kind: Literal["note"]
    text: str = Field(default="", max_length=20_000)
    color: str | None = Field(default=None, pattern=r"^[a-z]+$|^#[0-9a-fA-F]{6}$")
    include_in_docs: bool = False


Node = Annotated[
    OperatorNode | PythonTaskNode | TaskGroupNode | CustomNode | BlueprintNode | NoteNode,
    Field(discriminator="kind"),
]
TaskNode = OperatorNode | PythonTaskNode | CustomNode


class Edge(StrictModel):
    id: str | None = None
    source: NodeId
    target: NodeId
    label: str | None = Field(default=None, max_length=200)


# --------------------------------------------------------------------------- dag


class DefaultArgs(StrictModel):
    owner: str | None = None
    retries: int | None = Field(default=None, ge=0)
    retry_delay_seconds: float | None = Field(default=None, ge=0)
    execution_timeout_seconds: float | None = Field(default=None, gt=0)
    depends_on_past: bool | None = None
    email: list[str] | None = None
    email_on_failure: bool | None = None
    pool: str | None = None
    queue: str | None = None


ParamType = Literal["string", "number", "integer", "boolean", "array", "object", "null"]


class DagParam(StrictModel):
    """``airflow.sdk.Param`` — JSON Schema validated, rendered in Airflow's trigger form."""

    default: Value = None
    type: ParamType | list[ParamType] | None = None
    title: str | None = None
    description: str | None = None
    description_md: str | None = None
    section: str | None = None
    format: str | None = Field(
        default=None, examples=["date", "date-time", "time", "duration", "multiline", "email"]
    )
    enum: list[Value] | None = None
    values_display: dict[str, str] | None = None
    examples: list[Value] | None = None
    minimum: int | float | None = None
    maximum: int | float | None = None
    min_length: int | None = Field(default=None, ge=0)
    max_length: int | None = Field(default=None, ge=0)
    pattern: str | None = None
    items: dict[str, Value] | None = None
    const: Value = None

    def schema_kwargs(self) -> dict[str, Any]:
        """Keyword arguments for ``Param(default, **kwargs)`` (JSON Schema keywords)."""
        mapping = {"min_length": "minLength", "max_length": "maxLength"}
        out: dict[str, Any] = {}
        for name in type(self).model_fields:
            if name == "default":
                continue
            value = getattr(self, name)
            if value is None:
                continue
            out[mapping.get(name, name)] = value
        return out


OutputMode = Literal["python", "loader", "yaml"]

# Largest raw code DAG file (characters), like the default interchange.max_source_chars.
RAW_CODE_MAX_CHARS = 2_000_000


SourcePolicy = Literal["auto", "generate", "preserve", "strict-preserve"]


class DagSource(StrictModel):
    """How the DAG file of a spec-backed DAG is written (python output mode only).

    ``generate`` writes the file AutoAir generates from the spec; ``preserve`` applies each
    change to the existing (hand-written) file as a minimal patch and keeps its comments,
    formatting and ordering; ``strict-preserve`` is preserve that refuses every change it
    cannot apply exactly. ``auto``: preserve for DAGs that come from hand-written Python
    (imported or applied in the code mode), generate for DAGs built in AutoAir."""

    policy: SourcePolicy = Field(
        default="auto",
        description="auto | generate | preserve | strict-preserve (see the model description).",
    )
    origin: Literal["autoair", "python"] = Field(
        default="autoair",
        description="autoair: built in AutoAir; python: imported from or edited as a "
        "hand-written Python file (makes 'auto' preserve).",
    )
    code: str | None = Field(
        default=None,
        max_length=RAW_CODE_MAX_CHARS,
        description="The hand-written file this spec was last imported from or applied in "
        "the code mode. Preserve patches it when the deploy target has no newer version of "
        "the file. Writing it needs the 'code:raw' permission (it is arbitrary Python).",
    )
    base_sha: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        description="sha256 of the file the editor started from (the imported file). Equal "
        "to the hash of `code`: the code is that file unchanged; different: it was edited in "
        "the code mode, so a merge with changes made outside AutoAir uses the version "
        "deployed last as the common ancestor.",
    )


class EnvironmentOverlay(StrictModel):
    """Per-environment overrides applied at deploy time (dev / staging / prod...)."""

    schedule: Schedule | None = None
    is_paused_upon_creation: bool | None = None
    tags: list[str] | None = None
    default_args: DefaultArgs | None = None
    params_defaults: dict[Identifier, Value] = Field(default_factory=dict)
    max_active_runs: int | None = Field(default=None, ge=1)
    connections: dict[Key, Key] = Field(
        default_factory=dict,
        description="Logical connection id -> this environment's Airflow connection id "
        "(substituted in *_conn_id parameters, Jinja `conn.<id>` and Python hook calls; "
        "see autoair.spec.env_mapping).",
    )
    variables: dict[Key, Key] = Field(
        default_factory=dict,
        description="Logical Variable key -> this environment's Variable key (Jinja "
        '`var.value.<key>` / `var.json.<key>` and `Variable.get("<key>")`).',
    )


def check_timezone(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    import zoneinfo

    try:
        zoneinfo.ZoneInfo(value)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(
            f"unknown timezone {value!r} (use an IANA name like Europe/Istanbul)"
        ) from exc
    return value


class DagSpec(StrictModel):
    spec_version: Literal[3] = SPEC_VERSION
    dag_id: Key
    output_mode: OutputMode = "python"
    display_name: str | None = None
    description: str | None = None
    doc_md: str | None = None
    schedule: Schedule = Field(default_factory=lambda: NoSchedule(type="none"))
    timezone: str | None = Field(
        default=None,
        examples=["Europe/Istanbul", "America/New_York"],
        description="DAG timezone (IANA). start/end dates are wall-clock times in it and "
        "schedules without their own timezone (presets, cron, intervals) follow it. "
        "None: Airflow's default_timezone (UTC unless configured).",
    )
    start_date: datetime.datetime | None = None
    end_date: datetime.datetime | None = None
    catchup: bool = False
    tags: list[str] = Field(default_factory=list)
    max_active_runs: int | None = Field(default=None, ge=1)
    max_active_tasks: int | None = Field(default=None, ge=1)
    dagrun_timeout_seconds: float | None = Field(default=None, gt=0)
    is_paused_upon_creation: bool | None = None
    default_args: DefaultArgs = Field(default_factory=DefaultArgs)

    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, value: str | None) -> str | None:
        return check_timezone(value)

    params: dict[Identifier, DagParam] = Field(default_factory=dict)
    callbacks: Callbacks = Field(
        default_factory=Callbacks, description="DAG-level on_success / on_failure only."
    )
    deadlines: list[DeadlineSpec] = Field(default_factory=list)
    fail_fast: bool | None = None
    max_consecutive_failed_dag_runs: int | None = Field(default=None, ge=1)
    owner_links: dict[str, str] = Field(default_factory=dict)
    render_template_as_native_obj: bool | None = None
    rerun_with_latest_version: bool | None = None
    team: str | None = Field(default=None, pattern=r"^[a-z0-9_-]+$")
    lineage: Literal["inherit", "enabled", "disabled"] = Field(
        default="inherit",
        description="OpenLineage for the whole DAG (selective enable/disable).",
    )
    environments: dict[Key, EnvironmentOverlay] = Field(default_factory=dict)
    nodes: list[Node] = Field(default_factory=list)
    edges: list[Edge] = Field(default_factory=list)
    source: DagSource | None = Field(
        default=None,
        description="Source policy of the DAG file (generate / preserve / strict-preserve) "
        "and the hand-written file it comes from. None: auto policy, built in AutoAir.",
    )
    raw_code: str | None = Field(
        default=None,
        max_length=RAW_CODE_MAX_CHARS,
        description="Raw code DAG: the complete DAG file, written by hand and deployed as it "
        "is (no generated code, no spec rules; validated by syntax, lint, secret, policy and "
        "import checks). Set: nodes and edges must be empty, output_mode is python and "
        "environment overlays do not change the file. Absent: a spec-backed (visual) DAG.",
    )

    @model_validator(mode="before")
    @classmethod
    def _migrate(cls, data: Any) -> Any:
        if isinstance(data, dict):
            from autoair.spec.migrations import migrate

            return migrate(data)
        return data

    @model_validator(mode="after")
    def _raw_document(self) -> DagSpec:
        if self.raw_code is None:
            return self
        if self.nodes or self.edges:
            raise ValueError("a raw code DAG (raw_code) has no nodes or edges")
        if self.output_mode != "python":
            raise ValueError("a raw code DAG (raw_code) uses output_mode 'python'")
        return self

    @property
    def is_raw(self) -> bool:
        """A raw code DAG (hand-written file) rather than a spec-backed one."""
        return self.raw_code is not None

    @property
    def document_type(self) -> Literal["spec", "raw"]:
        return "raw" if self.raw_code is not None else "spec"

    def node_map(self) -> dict[str, Node]:
        return {n.id: n for n in self.nodes}

    def task_nodes(self, *, include_disabled: bool = True) -> list[TaskNode]:
        return [
            n
            for n in self.nodes
            if isinstance(n, (OperatorNode, PythonTaskNode, CustomNode))
            and (include_disabled or not n.disabled)
        ]

    def note_nodes(self) -> list[NoteNode]:
        return [n for n in self.nodes if isinstance(n, NoteNode)]

    def with_environment(self, env: str | None) -> DagSpec:
        """Return a copy with the named environment overlay applied. Raw code DAGs are
        returned unchanged: their file is deployed as written in every environment."""
        if not env:
            return self
        overlay = self.environments.get(env)
        if overlay is None:
            raise KeyError(f"unknown environment {env!r}")
        if self.raw_code is not None:
            return self
        update: dict[str, Any] = {}
        for field in (
            "schedule",
            "is_paused_upon_creation",
            "tags",
            "default_args",
            "max_active_runs",
        ):
            value = getattr(overlay, field)
            if value is not None:
                update[field] = value
        if overlay.params_defaults:
            params = {k: v.model_copy() for k, v in self.params.items()}
            for k, v in overlay.params_defaults.items():
                params[k] = (
                    params[k].model_copy(update={"default": v})
                    if k in params
                    else DagParam(default=v)
                )
            update["params"] = params
        effective = self.model_copy(update=update)
        if overlay.connections or overlay.variables:
            from autoair.spec.env_mapping import apply_mapping

            effective = apply_mapping(effective, overlay.connections, overlay.variables)
        return effective

    def group_nodes(self) -> list[TaskGroupNode]:
        return [n for n in self.nodes if isinstance(n, TaskGroupNode)]

    def blueprint_nodes(self, *, include_disabled: bool = True) -> list[BlueprintNode]:
        return [
            n
            for n in self.nodes
            if isinstance(n, BlueprintNode) and (include_disabled or not n.disabled)
        ]


def iter_tagged(value: Any, tag: str) -> list[dict[str, Any]]:
    """Raw tagged objects (``{"$type": tag, …}``) nested anywhere inside a value."""
    found: list[dict[str, Any]] = []
    if isinstance(value, BaseModel):
        value = value.model_dump(by_alias=True)
    if isinstance(value, dict):
        if value.get("$type") == tag:
            found.append(value)
        else:
            for v in value.values():
                found.extend(iter_tagged(v, tag))
    elif isinstance(value, (list, tuple)):
        for v in value:
            found.extend(iter_tagged(v, tag))
    return found


def iter_python_refs(value: Any) -> list[dict[str, Any]]:
    """Raw ``{"$type": "import"}`` objects nested anywhere inside a value (unvalidated)."""
    found: list[dict[str, Any]] = []
    if isinstance(value, PythonRef):
        found.append(value.model_dump(by_alias=True))
    elif isinstance(value, dict):
        if value.get("$type") == "import":
            found.append(value)
        else:
            for v in value.values():
                found.extend(iter_python_refs(v))
    elif isinstance(value, (list, tuple)):
        for v in value:
            found.extend(iter_python_refs(v))
    elif isinstance(value, BaseModel):
        found.extend(iter_python_refs(value.model_dump(by_alias=True)))
    return found


def iter_xcom_refs(value: Value) -> list[XComRef]:
    """Collect XCom references nested anywhere inside a parameter value."""
    found: list[XComRef] = []
    if isinstance(value, XComRef):
        found.append(value)
    elif isinstance(value, dict):
        if value.get("$type") == "xcom":
            found.append(XComRef.model_validate(value))
        else:
            for v in value.values():
                found.extend(iter_xcom_refs(v))
    elif isinstance(value, list):
        for v in value:
            found.extend(iter_xcom_refs(v))
    return found
