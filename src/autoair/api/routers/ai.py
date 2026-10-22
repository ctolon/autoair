"""AI-assisted authoring: generate / edit a DAG from a prompt, explain, review, scan for
leaks, optimise, fix, write code (writers), research, ask the agent - plus per-user
model settings, the knowledge base and the AI audit log.

Every endpoint is read-only with respect to DAGs: proposals come back as findings with
JSON patches and a validated ``proposed_spec`` that the UI shows as a diff before the
user applies it. The leak scan, review and optimise endpoints also work without an LLM.

Streaming (``*/stream``) endpoints answer ``text/event-stream`` (see
:mod:`autoair.ai.events`): token, tool and status events, then one ``result`` (or
``error``) and ``done``. Closing the connection cancels the run.

When ``ai.service_url`` is set the main server does not mount this router; it proxies
``/api/ai/*`` to the AI service (:mod:`autoair.ai.proxy`).
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import logging
import time
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from autoair.ai.access import (
    ADMIN_PERMISSION,
    has_permission,
    request_role,
    request_user,
)
from autoair.ai.agent import AgentResult, AgentTask, arun_agent
from autoair.ai.assist import (
    DagExplanation,
    TaskCodeResult,
    explain_dag,
    fix_dag,
    optimize_dag,
    review_dag,
    scan_dag_leaks,
    write_task_code,
)
from autoair.ai.audit_log import AiAuditRecord, audit_for, now_iso
from autoair.ai.config import AiSettings, ai_settings_of
from autoair.ai.config import active_ai as _active_ai
from autoair.ai.corpus import retriever_for
from autoair.ai.events import Emit, TokenEvent, encode_sse, stream_job
from autoair.ai.findings import AppliedPatches, Finding, FindingsReport, apply_findings
from autoair.ai.generator import GenerationResult, describe_dag, explain_problems, generate_spec
from autoair.ai.guardrails import (
    Budget,
    GuardrailNote,
    GuardrailPipeline,
    GuardrailViolation,
    filter_tools,
    guard_input,
    pipeline_for,
)
from autoair.ai.knowledge import KnowledgeDoc, KnowledgeError, knowledge_for
from autoair.ai.llm import LLM, SUGGESTED_MODELS, ChatMessage, LLMError
from autoair.ai.memory import conventions_for
from autoair.ai.problems import SpecProblem
from autoair.ai.rag import Hit
from autoair.ai.toolbox import build_toolbox, gateway_factory_for
from autoair.ai.tools import AgentTool, executable_tools
from autoair.ai.tuning import HistoryStats, fetch_history, history_stats
from autoair.ai.usage import Usage, UsageMeter, track_usage
from autoair.ai.user_llm import (
    UserLlmCache,
    UserLlmError,
    UserLlmPolicy,
    UserLlmUpdate,
    UserLlmView,
    build_user_llm,
    close_llm,
    policy_view,
    store_for,
    validate_update,
)
from autoair.ai.writers import (
    WRITER_KINDS,
    EnvironmentProfile,
    PythonCodeDisabled,
    WriteRequest,
    WriterResult,
)
from autoair.api.app import Services
from autoair.api.deps import GatewayDep, ServicesDep, policy_of
from autoair.api.features import ensure_feature
from autoair.api.routers.ai_mount import AIStatus
from autoair.auth import forwarded_credentials
from autoair.gateway import AirflowError
from autoair.spec import DagSpec
from autoair.validation import Problem, validate_spec

log = logging.getLogger(__name__)
router = APIRouter(prefix="/ai", tags=["ai"])

NOT_CONFIGURED = (
    "AI features are not configured: set AUTOAIR_ANTHROPIC_API_KEY (or ANTHROPIC_API_KEY), "
    "AUTOAIR_AI__PROVIDER + AUTOAIR_AI__API_KEY / AUTOAIR_AI__BASE_URL (openai, gemini, "
    "ollama), or AUTOAIR_AI__LLM_CONN_ID to an Airflow connection holding the provider "
    "credentials."
)
FEATURES = [
    "generate",
    "edit",
    "explain",
    "explain_dag",
    "explain_code",
    "describe",
    "review",
    "leaks",
    "optimize",
    "fix",
    "task_code",
    "write",
    "ask",
    "search",
    "research",
]
ModelField = Field(
    default=None,
    max_length=200,
    description="Model for this request (one of /ai/status `models`; default: configured).",
)


# --------------------------------------------------------------------------- request models


class GenerateRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=20_000)
    existing: DagSpec | None = Field(
        default=None, description="Current spec to modify ('edit with AI')."
    )
    max_repairs: int = Field(default=3, ge=0, le=5)
    model: str | None = ModelField


class ExplainRequest(BaseModel):
    spec: DagSpec
    problems: list[Problem] | None = Field(
        default=None, description="Problems to explain; validated server-side when omitted."
    )
    model: str | None = ModelField


class ExplainResult(BaseModel):
    explanation: str


class DescribeRequest(BaseModel):
    spec: DagSpec
    model: str | None = ModelField


class DescribeResult(BaseModel):
    doc_md: str


class ExplainDagRequest(BaseModel):
    spec: DagSpec
    node_id: str | None = Field(default=None, description="Focus on this node.")
    question: str | None = Field(default=None, max_length=4_000)
    model: str | None = ModelField


class ExplainCodeRequest(BaseModel):
    code: str = Field(min_length=1, max_length=200_000)
    language: Literal["python", "bash", "sql", "yaml", "json", "text"] = "python"
    question: str | None = Field(default=None, max_length=4_000)
    context: str | None = Field(default=None, max_length=4_000)
    model: str | None = ModelField


class ExplainCodeResult(BaseModel):
    explanation: str
    model: str | None = None
    usage: Usage | None = None


class TaskCodeRequest(BaseModel):
    description: str = Field(min_length=1, max_length=20_000)
    spec: DagSpec | None = None
    node_id: str | None = Field(default=None, description="python_task node to write for.")
    existing_code: str | None = Field(default=None, max_length=200_000)
    model: str | None = ModelField


class AnalysisRequest(BaseModel):
    spec: DagSpec
    use_ai: bool = Field(
        default=True, description="Add an LLM pass (when configured) to the deterministic checks."
    )
    model: str | None = ModelField


class OptimizeRequest(AnalysisRequest):
    dag_id: str | None = Field(
        default=None, description="Read run history of this DAG (default: spec.dag_id)."
    )
    history_runs: int = Field(default=15, ge=0, le=50)


class OptimizeReport(FindingsReport):
    stats: HistoryStats | None = None


class FixRequest(BaseModel):
    spec: dict[str, Any] = Field(description="The DagSpec document (may be invalid).")
    problems: list[SpecProblem] | None = Field(
        default=None, description="Problems to fix (default: validated server-side)."
    )
    import_errors: list[str] = Field(default_factory=list, max_length=10)
    task_log: str | None = Field(default=None, max_length=500_000)
    dag_id: str | None = None
    run_id: str | None = Field(default=None, description="Fetch the failed task's log from here.")
    task_id: str | None = None
    try_number: int | None = Field(default=None, ge=1)
    model: str | None = ModelField


class ApplyRequest(BaseModel):
    spec: DagSpec
    findings: list[Finding]
    only: list[str] | None = Field(default=None, description="Finding ids to apply (default all).")


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=20_000)
    spec: DagSpec | None = Field(default=None, description="The DAG being edited, if any.")
    model: str | None = ModelField
    research: bool = Field(default=True, description="Give the agent the research tools.")


class WritersInfo(BaseModel):
    kinds: list[str]
    environment: EnvironmentProfile


class KnowledgeContent(BaseModel):
    id: str
    content: str
    default_content: str | None = None


class KnowledgeUpload(BaseModel):
    content: str = Field(min_length=1, max_length=2_000_000)


class KnowledgeToggle(BaseModel):
    enabled: bool


class PromptInfo(BaseModel):
    name: str
    overridden: bool
    chars: int


class UserModelState(BaseModel):
    settings: UserLlmView
    policy: UserLlmPolicy


# --------------------------------------------------------------------------- per-services state


def _ai(services: Services) -> AiSettings:
    return ai_settings_of(services.settings)


def guardrails_for(services: Services) -> GuardrailPipeline:
    # Kept on the services object (no module-level strong references to it).
    return pipeline_for(services)


def _base_llm(services: Services) -> LLM | None:
    llm: LLM | None = getattr(services, "llm", None)
    return llm


def _selectable(services: Services, llm: Any) -> list[str]:
    configured = list(_ai(services).models)
    provider = str(getattr(llm, "provider", ""))
    models = (
        [llm.model, *configured] if configured else [llm.model, *SUGGESTED_MODELS.get(provider, [])]
    )
    return list(dict.fromkeys(m for m in models if m and not m.startswith("(")))


_USER_LLMS = UserLlmCache(512)


def _caller(request: Request, services: Services) -> str | None:
    """The caller's user id; one shared ``local`` user when authentication is disabled."""
    user = request_user(request)
    if not user and services.auth.name == "none" and not services.auth.app:
        return "local"
    return user


