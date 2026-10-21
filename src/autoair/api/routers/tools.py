from __future__ import annotations

import functools
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel

from autoair.api.deps import ServicesDep, policy_of
from autoair.codegen import CodegenError, render
from autoair.codegen.dagfactory import DagFactoryExport, export_dag_factory
from autoair.iam.deps import require_permission
from autoair.iam.model import CODE_EXECUTE
from autoair.spec import DagSpec
from autoair.validation import Problem, ValidationReport, validate_spec

router = APIRouter(tags=["tools"])


class CodegenResult(BaseModel):
    code: str | None
    problems: list[Problem]


@router.post("/codegen", operation_id="generateCode")
async def generate_code(
    spec: DagSpec,
    services: ServicesDep,
    environment: Annotated[
        str | None,
        Query(
            max_length=250,
            description="Render with this environment's overlay and connection/variable "
            "mapping applied (the read-only environment preview). Raw code DAGs render "
            "unchanged.",
        ),
    ] = None,
) -> CodegenResult:
    if environment and environment not in spec.environments:
        raise HTTPException(status_code=400, detail=f"unknown environment {environment!r}")

    def run() -> CodegenResult:
        try:
            effective = spec.with_environment(environment)
            return CodegenResult(code=render(effective, services.registry), problems=[])
        except CodegenError as exc:
            return CodegenResult(
                code=None,
                problems=[
                    Problem(severity="error", code="codegen", message=str(exc), node_id=exc.node_id)
                ],
            )
        except Exception as exc:  # e.g. handler config errors
            return CodegenResult(
                code=None, problems=[Problem(severity="error", code="codegen", message=str(exc))]
            )

    return await run_in_threadpool(run)


@router.post("/validate", operation_id="validateSpec")
async def validate(
    spec: DagSpec,
    services: ServicesDep,
    request: Request,
    import_check: bool = False,
    environment: str | None = None,
) -> ValidationReport:
    """``environment`` (default: ``default_environment``) applies that environment's overlay
    and selects the policy rules that match it."""
    if import_check:  # imports the generated file: runs inline code on this host
        await require_permission(request, CODE_EXECUTE, action="the import check")
    env = environment or services.settings.default_environment
    if env and env in spec.environments:
        try:
            spec = spec.with_environment(env)
        except ValueError as exc:  # a mapping that produces an invalid spec
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    return await run_in_threadpool(
        validate_spec,
        spec,
        environment=env,
        catalog=services.catalog,
        registry=services.registry,
        policy=policy_of(services),
        import_check=import_check,
        import_timeout=services.settings.import_check_timeout,
        templates=services.templates.lock,
    )


@router.post("/export/dag-factory", operation_id="exportDagFactory")
def export(spec: DagSpec) -> DagFactoryExport:
    return export_dag_factory(spec)


@functools.cache
def _spec_schema() -> dict[str, Any]:
    return DagSpec.model_json_schema()  # static per process: build it once


@router.get("/schema/spec", operation_id="getSpecSchema")
def spec_schema() -> dict[str, Any]:
    return _spec_schema()
