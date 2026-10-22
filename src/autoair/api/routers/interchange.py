"""Import/export between AutoAir specs and other DAG formats."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, ValidationError, model_validator

from autoair.api.app import Services
from autoair.api.deps import GatewayDep, ServicesDep, http_error
from autoair.api.features import requires_feature
from autoair.api.source_plans import source_code_of
from autoair.codegen.yaml_dag import YamlExport, YamlIssue, export_yaml_dag, import_yaml_dag
from autoair.gateway import AirflowError
from autoair.iam.deps import require, require_permission
from autoair.iam.model import AIRFLOW_READ, CODE_EXECUTE, CODE_RAW, SPEC_WRITE
from autoair.importer.bulk import (
    BulkDag,
    BulkImportError,
    BulkImportResult,
    GitSource,
    UploadedFile,
    cloned,
    materialized,
    scan,
)
from autoair.importer.code_sync import CodeSyncResult, sync_code
from autoair.importer.python_dag import PythonImportResult, import_python_source
from autoair.preserve.service import mark_hand_written
from autoair.spec import DagSpec

router = APIRouter(prefix="/interchange", tags=["interchange"])
_IMPORTS = requires_feature("imports")

# Schema ceiling of request bodies; the effective limit is AUTOAIR_INTERCHANGE__MAX_SOURCE_CHARS
# (2 MB by default), checked per request so it can be raised or lowered without a new client.
MAX_SOURCE = 100_000_000


def _check_size(source: str, services: Services) -> None:
    limit = services.settings.interchange.max_source_chars
    if len(source) > limit:
        raise HTTPException(
            status_code=422,
            detail=f"source is larger than {limit} characters "
            "(AUTOAIR_INTERCHANGE__MAX_SOURCE_CHARS)",
        )


def _python_allowed(services: Services) -> bool:
    return services.settings.feature("python_code")


class SourceBody(BaseModel):
    source: str = Field(max_length=MAX_SOURCE)
    filename: str = Field(default="dag.py", pattern=r"^[\w.-]+\.py$")


class YamlBody(BaseModel):
    source: str = Field(max_length=MAX_SOURCE)


class YamlImportResult(BaseModel):
    spec: DagSpec
    issues: list[YamlIssue]


@router.post("/yaml/export", operation_id="exportYamlDag")
def export_yaml(spec: DagSpec) -> YamlExport:
    """Airflow 3.4 native YAML Dag (AIP-85)."""
    return export_yaml_dag(spec)


@router.post("/yaml/import", operation_id="importYamlDag", dependencies=[_IMPORTS])
def import_yaml(body: YamlBody, services: ServicesDep) -> YamlImportResult:
    _check_size(body.source, services)
    try:
        spec, issues = import_yaml_dag(body.source)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"cannot import YAML Dag: {exc}") from exc
    return YamlImportResult(spec=spec, issues=issues)


class SpecJsonBody(BaseModel):
    source: str = Field(max_length=MAX_SOURCE, description="An exported .autoair.json document.")


@router.post("/spec/import", operation_id="importSpecJson", dependencies=[_IMPORTS])
def import_spec_json(body: SpecJsonBody, services: ServicesDep) -> DagSpec:
    """Parses an AutoAir spec file (older spec versions are migrated)."""
    _check_size(body.source, services)
    try:
        return DagSpec.model_validate_json(body.source)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or '(root)'}: {e['msg']}"
            for e in exc.errors()[:20]
        )
        detail = f"not a valid AutoAir spec: {problems}"
        raise HTTPException(status_code=422, detail=detail) from exc


@router.post("/python/import", operation_id="importPythonDag", dependencies=[_IMPORTS])
async def import_python(
    body: SourceBody, services: ServicesDep, request: Request
) -> PythonImportResult:
    """Reverse-engineer a hand-written Python DAG file (runs in an isolated subprocess)."""
    await require_permission(request, CODE_EXECUTE, action="importing a Python DAG")
    _check_size(body.source, services)
    if not _python_allowed(services):
        raise HTTPException(
            status_code=403,
            detail="importing Python DAGs executes their code "
            "and is disabled by policy (allow_python_code)",
        )
    result = await run_in_threadpool(
        import_python_source,
        body.source,
        body.filename,
        timeout=services.settings.python_import_timeout,
    )
    return _hand_written(result, body.source)


def _hand_written(result: PythonImportResult, source: str) -> PythonImportResult:
    """The imported DAG comes from a hand-written file: its deploys preserve that file
    (source policy auto). A file defining several DAGs cannot be the file of one of them."""
    if len(result.dags) == 1 and result.dags[0].spec is not None:
        dag = result.dags[0]
        if dag.spec is not None:
            dag.spec = mark_hand_written(dag.spec, source, dag.warnings)
    return result


class CodeSyncBody(BaseModel):
    source: str = Field(max_length=MAX_SOURCE, description="The edited DAG file.")
    dag_id: str | None = Field(
        default=None, max_length=250, description="DAG to take when the file defines several."
    )
    current: DagSpec | None = Field(
        default=None,
        description="The document being edited: its canvas-only state (positions, labels, "
        "notes…) is kept and the result carries the diff against it.",
    )


@router.post(
    "/python/sync",
    operation_id="syncPythonCode",
    dependencies=[requires_feature("code_editor")],
)
async def sync_python_code(
    body: CodeSyncBody, services: ServicesDep, request: Request
) -> CodeSyncResult:
    """Code mode: import the edited DAG file into a spec (syntax checked first, then executed
    in an isolated subprocess). Problems come back located when possible; nothing is saved."""
    await require_permission(request, SPEC_WRITE, action="editing the DAG code")
    await require_permission(request, CODE_EXECUTE, action="applying DAG code")
    if body.source != source_code_of(body.current):
        # The applied file becomes the spec's source.code (hand-written Python a deploy may
        # write as the DAG file): the same 'code:raw' a save of that spec needs.
        await require_permission(request, CODE_RAW, action="applying DAG code")
    _check_size(body.source, services)
    if not _python_allowed(services):
        raise HTTPException(
            status_code=403,
            detail="applying DAG code executes it and is disabled by policy (allow_python_code)",
        )
    stem = re.sub(r"[^\w.-]", "_", body.dag_id or (body.current.dag_id if body.current else "dag"))
    return await run_in_threadpool(
        sync_code,
        body.source,
        dag_id=body.dag_id or (body.current.dag_id if body.current else None),
        current=body.current,
        filename=f"{stem or 'dag'}.py",
        timeout=services.settings.python_import_timeout,
    )


@router.post(
    "/airflow/{dag_id}/import",
    operation_id="importAirflowDag",
    # Reads the DAG's source from Airflow: gated like the /airflow routes as well.
    dependencies=[_IMPORTS, requires_feature("airflow"), require(AIRFLOW_READ)],
)
async def import_from_airflow(
    dag_id: str, services: ServicesDep, gw: GatewayDep, request: Request
) -> PythonImportResult:
    """Fetch an existing DAG's source from Airflow (``/dagSources``) and import it."""
    await require_permission(request, CODE_EXECUTE, action="importing a Python DAG")
    if not _python_allowed(services):
        raise HTTPException(status_code=403, detail="disabled by policy (allow_python_code)")
    try:
        source = await gw.dag_source(dag_id)
    except AirflowError as exc:
        raise http_error(exc) from exc
    result = await run_in_threadpool(
        import_python_source,
        source,
        f"{dag_id}.py",
        timeout=services.settings.python_import_timeout,
    )
    several = len(result.dags) > 1
    result.dags = [d for d in result.dags if d.dag_id == dag_id] or result.dags
    result.source = source
    return result if several else _hand_written(result, source)