async def _user_llm(request: Request, services: Services) -> Any | None:
    """The caller's own model (``per_user`` / ``both``), or ``None`` when not set."""
    user = _caller(request, services)
    if not user:
        return None
    store = await asyncio.to_thread(store_for, services)
    stored = await asyncio.to_thread(store.get, user)
    if stored is None:
        return None
    key = (user, stored.updated_at)
    cached = _USER_LLMS.get(key)
    if cached is None:
        cached = await build_user_llm(stored, store, _ai(services))
        # LRU: evicted clients (and older versions of this user's model) are closed.
        await _USER_LLMS.put(key, cached)
    return cached


async def resolve_llm(
    request: Request, services: Services, model: str | None = None, *, required: bool = True
) -> tuple[Any | None, str]:
    """(llm, source) for this caller according to ``ai.llm_mode``."""
    mode = _ai(services).llm_mode
    llm: Any = None
    source = "central"
    if mode in ("per_user", "both"):
        try:
            llm = await _user_llm(request, services)
        except UserLlmError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        source = "user"
        if llm is None and mode == "per_user":
            if not required:
                return None, "none"
            raise HTTPException(
                status_code=428,
                detail="Configure your AI model first (AI menu > My AI model): this server "
                "uses per-user models.",
            )
    if llm is None:
        llm = _base_llm(services)
        source = "central"
        if llm is None:
            if not required:
                return None, "none"
            raise HTTPException(status_code=503, detail=NOT_CONFIGURED)
    if model and model != llm.model:
        if not _model_allowed(services, llm, model, source):
            raise HTTPException(status_code=422, detail=f"model {model!r} is not allowed")
        llm = llm.with_model(model)
    return llm, source


