"""API and cache models of the blueprint catalog."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

BlueprintSource = Literal["entry_point", "template_dir", "module"]


class BlueprintPreview(BaseModel):
    """The tasks one blueprint step renders, relative to its ``step_id``."""

    kind: Literal["task", "group"] | None = Field(
        default=None, description="What render() returned: one task or a TaskGroup."
    )
    tasks: list[str] = Field(
        default_factory=list,
        description="Task ids inside the step (without the step prefix); a single-task "
        "blueprint lists its own id.",
    )
    error: str | None = Field(default=None, description="Why the preview could not be built.")


class BlueprintSummary(BaseModel):
    """One version of a blueprint template, for the palette."""

    name: str = Field(description="Registered name (snake_case), as `blueprint list` shows it.")
    version: int = Field(ge=1)
    class_name: str
    module: str = Field(description="Module the generated DAG imports the class from.")
    class_path: str = Field(description="module.Class")
    config_class: str | None = Field(
        default=None,
        description="Name of the config model importable from the same module, if any.",
    )
    summary: str | None = Field(default=None, description="First sentence of the docstring.")
    source: BlueprintSource
    package: str | None = Field(default=None, description="Distribution providing it.")
    package_version: str | None = None
    supports_params: bool = Field(
        default=False, description="Config fields become DAG params (supports_params=True)."
    )
    latest: bool = Field(default=True, description="Highest version of this name.")
    schema_error: str | None = Field(
        default=None,
        description="Why the config schema could not be read (e.g. the module failed to import).",
    )


class BlueprintDetail(BlueprintSummary):
    """A blueprint version with its config schema and a default task preview."""

    docstring: str | None = None
    config_schema: dict[str, Any] | None = Field(
        default=None,
        description="JSON Schema of the config model (Blueprint.get_schema(): references "
        "inlined), read in a child process.",
    )
    preview: BlueprintPreview | None = Field(
        default=None, description="Tasks rendered with the config defaults, when they suffice."
    )


class BlueprintCatalogError(BaseModel):
    location: str
    error: str


class BlueprintCatalogStatus(BaseModel):
    """What the catalog found and whether schemas can be read in this installation."""

    available: bool = Field(
        description="airflow-blueprint is installed in AutoAir's environment (needed to read "
        "config schemas; install autoair[blueprint])."
    )
    package_version: str | None = Field(default=None, description="airflow-blueprint version.")
    blueprints: int
    errors: list[BlueprintCatalogError] = Field(default_factory=list)
    built_at: float | None = None


class BlueprintCatalogData(BaseModel):
    """One catalog build (the on-disk cache format)."""

    key: str
    built_at: float
    blueprints: list[BlueprintDetail] = Field(default_factory=list)
    errors: list[BlueprintCatalogError] = Field(default_factory=list)


class BlueprintPreviewRequest(BaseModel):
    name: str = Field(max_length=100, pattern=r"^[a-z][a-z0-9_]*$")
    version: int = Field(ge=1)
    step_id: str = Field(default="step", pattern=r"^[A-Za-z0-9_.-]+$", max_length=250)
    config: dict[str, Any] = Field(default_factory=dict)
