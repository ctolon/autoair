"""Deterministic DAG checks that need no AI: ``POST /review/rules`` and ``POST /lint/secrets``.

The rules-only best-practice review and the leak/secret scan are plain rules over the
DagSpec (:mod:`autoair.ai.rules`; no LLM, no pydantic-ai), so they live outside the AI
router and stay available without the ``autoair[ai]`` extra or with ``features.ai`` off.
Gated like validation (``builder:use``). The LLM-assisted passes stay at ``/ai/review`` and
``/ai/leaks`` (``ai:use`` + ``features.ai``).
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from autoair.ai.findings import FindingsReport
from autoair.api.deps import ServicesDep, policy_of
from autoair.spec import DagSpec

router = APIRouter(tags=["review"])


class RulesRequest(BaseModel):
    spec: DagSpec = Field(description="The DAG to check (as open in the editor).")


@router.post("/review/rules", operation_id="reviewDagRules")
async def review_rules(body: RulesRequest, services: ServicesDep) -> FindingsReport:
    """Best-practice review by rules: retries, timeouts, idempotency, Airflow 3 idioms,
    ownership and docs. Findings carry JSON patches; ``proposed_spec`` has them applied."""
    from autoair.ai.assist import review_dag

    report: FindingsReport = await run_in_threadpool(
        review_dag,
        body.spec,
        catalog=services.catalog,
        registry=services.registry,
        policy=policy_of(services),
    )
    return report


@router.post("/lint/secrets", operation_id="scanDagSecrets")
async def scan_secrets(body: RulesRequest, services: ServicesDep) -> FindingsReport:
    """Leak scan by rules: hard-coded secrets, credentials exposed through logs, templates,
    XCom or command lines, and unbounded resources. Secret values are masked."""
    from autoair.ai.assist import scan_dag_leaks

    report: FindingsReport = await run_in_threadpool(
        scan_dag_leaks,
        body.spec,
        catalog=services.catalog,
        registry=services.registry,
        policy=policy_of(services),
    )
    return report