def _model_allowed(services: Services, llm: Any, model: str, source: str) -> bool:
    """A per-request ``model`` must be on the list for its source: ``ai.models`` (or the
    provider's suggestions) for the central model, ``ai.user_llm.allowed_models`` for a
    user's own model (an empty list there allows only the user's saved model)."""
    if source == "user":
        allowed = list(_ai(services).user_llm.allowed_models)
        if not allowed:
            return False
        return any(fnmatch.fnmatchcase(model, pattern) for pattern in allowed)
    return model in _selectable(services, llm)


def _llm_error(exc: LLMError) -> HTTPException:
    return HTTPException(status_code=502, detail=f"LLM error: {exc}")


async def _knowledge(services: Services, query: str, max_chars: int = 3_000) -> str:
    try:
        retriever = retriever_for(services)
        local = await retriever.acontext(query, k=3, kinds=["doc"], max_chars=max_chars)
    except Exception:
        log.debug("retrieval failed", exc_info=True)
        local = ""
    # Official documentation from the knowledge base (autoair.kb), when it is indexed.
    from autoair.kb.service import kb_context

    docs = await kb_context(services, query, k=2, max_chars=max_chars)
    return "\n\n---\n\n".join(p for p in (local, docs) if p)


@dataclass
class AiCall:
    """One audited AI request: the (guarded) LLM, its source and collected metadata."""

    feature: str
    llm: Any
    source: str
    notes: list[GuardrailNote] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    meter: UsageMeter | None = None
    #: Tools that actually ran / sources used / feature details, for the audit record.
    used_tools: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)


@contextlib.asynccontextmanager
async def ai_call(
    request: Request,
    services: Services,
    feature: str,
    *,
    model: str | None = None,
    required: bool = True,
    use_ai: bool = True,
    inputs: Sequence[str | None] = (),
    llm_override: Any = None,
) -> AsyncIterator[AiCall]:
    """Resolve + guard the LLM, record usage, map errors and write the AI audit log.

    ``inputs`` (the user-written text of the request) pass the full input guardrail
    pipeline first: ``ai.guardrails.max_input_chars`` and the external guardrails."""
    started = time.monotonic()
    llm: Any = None
    source = "none"
    if llm_override is not None:
        llm, source = llm_override, "user"
    elif use_ai:
        llm, source = await resolve_llm(request, services, model, required=required)
    notes: list[GuardrailNote] = []
    if llm is not None:
        llm = llm.guarded(guardrails_for(services), notes)
    call = AiCall(feature, llm, source, notes)
    error: BaseException | None = None
    with track_usage() as meter:
        call.meter = meter
        try:
            await _guard_inputs(services, inputs, notes)
            yield call
        except PythonCodeDisabled as exc:
            error = exc
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except LLMError as exc:
            error = exc
            raise _llm_error(exc) from exc
        except GuardrailViolation as exc:
            error = exc
            raise HTTPException(status_code=422, detail=f"blocked by guardrail: {exc}") from exc
        except UserLlmError as exc:
            error = exc
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except BaseException as exc:
            error = exc
            raise
        finally:
            if call.llm is not None or error is not None:
                _record(request, services, call, meter.total, started, error)


async def _guard_inputs(
    services: Services, inputs: Sequence[str | None], notes: list[GuardrailNote]
) -> None:
    text = "\n\n".join(t for t in inputs if t)
    if text:
        await guard_input(guardrails_for(services), text, notes)


def _record(
    request: Request,
    services: Services,
    call: AiCall,
    usage: Usage,
    started: float,
    error: BaseException | None,
) -> None:
    outcome = "ok"
    if isinstance(error, asyncio.CancelledError):
        outcome = "cancelled"
    elif error is not None:
        outcome = "error"
    try:
        audit_for(services).submit(
            AiAuditRecord(
                timestamp=now_iso(),
                user=request_user(request),
                role=request_role(request),
                feature=call.feature,
                provider=str(getattr(call.llm, "provider", "")) or None,
                model=str(getattr(call.llm, "model", "")) or None,
                llm_source=call.source,
                usage=usage,
                tools=sorted(set(call.used_tools or call.tools)),
                guardrails=sorted({f"{n.guardrail}:{n.action}" for n in call.notes}),
                duration_ms=int((time.monotonic() - started) * 1000),
                outcome=outcome,
                error=(str(getattr(error, "detail", "") or error)[:300] if error else None),
                sources=list(dict.fromkeys(call.sources))[:100],
                detail=dict(call.detail),
            )
        )
    except Exception:
        log.warning("AI audit record failed", exc_info=True)


