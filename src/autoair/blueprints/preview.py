"""Task preview of one blueprint step with the author's config.

The step is generated exactly as in a deployed DAG (a one-node spec through the code
generator) and imported by the warm DagBag worker pool, so the preview runs the blueprint's
``render()`` out of the API process and shows the task ids Airflow would see.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from autoair.blueprints.models import BlueprintPreview, BlueprintPreviewRequest
from autoair.blueprints.worker import PREVIEW_DAG_ID

if TYPE_CHECKING:
    from autoair.blueprints.catalog import BlueprintCatalog

_MAX_ERROR_LINES = 6


def _short(error: str) -> str:
    lines = [ln for ln in error.strip().splitlines() if ln.strip()]
    return "\n".join(lines[-_MAX_ERROR_LINES:])


def preview_step(
    catalog: BlueprintCatalog, request: BlueprintPreviewRequest, timeout: float
) -> BlueprintPreview:
    from pydantic import ValidationError

    from autoair.catalog.schema_check import validate_value
    from autoair.codegen import CodegenError, render
    from autoair.spec import BlueprintNode, DagSpec
    from autoair.validation.blueprints import _schema_value
    from autoair.validation.dagbag_check import ImportCheckUnavailable, check_source

    detail = catalog.get(request.name, request.version)
    if detail is None:
        return BlueprintPreview(
            error=f"blueprint {request.name!r} v{request.version} is not in the catalog"
        )
    if detail.config_schema is not None:
        problems = validate_value(
            detail.config_schema, _schema_value(request.config, detail.config_schema)
        )
        if problems:
            return BlueprintPreview(error="; ".join(problems))
    try:
        node = BlueprintNode(
            kind="blueprint",
            id="preview",
            step_id=request.step_id,
            blueprint=detail.name,
            version=detail.version,
            class_path=detail.class_path,
            config_class=detail.config_class,
            config=request.config,
        )
        source = render(DagSpec(dag_id=PREVIEW_DAG_ID, nodes=[node]), fmt=False)
    except (ValidationError, CodegenError) as exc:
        return BlueprintPreview(error=str(exc).splitlines()[0])
    try:
        result = check_source(source, filename=f"{PREVIEW_DAG_ID}.py", timeout=timeout)
    except ImportCheckUnavailable as exc:  # the validator service is down: a preview error
        return BlueprintPreview(error=str(exc))
    if result.import_errors:
        return BlueprintPreview(error=_short(result.import_errors[0]))
    task_ids = result.task_ids.get(PREVIEW_DAG_ID, [])
    if task_ids == [request.step_id]:
        return BlueprintPreview(kind="task", tasks=task_ids)
    prefix = request.step_id + "."
    return BlueprintPreview(
        kind="group",
        tasks=[t[len(prefix) :] if t.startswith(prefix) else t for t in task_ids],
    )