class BulkImportRequest(BaseModel):
    """A DAG repository: a git repository (cloned shallowly on the server) or the files of a
    folder picked in the browser. Exactly one of ``git`` / ``files``."""

    git: GitSource | None = None
    files: list[UploadedFile] = Field(default_factory=list)

    @model_validator(mode="after")
    def _one_source(self) -> BulkImportRequest:
        if (self.git is None) == (not self.files):
            raise ValueError("give either git or files")
        return self


@router.post(
    "/bulk/scan",
    operation_id="bulkImportDags",
    dependencies=[_IMPORTS],
)
async def bulk_import(
    body: BulkImportRequest, services: ServicesDep, request: Request
) -> BulkImportResult:
    """Import every DAG file of a repository with the Python importer (isolated subprocesses)
    and report per file what was found, which DAGs convert and which already exist. Nothing
    is saved: the client deploys the selected specs (``create_only``, or the stored etag for
    a confirmed overwrite)."""
    await require_permission(request, CODE_EXECUTE, action="importing a DAG repository")
    await require_permission(request, SPEC_WRITE, action="importing a DAG repository")
    if not _python_allowed(services):
        raise HTTPException(
            status_code=403,
            detail="importing Python DAGs executes their code "
            "and is disabled by policy (allow_python_code)",
        )
    cfg = services.settings.interchange
    if len(body.files) > cfg.bulk_max_files * 20:
        raise HTTPException(status_code=422, detail="too many files in one upload")

    def lookup(dag: BulkDag) -> None:
        from autoair.api.routers.specs import _stored_etag

        assert dag.spec is not None
        etag = _stored_etag(services.target_for(dag.spec.team), dag.dag_id, None)
        dag.exists = etag is not None
        dag.stored_etag = etag

    def run() -> BulkImportResult:
        def scan_root(root: Path, label: str, subdir: str | None = None) -> BulkImportResult:
            return scan(
                root,
                label=label,
                subdir=subdir,
                exclude=cfg.bulk_exclude,
                max_files=cfg.bulk_max_files,
                max_chars=cfg.max_source_chars,
                workers=cfg.bulk_workers,
                timeout=services.settings.python_import_timeout,
                lookup=lookup,
            )

        if body.git is not None:
            label = body.git.url + (f"@{body.git.ref}" if body.git.ref else "")
            with cloned(
                body.git, allowed_hosts=cfg.bulk_git_allowed_hosts, timeout=cfg.bulk_git_timeout
            ) as root:
                return scan_root(root, label, body.git.subdir)
        with materialized(body.files, cfg.max_source_chars) as root:
            return scan_root(root, "upload")

    try:
        return await run_in_threadpool(run)
    except BulkImportError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