async def _sse(request: Request, events: AsyncIterator[BaseModel], services: Services) -> Any:
    heartbeat = _ai(services).stream_heartbeat_seconds

    async def body() -> AsyncIterator[bytes]:
        iterator = events.__aiter__()
        pending: asyncio.Task[BaseModel] | None = None
        try:
            while True:
                if pending is None:
                    pending = asyncio.ensure_future(iterator.__anext__())
                done, _ = await asyncio.wait({pending}, timeout=heartbeat)
                if not done:
                    yield b": keep-alive\n\n"
                    continue
                try:
                    event = pending.result()
                except StopAsyncIteration:
                    return
                pending = None
                yield encode_sse(event)
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                with contextlib.suppress(BaseException):
                    await pending
            aclose = getattr(iterator, "aclose", None)
            if callable(aclose):
                with contextlib.suppress(BaseException):
                    await aclose()

    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _streamed(
    request: Request,
    services: Services,
    feature: str,
    model: str | None,
    job: Any,
    kind: str,
    inputs: Sequence[str | None] = (),
) -> Any:
    """Resolve the LLM and check the inputs up front (HTTP errors before the stream
    starts), then stream."""
    started = time.monotonic()
    llm, source = await resolve_llm(request, services, model)
    assert llm is not None  # required=True
    notes: list[GuardrailNote] = []
    call = AiCall(feature, llm.guarded(guardrails_for(services), notes), source, notes)
    try:
        await _guard_inputs(services, inputs, notes)
    except GuardrailViolation as exc:
        _record(request, services, call, Usage(), started, exc)
        raise HTTPException(status_code=422, detail=f"blocked by guardrail: {exc}") from exc
    meter_box: dict[str, UsageMeter] = {}

    async def run(emit: Emit) -> Any:
        with track_usage() as meter:
            meter_box["m"] = meter
            return await job(call, emit)

    def finish(value: Any, error: BaseException | None) -> None:
        meter = meter_box.get("m")
        _record(request, services, call, meter.total if meter else Usage(), started, error)

    return await _sse(request, stream_job(run, kind, on_finish=finish), services)


def _require_admin(request: Request) -> None:
    # AI administration (knowledge base, audit log, prompts) requires the IAM ``ai:admin``
    # permission: request_role() maps it to the "admin" AI role (autoair.ai.access).
    if request_role(request) != "admin":
        raise HTTPException(status_code=403, detail=f"requires the {ADMIN_PERMISSION} permission")


AdminDep = Depends(_require_admin)


def _require_user(request: Request, services: ServicesDep) -> str:
    user = _caller(request, services)
    if not user:
        raise HTTPException(
            status_code=401, detail="a signed-in user is required for personal AI settings"
        )
    return user


UserDep = Annotated[str, Depends(_require_user)]


# --------------------------------------------------------------------------- status


@router.get("/status", operation_id="aiStatus")
async def ai_status(request: Request, services: ServicesDep) -> AIStatus:
    ai = _ai(services)
    retrieval: Literal["bm25", "hybrid"] = "hybrid" if ai.embedding.provider else "bm25"
    user_configured = False
    llm: Any = None
    source: Literal["central", "user", "none"] = "none"
    try:
        llm, src = await resolve_llm(request, services, required=False)
        source = "user" if src == "user" else ("central" if llm is not None else "none")
        user_configured = src == "user" and llm is not None
    except HTTPException:
        llm = None
    base = AIStatus(
        configured=llm is not None,
        retrieval=retrieval,
        reranker=ai.retrieval.reranker,
        llm_mode=ai.llm_mode,
        llm_source=source,
        user_model_configured=user_configured,
        research_online=ai.research.online,
        writers=list(WRITER_KINDS),
        role=request_role(request),
    )
    if llm is None:
        base.features = ["review", "leaks", "optimize", "search", "research"]
        return base
    base.provider = str(getattr(llm, "provider", "")) or None
    base.model = llm.model
    base.models = _selectable(services, llm) if source == "central" else [llm.model]
    base.features = FEATURES
    return base


# --------------------------------------------------------------------------- endpoints


async def _thread(fn: Any, *args: Any, **kwargs: Any) -> Any:
    return await run_in_threadpool(fn, *args, **kwargs)


@router.post("/generate", operation_id="generateDag")
async def generate(
    body: GenerateRequest, request: Request, services: ServicesDep
) -> GenerationResult:
    async with ai_call(
        request, services, "generate", model=body.model, inputs=[body.prompt]
    ) as call:
        result: GenerationResult = await _thread(
            generate_spec,
            body.prompt,
            catalog=services.catalog,
            registry=services.registry,
            llm=call.llm,
            existing=body.existing,
            max_repairs=body.max_repairs,
            policy=policy_of(services),
        )
    return result


@router.post("/explain", operation_id="explainProblems")
async def explain(body: ExplainRequest, request: Request, services: ServicesDep) -> ExplainResult:
    async with ai_call(request, services, "explain", model=body.model) as call:
        problems = body.problems
        if problems is None:
            report = await _thread(
                validate_spec,
                body.spec,
                catalog=services.catalog,
                registry=services.registry,
                policy=policy_of(services),
            )
            problems = report.problems
        text: str = await _thread(explain_problems, body.spec, problems, call.llm)
    return ExplainResult(explanation=text)


@router.post("/describe", operation_id="describeDag")
async def describe(
    body: DescribeRequest, request: Request, services: ServicesDep
) -> DescribeResult:
    async with ai_call(request, services, "describe", model=body.model) as call:
        text: str = await _thread(describe_dag, body.spec, call.llm)
    return DescribeResult(doc_md=text)


@router.post("/explain-dag", operation_id="explainDag")
async def explain_dag_endpoint(
    body: ExplainDagRequest, request: Request, services: ServicesDep
) -> DagExplanation:
    async with ai_call(
        request, services, "explain_dag", model=body.model, inputs=[body.question]
    ) as call:
        result: DagExplanation = await _thread(
            explain_dag,
            body.spec,
            llm=call.llm,
            catalog=services.catalog,
            focus_node=body.node_id,
            question=body.question,
        )
    return result


