"""Blueprint catalog settings (``AUTOAIR_BLUEPRINTS__*``).

Astronomer Blueprint (``airflow-blueprint``) templates become canvas nodes: the catalog finds
``Blueprint[Config]`` subclasses without importing them, then reads each config schema in a
child process. See :mod:`autoair.blueprints.catalog`.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class BlueprintSettings(BaseModel):
    """Where the blueprint catalog looks for templates and how long it may take."""

    model_config = ConfigDict(extra="forbid")

    entry_points: bool = Field(
        default=True,
        description="Discover installed packages that advertise blueprint modules under the "
        "`airflow_blueprint.blueprints` entry-point group (the blueprint project's own "
        "sharing mechanism). false: only template_dirs and modules.",
    )
    template_dirs: list[str] = Field(
        default_factory=list,
        description="Folders scanned for blueprint templates (*.py files defining "
        "Blueprint[Config] subclasses), like the blueprint registry scans the dags folder. A "
        "file's module name is its path relative to the folder, so generated DAGs import it "
        "the way Airflow does when the folder is the dags folder (on sys.path). The files are "
        "imported (in a child process) to read schemas: list only folders that admins "
        "control, never one DAG authors deploy to. JSON list in the environment. Empty: no "
        "folders.",
    )
    modules: list[str] = Field(
        default_factory=list,
        description="Additional importable modules holding blueprint templates (dotted "
        "names). JSON list in the environment.",
    )
    max_files: int = Field(
        default=2000,
        ge=1,
        description="Most Python files read per catalog build across template_dirs, modules "
        "and entry-point packages; files beyond it are reported as catalog errors.",
    )
    schema_timeout: float = Field(
        default=120.0,
        gt=0,
        description="Seconds the child process may take to import the blueprint modules and "
        "read their config schemas (one process per catalog build).",
    )
    rescan_interval: float = Field(
        default=30.0,
        ge=0,
        description="Seconds between checks whether template files or packages changed "
        "(a cheap stat of the sources); a change rebuilds the catalog. 0: check on every "
        "request.",
    )
    preview_timeout: float = Field(
        default=60.0,
        gt=0,
        description="Seconds a task preview of one blueprint step may take (rendered in the "
        "warm DagBag worker pool).",
    )
