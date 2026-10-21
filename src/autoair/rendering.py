"""Spec -> deployable files for every output mode (python, loader, yaml)."""

from __future__ import annotations

from autoair.codegen.yaml_dag import export_yaml_dag
from autoair.registry import HandlerRegistry
from autoair.spec import DagSpec
from autoair.storage import StorageError
from autoair.storage.render import render_dag_files


def render_files(
    spec: DagSpec, environment: str | None = None, *, registry: HandlerRegistry | None = None
) -> dict[str, str]:
    """``{file name: content}`` written next to ``<dag_id>.autoair.json``.

    Environment overlays are applied for python and yaml output. Loader stubs read the
    raw spec and apply ``AUTOAIR_ENVIRONMENT`` at parse time.
    """
    if spec.output_mode != "yaml":
        return render_dag_files(spec, environment=environment, registry=registry)
    effective = spec.with_environment(environment) if environment in spec.environments else spec
    export = export_yaml_dag(effective)
    if not export.lossless:
        errors = "; ".join(i.message for i in export.issues if i.severity == "error")
        raise StorageError(f"spec cannot be expressed as an Airflow YAML Dag: {errors}")
    return {f"{spec.dag_id}.yaml": export.yaml}