def _explain_code_messages(body: ExplainCodeRequest) -> list[ChatMessage]:
    limit = _active_ai().assist.max_code_chars
    parts = [f"Language: {body.language}", f"```{body.language}\n{body.code[:limit]}\n```"]
    if body.context:
        parts.insert(0, f"Context: {body.context[:2000]}")
    if body.question:
        parts.append(f"Question: {body.question}")
    return [{"role": "user", "content": "\n\n".join(parts)}]


@router.post("/explain-code", operation_id="explainCode")
async def explain_code_endpoint(
    body: ExplainCodeRequest, request: Request, services: ServicesDep
) -> ExplainCodeResult:
    from autoair.ai import prompts

    async with ai_call(
        request,
        services,
        "explain_code",
        model=body.model,
        inputs=[body.code, body.question, body.context],
    ) as call:
        text = await call.llm.acomplete_text(
            prompts.get("EXPLAIN_CODE_SYSTEM"),
            _explain_code_messages(body),
            _active_ai().assist.explain_code_max_tokens,
        )
        assert call.meter is not None
        usage = call.meter.total
    return ExplainCodeResult(explanation=text, model=call.llm.model, usage=usage)


@router.post("/explain-code/stream", operation_id="explainCodeStream")
async def explain_code_stream(
    body: ExplainCodeRequest, request: Request, services: ServicesDep
) -> StreamingResponse:
    """Token-streamed explanation (``text/event-stream``; result kind ``explain_code``)."""
    from autoair.ai import prompts

    async def job(call: AiCall, emit: Emit) -> ExplainCodeResult:
        text = ""
        with track_usage() as meter:
            async for delta in call.llm.stream_text(
                prompts.get("EXPLAIN_CODE_SYSTEM"),
                _explain_code_messages(body),
                _active_ai().assist.explain_code_max_tokens,
            ):
                emit(TokenEvent(text=delta))
                text += delta
        if not text.strip():
            raise LLMError("the model returned an empty answer")
        return ExplainCodeResult(explanation=text.strip(), model=call.llm.model, usage=meter.total)

    response: StreamingResponse = await _streamed(
        request,
        services,
        "explain_code",
        body.model,
        job,
        "explain_code",
        inputs=[body.code, body.question, body.context],
    )
    return response


@router.post("/task-code", operation_id="writeTaskCode")
async def task_code(
    body: TaskCodeRequest, request: Request, services: ServicesDep
) -> TaskCodeResult:
    async with ai_call(
        request,
        services,
        "task_code",
        model=body.model,
        inputs=[body.description, body.existing_code],
    ) as call:
        result: TaskCodeResult = await _thread(
            write_task_code,
            body.description,
            llm=call.llm,
            spec=body.spec,
            node_id=body.node_id,
            existing_code=body.existing_code,
            catalog=services.catalog,
            conventions=await asyncio.to_thread(conventions_for, services),
        )
    return result


@router.post("/review", operation_id="reviewDag")
async def review(body: AnalysisRequest, request: Request, services: ServicesDep) -> FindingsReport:
    async with ai_call(
        request, services, "review", model=body.model, required=False, use_ai=body.use_ai
    ) as call:
        knowledge = (
            await _knowledge(services, "airflow 3 best practices idempotency retries")
            if call.llm
            else ""
        )
        report: FindingsReport = await _thread(
            review_dag,
            body.spec,
            catalog=services.catalog,
            registry=services.registry,
            llm=call.llm,
            policy=policy_of(services),
            conventions=await asyncio.to_thread(conventions_for, services),
            knowledge=guardrails_for(services).untrusted(knowledge, "knowledge base")
            if knowledge
            else "",
        )
    return report


@router.post("/leaks", operation_id="scanLeaks")
async def leaks(body: AnalysisRequest, request: Request, services: ServicesDep) -> FindingsReport:
    async with ai_call(
        request, services, "leaks", model=body.model, required=False, use_ai=body.use_ai
    ) as call:
        report: FindingsReport = await _thread(
            scan_dag_leaks,
            body.spec,
            catalog=services.catalog,
            registry=services.registry,
            llm=call.llm,
            policy=policy_of(services),
        )
    return report


@router.post("/optimize", operation_id="optimizeDag")
async def optimize(
    body: OptimizeRequest, request: Request, services: ServicesDep, gateway: GatewayDep
) -> OptimizeReport:
    stats: HistoryStats | None = None
    note: str | None = None
    if body.history_runs:
        dag_id = body.dag_id or body.spec.dag_id
        try:
            history = await fetch_history(gateway, dag_id, body.history_runs)
            stats = history_stats(dag_id, history)
        except AirflowError as exc:
            note = f"Run history unavailable ({exc.detail})."
    async with ai_call(
        request, services, "optimize", model=body.model, required=False, use_ai=body.use_ai
    ) as call:
        report: FindingsReport = await _thread(
            optimize_dag,
            body.spec,
            catalog=services.catalog,
            registry=services.registry,
            stats=stats,
            llm=call.llm,
            policy=policy_of(services),
            conventions=await asyncio.to_thread(conventions_for, services),
        )
    result = OptimizeReport.model_validate({**report.model_dump(), "stats": stats})
    if note:
        result.notes.insert(0, note)
    return result


