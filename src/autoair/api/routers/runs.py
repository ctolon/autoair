"""Test-run a task / DAG of a spec: on the validator service when ``validator.url`` is set,
else in a sandboxed subprocess on the AutoAir host (:mod:`autoair.runner`)."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from autoair.api.deps import ServicesDep
from autoair.codegen import CodegenError, render
from autoair.iam import model as P
from autoair.iam.deps import PrincipalDep, check
from autoair.iam.model import Principal
from autoair.runner import (
    DagTestMode,
    DagTestResult,
    TaskTestResult,
    local_sandbox,
    process_isolated,
    ran_on,
    run_dag_test,
    run_task_test,
)
from autoair.settings import Settings
from autoair.spec import CustomNode, DagSpec, OperatorNode, PythonTaskNode
from autoair.validation import effective_task_id, policy_from_settings

router = APIRouter(prefix="/runner", tags=["runner"])

MAX_TIMEOUT = 86_400.0  # schema ceiling; AUTOAIR_RUNNER__MAX_TIMEOUT is enforced per request


class TestTaskRequest(BaseModel):
    spec: DagSpec
    task_node_id: str
    conf: dict[str, Any] = Field(default_factory=dict, description="dag_run.conf / params")
    mock_xcoms: dict[str, Any] = Field(
        default_factory=dict,
        description="Upstream node id -> value returned by xcom_pull "
        "(use '<node_id>:<key>' for non-default XCom keys).",
    )
    timeout: float | None = Field(
        default=None,
        gt=0,
        le=MAX_TIMEOUT,
        description="Seconds (default AUTOAIR_RUNNER__DEFAULT_TASK_TIMEOUT, at most "
        "AUTOAIR_RUNNER__MAX_TIMEOUT).",
    )


class TestDagRequest(BaseModel):
    spec: DagSpec
    conf: dict[str, Any] = Field(default_factory=dict)
    mode: DagTestMode = Field(
        default="memory",
        description="memory: no metadata DB; db: dag.test() (writes a run to the DB; needs "
        "runner.allow_db_mode and admin:config); auto: db with fallback to memory when db is "
        "allowed for the caller, else memory.",
    )
    timeout: float | None = Field(
        default=None,
        gt=0,
        le=MAX_TIMEOUT,
        description="Seconds (default AUTOAIR_RUNNER__DEFAULT_DAG_TIMEOUT, at most "
        "AUTOAIR_RUNNER__MAX_TIMEOUT).",
    )


DbModeUnavailable = Literal["disabled", "admin_only"]


def db_mode_unavailable(settings: Settings, principal: Principal) -> DbModeUnavailable | None:
    """Why ``mode=db`` is not available to ``principal`` (``None``: it is). ``mode=db`` hands
    the DAG code the metadata DB connection and the Fernet key: off unless an admin turns
    on ``runner.allow_db_mode``, and then only for callers holding ``admin:config``."""
    if not settings.runner.allow_db_mode:
        return "disabled"
    if not principal.has(P.ADMIN_CONFIG):
        return "admin_only"
    return None


def _timeout(requested: float | None, default: float, services: ServicesDep) -> float:
    limit = services.settings.runner.max_timeout
    timeout = default if requested is None else requested
    if timeout > limit:
        raise HTTPException(
            status_code=422,
            detail=f"timeout must be at most {limit:g}s (AUTOAIR_RUNNER__MAX_TIMEOUT)",
        )
    return timeout


def _enforce_policy(spec: DagSpec, services: ServicesDep) -> None:
    policy = policy_from_settings(services.settings)
    if spec.raw_code is not None:  # a raw code DAG runs its own Python
        from autoair.validation.raw import blocked_operators

        if not policy.allow_python_code:
            raise HTTPException(
                status_code=403,
                detail="raw code DAGs are Python and are disabled by policy (allow_python_code)",
            )
        blocked = blocked_operators(spec.raw_code, policy)
        if blocked:
            raise HTTPException(status_code=403, detail=blocked[0])
    for node in spec.task_nodes(include_disabled=False):
        if isinstance(node, PythonTaskNode) and not policy.allow_python_code:
            raise HTTPException(
                status_code=403,
                detail=f"node {node.id!r}: Python code tasks are disabled by policy",
            )
        if (
            isinstance(node, OperatorNode)
            and policy.is_privileged(node.operator)
            and not policy.allow_privileged_operators
        ):
            raise HTTPException(
                status_code=403,
                detail=f"node {node.id!r}: {node.operator} is disabled by policy",
            )


def _render(spec: DagSpec, services: ServicesDep) -> str:
    try:
        return render(spec, services.registry, fmt=False)
    except CodegenError as exc:
        raise HTTPException(status_code=422, detail=f"codegen failed: {exc}") from exc
    except Exception as exc:  # handler config errors
        raise HTTPException(status_code=422, detail=f"codegen failed: {exc}") from exc


def _sandbox(services: ServicesDep) -> Any:
    """The local sandbox for this server (``runner.*``), AutoAir's DAG folder read-only."""
    s = services.settings
    return local_sandbox(s.runner, read_only=(s.dags_folder,))


