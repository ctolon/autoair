"""Operator catalog data models (serialised to the UI via OpenAPI)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

OperatorCategory = Literal["operator", "sensor", "transfer"]
CatalogStrategy = Literal["static", "import"]


class OperatorSummary(BaseModel):
    fqn: str = Field(description="Fully qualified class path; the import allowlist key.")
    class_name: str
    module: str
    provider: str
    provider_version: str | None = None
    integration: str | None = None
    category: OperatorCategory
    summary: str = ""
    ui_color: str | None = None
    deprecated: bool = False
    builder_supported: bool = True
    unsupported_reason: str | None = None
    unsupported_params: list[str] = Field(
        default=[], description="Constructor params the builder cannot express."
    )
    python_ref_params: list[str] = Field(
        default=[],
        description="Params that take a Python object (callable, class…), given as an import "
        'reference ``{"$type": "import", "path": "pkg.module.name"}``.',
    )
    requires_python_code: bool = Field(
        default=False,
        description="A required param can only be a Python reference: the operator needs the "
        "allow_python_code permission (like Python task nodes).",
    )
    icon: str | None = Field(
        default=None, description="Canvas icon id (assignment or integration logo)."
    )
    equivalent_decorator: str | None = Field(
        default=None,
        description="TaskFlow alternative (``@task.<name>``) where the callable is written in "
        "the builder instead of referenced, e.g. ``branch`` for BranchPythonOperator.",
    )
    group: str | None = Field(
        default=None,
        description="Palette group (catalog.groups or the package metadata); unset: the "
        "provider/package.",
    )
    group_order: int | None = Field(
        default=None, description="Sort key of the palette group (lower first)."
    )
    docs_url: str | None = Field(default=None, description="Documentation link (package metadata).")


class OperatorExample(BaseModel):
    """A ready-made parameter set (package metadata ``examples``)."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    params: dict[str, Any] = Field(default_factory=dict, description="Operator params.")


class OperatorDetail(OperatorSummary):
    params_schema: dict[str, Any] = Field(
        description="JSON Schema (draft 2020-12) of the operator-specific constructor params."
    )
    template_fields: list[str] = []
    docstring: str = ""
    connection_types: list[str] = Field(
        default=[],
        description="Connection types used by the operator's ``*_conn_id`` params "
        "(also given per param as ``x-autoair-conn-type``).",
    )
    is_sensor: bool = False
    deferrable_param: bool = Field(
        default=False, description="The operator accepts a ``deferrable`` argument."
    )
    hitl: bool = Field(default=False, description="Human-in-the-loop operator.")
    required_airflow: str | None = Field(
        default=None, description="Airflow version specifier required by the provider."
    )
    examples: list[OperatorExample] = Field(
        default=[], description="Example parameter sets (package metadata)."
    )


class NotifierSummary(BaseModel):
    fqn: str
    class_name: str
    module: str
    provider: str
    provider_version: str | None = None
    summary: str = ""
    deprecated: bool = False
    builder_supported: bool = True
    unsupported_reason: str | None = None
    unsupported_params: list[str] = []
    python_ref_params: list[str] = []
    requires_python_code: bool = False


class NotifierDetail(NotifierSummary):
    params_schema: dict[str, Any]
    template_fields: list[str] = []
    docstring: str = ""
    connection_types: list[str] = []


class TaskDecoratorInfo(BaseModel):
    name: str = Field(description="Used as ``@task.<name>``.")
    class_name: str = Field(description="Dotted path of the decorator callable.")
    provider: str
    provider_version: str | None = None
    summary: str | None = Field(default=None, description="First sentence of the docs.")
    docstring: str | None = Field(
        default=None, description="Docstring of the decorator (or of its decorated operator)."
    )


class QueueScheme(BaseModel):
    scheme: str
    provider: str = Field(description="Provider package implementing the scheme.")
    available: bool
    provider_class: str | None = None
    trigger_class: str | None = None
    kwargs_schema: dict[str, Any] = Field(
        default_factory=dict, description="JSON Schema of extra MessageQueueTrigger kwargs."
    )


class ConnectionTypeInfo(BaseModel):
    conn_type: str
    hook_class: str | None = None
    hook_name: str | None = None
    provider: str
    default_conn_name: str | None = None


class CatalogError(BaseModel):
    module: str
    provider: str
    error: str


class CatalogStats(BaseModel):
    operators: int
    providers: int
    errors: list[CatalogError]
    notifiers: int = 0
    strategy: CatalogStrategy = "static"
    cache_key: str | None = None
    loaded_from_cache: bool = False
    build_seconds: float | None = None


# --------------------------------------------------------------------------- diagnostics


class CatalogSourceInfo(BaseModel):
    """One catalog source of a build and what it contributed."""

    source: str = Field(description="Source type (ProvidersSource, PathsSource…).")
    detail: list[str] = Field(
        default=[], description="What it scans: folders, packages, distributions, modules."
    )
    modules: int = Field(default=0, description="Candidate modules it yielded.")
    providers: list[str] = Field(default=[], description="Provider/package labels it yielded.")
    operators: list[str] = Field(default=[], description="Operator class paths found.")
    error: str | None = None


class CatalogFiltered(BaseModel):
    """Something discovery or the configuration left out of the palette."""

    name: str = Field(description="Operator class path, module or distribution.")
    kind: Literal["operator", "module", "distribution"]
    reason: str = Field(
        description="operator_allow, operator_deny, discovery.skip_class_pattern, "
        "discovery.skip_module_pattern, discovery.exclude_distributions, "
        "policy.forbidden_operators, policy.privileged_operators."
    )
    detail: str | None = Field(default=None, description="The matching pattern.")


class CatalogMetadataSource(BaseModel):
    """A package metadata file or entry point read by the build."""

    kind: Literal["file", "entry_point"]
    origin: str = Field(description="File path, or `name = module:attr` of the entry point.")
    package: str | None = None
    distribution: str | None = None
    operators: int = Field(default=0, description="Operators it applied to.")
    icons: int = Field(default=0, description="Package icons it provided.")
    error: str | None = None


class CatalogBuildDiagnostics(BaseModel):
    """What a build saw (stored with the catalog)."""

    sources: list[CatalogSourceInfo] = []
    filtered: list[CatalogFiltered] = []
    parse_errors: list[CatalogError] = []
    metadata: list[CatalogMetadataSource] = []


class CatalogDiagnostics(CatalogBuildDiagnostics):
    """``GET /api/catalog/diagnostics``: the build's diagnostics plus the load-time filters."""

    strategy: CatalogStrategy
    cache_key: str | None = None
    loaded_from_cache: bool = False
    built_at: float = Field(description="Build time (epoch seconds).")
    build_seconds: float
    loaded_at: float = Field(description="When this process loaded the catalog (epoch s).")
    operators: int
    notifiers: int
    providers: int
    errors: list[CatalogError] = Field(
        default=[], description="Modules that failed to import or be analysed."
    )