@router.post("/fix", operation_id="fixDag")
async def fix(
    body: FixRequest, request: Request, services: ServicesDep, gateway: GatewayDep
) -> FindingsReport:
    async with ai_call(request, services, "fix", model=body.model) as call:
        log_text = body.task_log
        task_id = body.task_id
        if log_text is None and body.run_id and task_id:
            ensure_feature(request, "airflow")  # the log is read from Airflow
            dag_id = body.dag_id or str(body.spec.get("dag_id") or "")
            try:
                try_number = body.try_number
                if try_number is None:
                    tis = await gateway.task_instances(dag_id, body.run_id)
                    ti = next((t for t in tis if t.task_id == task_id), None)
                    try_number = await gateway.latest_log_try(ti) if ti else 1
                log_text = await gateway.task_log(dag_id, body.run_id, task_id, try_number)
            except AirflowError as exc:
                raise HTTPException(
                    status_code=502, detail=f"cannot read the task log: {exc.detail}"
                ) from exc
        evidence = " ".join(
            [
                *(p.message for p in (body.problems or [])[:5]),
                *(body.import_errors[:2]),
                (log_text or "")[-500:],
            ]
        )
        knowledge = await _knowledge(services, evidence[:1000]) if evidence.strip() else ""
        guards = guardrails_for(services)
        report: FindingsReport = await _thread(
            fix_dag,
            body.spec,
            llm=call.llm,
            catalog=services.catalog,
            registry=services.registry,
            policy=policy_of(services),
            problems=body.problems,
            import_errors=body.import_errors,
            # Logs are untrusted input: redacted and delimited before the model sees them.
            task_log=guards.untrusted(log_text, "task log") if log_text else None,
            task_id=task_id,
            conventions=await asyncio.to_thread(conventions_for, services),
            knowledge=guards.untrusted(knowledge, "knowledge base") if knowledge else "",
        )
    return report


@router.post("/apply", operation_id="applyFindings")
async def apply(body: ApplyRequest, services: ServicesDep) -> AppliedPatches:
    """Apply the patches of the selected findings and validate the result."""
    result: AppliedPatches = await run_in_threadpool(
        apply_findings,
        body.spec,
        body.findings,
        catalog=services.catalog,
        registry=services.registry,
        policy=policy_of(services),
        only=body.only,
    )
    return result


@router.get("/search", operation_id="aiSearch")
async def search(
    services: ServicesDep,
    q: Annotated[str, Query(min_length=1, max_length=500)],
    kinds: Annotated[list[Literal["operator", "doc", "spec", "template"]] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> list[Hit]:
    """Hybrid retrieval over operators, docs, saved DAGs and templates."""
    hits: list[Hit] = await retriever_for(services).asearch(q, limit, kinds)
    return hits


@router.get("/research/search", operation_id="aiResearchSearch")
async def research_search(
    services: ServicesDep,
    q: Annotated[str, Query(min_length=1, max_length=500)],
    scope: Annotated[list[Literal["installed", "knowledge", "web"]] | None, Query()] = None,
    package: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=30)] = 8,
) -> list[Hit]:
    """Search installed provider docs / examples and the knowledge base (air-gapped)."""
    from autoair.ai.research.tools import SCOPE_KINDS, research_for

    ctx = await asyncio.to_thread(research_for, services)
    scopes: list[str] = list(scope) if scope else list(SCOPE_KINDS)
    kinds = sorted({k for s in scopes for k in SCOPE_KINDS[s]})
    filters = {"package": package.lower()} if package else None
    hits: list[Hit] = await ctx.retriever.asearch(q, limit, kinds, filters)
    return hits


# --------------------------------------------------------------------------- agent


def _agent_tools(
    request: Request, services: Services, research: bool, llm: Any = None
) -> list[AgentTool]:
    headers, cookies = forwarded_credentials(request)
    tools = build_toolbox(
        services,
        gateway_factory=gateway_factory_for(
            services.settings.airflow_url,
            headers,
            cookies,
            timeout=services.settings.airflow_timeout,
        ),
    )
    if research:
        from autoair.ai.research.tools import research_tools

        # The caller's LLM (not the central one) rewrites queries / reranks hits.
        tools += research_tools(services, llm=llm)
    from autoair.ai.approvals import withdraw_gated

    ai = _ai(services)
    selected: list[AgentTool] = executable_tools(
        filter_tools(tools, request_role(request), ai.guardrails.tool_allowlist),
        lambda permission: has_permission(request, permission),
    )
    # A one-shot answer cannot pause for a decision: tools needing approval are not offered.
    return withdraw_gated(selected, ai.approvals, "developer")