async def _audit(
    services: ServicesDep,
    principal: Principal,
    dag_id: str,
    result: TaskTestResult | DagTestResult,
    **details: Any,
) -> None:
    await run_in_threadpool(
        lambda: services.audit.record(
            "test_run",
            user=principal.user,
            dag_id=dag_id,
            state=result.state,
            where=ran_on(result),
            process_isolated=process_isolated(result),
            **details,
        )
    )


@router.post("/test-task", operation_id="testTask")
async def test_task(
    req: TestTaskRequest, services: ServicesDep, principal: PrincipalDep
) -> TaskTestResult:
    spec = req.spec
    nodes = spec.node_map()
    node = nodes.get(req.task_node_id)
    if node is None:
        raise HTTPException(status_code=404, detail=f"node {req.task_node_id!r} not found")
    if not isinstance(node, (OperatorNode, PythonTaskNode, CustomNode)):
        raise HTTPException(status_code=422, detail=f"node {node.id!r} is not a task")
    if node.disabled:
        raise HTTPException(status_code=422, detail=f"node {node.id!r} is disabled")
    _enforce_policy(spec, services)
    task_id = effective_task_id(node, dict(nodes))
    mocks: dict[str, Any] = {}
    for key, value in req.mock_xcoms.items():
        node_id, sep, xcom_key = key.partition(":")
        target = nodes.get(node_id)
        tid = (
            effective_task_id(target, dict(nodes))
            if isinstance(target, (OperatorNode, PythonTaskNode, CustomNode))
            else node_id
        )
        mocks[f"{tid}{sep}{xcom_key}"] = value
    source = await run_in_threadpool(_render, spec, services)
    s = services.settings
    result = await run_in_threadpool(
        lambda: run_task_test(
            source,
            spec.dag_id,
            task_id,
            conf=req.conf,
            timeout=_timeout(req.timeout, s.runner.default_task_timeout, services),
            mock_xcoms=mocks,
            max_log=s.runner.max_log_chars,
            max_value=s.runner.max_value_chars,
            validator=s.validator,
            sandbox=_sandbox(services),
        )
    )
    await _audit(
        services,
        principal,
        spec.dag_id,
        result,
        kind="task",
        task_id=task_id,
    )
    return result


@router.post("/test-dag", operation_id="testDag")
async def test_dag(
    req: TestDagRequest, services: ServicesDep, principal: PrincipalDep, request: Request
) -> DagTestResult:
    s = services.settings
    mode: DagTestMode = req.mode
    why = db_mode_unavailable(s, principal)
    if mode == "db":
        if why == "disabled":
            raise HTTPException(
                status_code=403,
                detail="mode=db is disabled (runner.allow_db_mode): it gives the DAG code the "
                "metadata DB and the Fernet key; use mode=memory",
            )
        # audited 403 like every other missing permission
        await check(
            request,
            principal,
            P.ADMIN_CONFIG,
            detail="mode=db gives the DAG code the metadata DB and the Fernet key: it needs "
            "'admin:config'; use mode=memory",
        )
    elif mode == "auto" and why is not None:
        mode = "memory"  # auto never picks db for a caller who may not use it
    _enforce_policy(req.spec, services)
    source = await run_in_threadpool(_render, req.spec, services)
    result = await run_in_threadpool(
        lambda: run_dag_test(
            source,
            req.spec.dag_id,
            conf=req.conf,
            timeout=_timeout(req.timeout, s.runner.default_dag_timeout, services),
            mode=mode,
            max_log=s.runner.max_log_chars,
            max_value=s.runner.max_value_chars,
            validator=s.validator,
            sandbox=_sandbox(services),
        )
    )
    await _audit(
        services,
        principal,
        req.spec.dag_id,
        result,
        kind="dag",
        mode=result.mode or mode,
        requested_mode=req.mode,
    )
    return result