async def _agent_task(
    request: Request, services: Services, body: AskRequest, call: AiCall, emit: Emit | None
) -> AgentTask:
    from autoair.ai.assist import compact_spec

    ai = _ai(services)
    context_parts = []
    node_ids: set[str] = set()
    if body.spec is not None:
        context_parts.append("The DAG the user is editing:\n" + compact_spec(body.spec))
        node_ids = {n.id for n in body.spec.nodes}
    conventions = await asyncio.to_thread(conventions_for, services, user=request_user(request))
    if conventions:
        context_parts.append("Team conventions:\n" + "\n".join(f"- {c}" for c in conventions))
    tools = _agent_tools(request, services, body.research, call.llm)
    call.tools = [t.name for t in tools]
    task = AgentTask(
        llm=call.llm,
        task=body.question,
        tools=tools,
        context="\n\n".join(context_parts),
        budget=Budget(
            max_steps=ai.agent.max_steps,
            max_tokens=ai.agent.token_budget,
            max_cost_usd=ai.agent.cost_budget_usd,
            timeout_seconds=ai.agent.timeout_seconds,
        ),
        max_tokens=ai.agent.max_tokens_per_call,
        tool_timeout=ai.agent.tool_timeout_seconds,
        max_tool_chars=ai.agent.max_tool_output_chars,
        catalog=services.catalog,
        registry=services.registry,
        policy=policy_of(services),
        node_ids=node_ids,
        guardrails=guardrails_for(services),
        final_turn_grace_seconds=ai.agent.final_turn_grace_seconds,
    )
    if emit is not None:
        task.emit = emit
    else:
        task.stream = False
    return task


@router.post("/ask", operation_id="askAssistant")
async def ask(body: AskRequest, request: Request, services: ServicesDep) -> AgentResult:
    """Open-ended question answered by the tool-using agent (read-only tools)."""
    async with ai_call(request, services, "ask", model=body.model, inputs=[body.question]) as call:
        task = await _agent_task(request, services, body, call, None)
        result = await arun_agent(task)
    return result


@router.post("/ask/stream", operation_id="askAssistantStream")
async def ask_stream(
    body: AskRequest, request: Request, services: ServicesDep
) -> StreamingResponse:
    """The agent as a stream: tokens, tool calls/results, usage, then the ``agent`` result."""

    async def job(call: AiCall, emit: Emit) -> AgentResult:
        task = await _agent_task(request, services, body, call, emit)
        return await arun_agent(task)

    response: StreamingResponse = await _streamed(
        request, services, "ask", body.model, job, "agent", inputs=[body.question]
    )
    return response


# --------------------------------------------------------------------------- writers


@router.get("/writers", operation_id="aiWriters")
async def writers_info(services: ServicesDep) -> WritersInfo:
    """Writer kinds and the installed environment their prompts are built from."""
    from autoair.ai.writers import environment_profile

    profile = await asyncio.to_thread(environment_profile, services)
    return WritersInfo(kinds=list(WRITER_KINDS), environment=profile)


@router.post("/write", operation_id="aiWrite")
async def write(body: WriteRequest, request: Request, services: ServicesDep) -> WriterResult:
    """Write code with a specialised writer (validate-and-repair loop)."""
    from autoair.ai.writers import run_writer

    async with ai_call(
        request,
        services,
        f"write:{body.kind}",
        model=body.model,
        inputs=[body.description, body.existing],
    ) as call:
        result = await run_writer(
            body, llm=call.llm, services=services, guardrails=guardrails_for(services)
        )
    return result


@router.post("/write/stream", operation_id="aiWriteStream")
async def write_stream(
    body: WriteRequest, request: Request, services: ServicesDep
) -> StreamingResponse:
    from autoair.ai.writers import run_writer

    async def job(call: AiCall, emit: Emit) -> WriterResult:
        return await run_writer(
            body, llm=call.llm, services=services, emit=emit, guardrails=guardrails_for(services)
        )

    response: StreamingResponse = await _streamed(
        request,
        services,
        f"write:{body.kind}",
        body.model,
        job,
        "writer",
        inputs=[body.description, body.existing],
    )
    return response


# --------------------------------------------------------------------------- my model


@router.get("/me/model", operation_id="getMyAiModel")
async def get_my_model(user: UserDep, services: ServicesDep) -> UserModelState:
    store = await asyncio.to_thread(store_for, services)
    view = await asyncio.to_thread(store.view, user)
    return UserModelState(settings=view, policy=policy_view(_ai(services)))


@router.put("/me/model", operation_id="setMyAiModel")
async def set_my_model(body: UserLlmUpdate, user: UserDep, services: ServicesDep) -> UserModelState:
    ai = _ai(services)
    if ai.llm_mode == "central":
        raise HTTPException(status_code=409, detail="this server uses one central AI model")
    store = await asyncio.to_thread(store_for, services)
    try:
        await validate_update(body, ai.user_llm)
    except UserLlmError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    view = await asyncio.to_thread(store.put, user, body)
    return UserModelState(settings=view, policy=policy_view(ai))


@router.delete("/me/model", operation_id="deleteMyAiModel")
async def delete_my_model(user: UserDep, services: ServicesDep) -> UserModelState:
    store = await asyncio.to_thread(store_for, services)
    await asyncio.to_thread(store.delete, user)
    return UserModelState(settings=UserLlmView(), policy=policy_view(_ai(services)))


@router.post("/me/model/test", operation_id="testMyAiModel")
async def test_my_model(
    request: Request,
    services: ServicesDep,
    body: UserLlmUpdate | None = None,  # noqa: PT028 - an endpoint, not a test
) -> ExplainResult:
    """Send a tiny prompt with the caller's effective model, or - with a body - with the
    unsaved model settings of the form (validated like a save, never stored)."""
    draft: Any = None
    if body is not None:
        ai = _ai(services)
        if ai.llm_mode == "central":
            raise HTTPException(status_code=409, detail="this server uses one central AI model")
        user = _require_user(request, services)
        try:
            await validate_update(body, ai.user_llm)
            store = await asyncio.to_thread(store_for, services)
            stored = await asyncio.to_thread(store.preview, user, body)
            draft = await build_user_llm(stored, store, ai)
        except UserLlmError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        async with ai_call(request, services, "model_test", llm_override=draft) as call:
            text = await call.llm.acomplete_text(
                "You are a connectivity check. Answer with one short sentence.",
                [{"role": "user", "content": "Say hello and name the model you are."}],
                60,
            )
    finally:
        if draft is not None:
            await close_llm(draft)
    return ExplainResult(explanation=text)


# --------------------------------------------------------------------------- knowledge (admin)


def _kb_error(exc: KnowledgeError) -> HTTPException:
    status = 404 if "no knowledge document" in str(exc) else 422
    return HTTPException(status_code=status, detail=str(exc))


def _invalidate_retrieval(services: Services) -> None:
    with contextlib.suppress(Exception):
        retriever_for(services).invalidate()
    with contextlib.suppress(Exception):
        from autoair.ai.research.tools import research_for

        research_for(services).retriever.invalidate()


@router.get("/knowledge", operation_id="listAiKnowledge", dependencies=[AdminDep])
async def list_knowledge(services: ServicesDep) -> list[KnowledgeDoc]:
    docs: list[KnowledgeDoc] = await asyncio.to_thread(knowledge_for(services).entries)
    return docs


@router.get("/knowledge/{doc_id}", operation_id="getAiKnowledge", dependencies=[AdminDep])
async def get_knowledge(doc_id: str, services: ServicesDep) -> KnowledgeContent:
    kb = knowledge_for(services)
    try:
        content = await asyncio.to_thread(kb.content, doc_id)
    except KnowledgeError as exc:
        raise _kb_error(exc) from exc
    try:
        default: str | None = await asyncio.to_thread(kb.content, doc_id, default=True)
    except KnowledgeError:
        default = None
    return KnowledgeContent(id=doc_id, content=content, default_content=default)


@router.put("/knowledge/{doc_id}", operation_id="putAiKnowledge", dependencies=[AdminDep])
async def put_knowledge(
    doc_id: str, body: KnowledgeUpload, request: Request, services: ServicesDep
) -> KnowledgeDoc:
    """Upload a replacement of a built-in document, or a new custom document."""
    try:
        doc: KnowledgeDoc = await asyncio.to_thread(
            knowledge_for(services).put, doc_id, body.content
        )
    except KnowledgeError as exc:
        raise _kb_error(exc) from exc
    services.audit.record("ai_knowledge_put", user=request_user(request), doc=doc.id)
    _invalidate_retrieval(services)
    return doc


@router.post(
    "/knowledge/{doc_id}/enabled", operation_id="toggleAiKnowledge", dependencies=[AdminDep]
)
async def toggle_knowledge(
    doc_id: str, body: KnowledgeToggle, request: Request, services: ServicesDep
) -> KnowledgeDoc:
    try:
        doc: KnowledgeDoc = await asyncio.to_thread(
            knowledge_for(services).set_enabled, doc_id, body.enabled
        )
    except KnowledgeError as exc:
        raise _kb_error(exc) from exc
    services.audit.record(
        "ai_knowledge_toggle", user=request_user(request), doc=doc.id, enabled=body.enabled
    )
    _invalidate_retrieval(services)
    return doc


@router.delete("/knowledge/{doc_id}", operation_id="resetAiKnowledge", dependencies=[AdminDep])
async def reset_knowledge(
    doc_id: str, request: Request, services: ServicesDep
) -> KnowledgeDoc | None:
    """Back to the default: delete the override and UI toggles (custom docs are removed)."""
    try:
        doc = await asyncio.to_thread(knowledge_for(services).reset, doc_id)
    except KnowledgeError as exc:
        raise _kb_error(exc) from exc
    services.audit.record("ai_knowledge_reset", user=request_user(request), doc=doc_id)
    _invalidate_retrieval(services)
    return doc


# --------------------------------------------------------------------------- audit / prompts


@router.get("/audit", operation_id="aiAuditLog", dependencies=[AdminDep])
async def ai_audit(
    services: ServicesDep,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    user: Annotated[str | None, Query(max_length=200)] = None,
) -> list[AiAuditRecord]:
    records: list[AiAuditRecord] = await asyncio.to_thread(audit_for(services).recent, limit, user)
    return records


@router.get("/prompts", operation_id="aiPrompts", dependencies=[AdminDep])
async def ai_prompts() -> list[PromptInfo]:
    from autoair.ai import prompts

    return [
        PromptInfo(name=n, overridden=prompts.overridden(n), chars=len(prompts.get(n)))
        for n in prompts.names()
    ]


# --------------------------------------------------------------------------- mounting


def include_ai_routers(parent: APIRouter, services: Services, deps: Sequence[Any]) -> None:
    """The in-process AI routes (main app and the separate AI service)."""
    from autoair.ai.assistants import project_rules
    from autoair.api.routers import ai_assistants

    project_rules.install(services)
    parent.include_router(router, dependencies=list(deps))
    parent.include_router(ai_assistants.router, dependencies=list(deps))


__all__ = ["include_ai_routers", "resolve_llm", "router"]
