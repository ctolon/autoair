"""``/api/ai/assistant/*``, ``/api/ai/troubleshoot/*``, ``/api/ai/upgrade``,
``/api/ai/deploy-message``, ``/api/ai/rules`` and ``/api/ai/mcp/*``.

* Chat assistants (developer: ``ai:use``; admin: ``ai:admin`` or ``admin:panel``): one
  streamed turn per request, persisted threads (list / rename / delete), regenerate,
  proposal decisions. Each turn is one AI audit record (tools, sources, thread).
* Troubleshooting (``ai:use`` + ``airflow:read``): a streamed, scoped analysis of a DAG
  run; stored analyses; approve / reject a proposal (approve needs ``spec:write``) and
  report that it was applied (deployed through the normal deploy pipeline); execute or
  reject a proposed Airflow action (``run:manage`` + ``features.runs``, per
  ``ai.troubleshoot.airflow_actions``).
* Tool approvals (``ai.approvals``): a run that stopped because the agent called a tool
  needing approval is listed under ``/api/ai/approvals``; ``POST
  /api/ai/approvals/{id}/stream`` decides its calls and resumes the run as a stream that
  ends like the original one (``assistant_turn`` or ``troubleshoot``). Only the user who
  started the run can see or decide it.
* Upgrade assistant, commit/PR message generation, project rules and the external MCP
  servers users may attach.

Mounted with the AI router (``features.ai``; proxied with it to a separate AI service).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from autoair.ai.access import has_permission, request_role, request_user
from autoair.ai.agent import AgentTask, ResumeState, arun_agent
from autoair.ai.approvals import (
    ApprovalBusy,
    ApprovalExpired,
    ApprovalNotFound,
    ToolApproval,
    ToolApprovalView,
    ToolDecision,
    approval_store_for,
    gated_tools,
    new_approval,
    refresh_views,
    replica_id,
)
from autoair.ai.approvals import new_id as new_claim_id
from autoair.ai.assistants.airflow_actions import (
    ACTION_FEATURE,
    ACTION_PERMISSION,
    ActionBusy,
    ActionResult,
    ActionsMode,
    AirflowAction,
    BlockReason,
    audit_view,
    block_reason,
    blocked_result,
    effective_mode,
    execute_action,
    record_result,
)
from autoair.ai.assistants.chat import (
    ChatContext,
    ChatTurnRequest,
    ChatTurnResult,
    decisions_text,
    mark_approval,
    resume_turn,
    run_turn,
)
from autoair.ai.assistants.compaction import dump_history
from autoair.ai.assistants.deploy_message import (
    DeployMessage,
    DeployMessageRequest,
    deploy_message,
)
from autoair.ai.assistants.dev_tools import TurnState
from autoair.ai.assistants.mcp_client import (
    McpError,
    McpToolInfo,
    attach_mcp_tools,
    list_server_tools,
    server_by_name,
    servers_for,
)
from autoair.ai.assistants.project_rules import ProjectRules, RulesUpdate, rules_store_for
from autoair.ai.assistants.sources import SourceCollector
from autoair.ai.assistants.threads import (
    ChatThread,
    Citation,
    Proposal,
    ProposalStatus,
    ThreadNotFound,
    ThreadSummary,
    now_iso,
    thread_store_for,
)
from autoair.ai.assistants.troubleshoot import (
    AnalysisStore,
    AnalysisSummary,
    Decision,
    EvidencePack,
    TroubleshootAnalysis,
    TroubleshootFinal,
    TroubleshootRequest,
    TroubleshootScope,
    analysis_store_for,
    build_analysis,
    collect_evidence,
    effective_scope,
    troubleshoot_tools,
)
from autoair.ai.assistants.upgrade import UpgradeReport, UpgradeRequest, upgrade_dag
from autoair.ai.audit_log import AiAuditRecord, audit_for
from autoair.ai.audit_log import now_iso as audit_now
from autoair.ai.events import Emit, StatusEvent
from autoair.ai.findings import AppliedPatches, apply_findings
from autoair.ai.guardrails import Budget, filter_tools
from autoair.ai.rules import scrub_secrets
from autoair.ai.toolbox import gateway_factory_for
from autoair.api.deps import ServicesDep, policy_of
from autoair.api.features import ensure_feature, feature_enabled
from autoair.api.routers.ai import (
    AdminDep,
    AiCall,
    _ai,
    _caller,
    _streamed,
    ai_call,
    guardrails_for,
)
from autoair.auth import forwarded_credentials
from autoair.gateway import AirflowError
from autoair.iam import model as P
from autoair.persistence.documents import DocumentConflict
from autoair.secret_mask import mask
from autoair.spec import DagSpec

log = logging.getLogger(__name__)
router = APIRouter(prefix="/ai", tags=["ai"])

#: Tools shared with the other agents: the role allow-list (ai.guardrails.tool_allowlist)
#: applies to them; the troubleshooting tools are governed by ai.troubleshoot.* instead.
_SHARED = frozenset(
    {
        "search_catalog",
        "get_operator",
        "validate_spec",
        "lint_spec",
        "search_docs",
        "read_source",
        "list_installed_packages",
        "get_package_info",
        "fetch_doc",
    }
)


# --------------------------------------------------------------------------- models


class McpServerOption(BaseModel):
    name: str
    description: str = ""
    tools: list[str] = Field(default_factory=list, description="Allow-list patterns.")


class AssistantModeInfo(BaseModel):
    enabled: bool
    allowed: bool
    tools: list[str] = Field(default_factory=list)
    mcp_servers: list[McpServerOption] = Field(
        default_factory=list,
        description="External MCP servers attached to this assistant's turns (by default all "
        "of them; send `mcp` in the chat request to choose).",
    )


class AssistantInfo(BaseModel):
    developer: AssistantModeInfo
    admin: AssistantModeInfo
    troubleshoot: bool
    compaction_trigger_chars: int
    max_follow_ups: int


class ThreadRename(BaseModel):
    title: str = Field(min_length=1, max_length=120)


class ProposalDecision(BaseModel):
    decision: Literal["approve", "reject", "applied"]
    comment: str | None = Field(default=None, max_length=2_000)
    detail: dict[str, Any] = Field(
        default_factory=dict, description="applied: deploy outcome (version, PR url…)."
    )


class TroubleshootOptions(BaseModel):
    enabled: bool
    allowed: TroubleshootScope = Field(description="What the server allows at most.")
    defaults: TroubleshootScope
    filesystem_roots: list[str] = Field(default_factory=list)
    online_available: bool = False
    mcp_servers: list[McpServerOption] = Field(default_factory=list)
    actions_mode: ActionsMode = Field(
        default="read_only",
        description="The most permissive Airflow-action mode this caller may choose "
        "(ai.troubleshoot.airflow_actions; read_only without run:manage or features.runs).",
    )
    allowed_actions: list[str] = Field(
        default_factory=list, description="Action types that may be executed."
    )
    max_actions: int = Field(default=0, description="Executable actions per analysis.")


class AnalysisDecision(BaseModel):
    decision: Literal["approve", "reject"]
    finding_ids: list[str] | None = Field(
        default=None, description="Findings to apply (default: every patch)."
    )
    comment: str | None = Field(default=None, max_length=2_000)


class ActionDecision(BaseModel):
    decision: Literal["execute", "reject"]
    comment: str | None = Field(default=None, max_length=2_000)


class AnalysisApplied(BaseModel):
    deployed: bool = True
    detail: dict[str, Any] = Field(
        default_factory=dict, description="Deploy outcome: version, etag, PR url, environment."
    )


class DecisionResult(BaseModel):
    analysis: TroubleshootAnalysis
    applied: AppliedPatches | None = None
    code_after: str | None = None


class ToolApprovalDecision(BaseModel):
    decisions: list[ToolDecision] = Field(
        min_length=1,
        max_length=50,
        description="One decision per pending tool call (approve, or deny with an optional "
        "reason that is sent to the model).",
    )


class EffectiveRules(BaseModel):
    team: str | None = None
    text: str = ""


# --------------------------------------------------------------------------- helpers


def _user(request: Request, services: Any) -> str:
    user = _caller(request, services)
    if not user:
        raise HTTPException(status_code=401, detail="a signed-in user is required")
    return user


def _can_admin(request: Request) -> bool:
    return has_permission(request, P.AI_ADMIN) or has_permission(request, P.ADMIN_PANEL)


def _gateway_factory(request: Request, services: Any) -> Any:
    headers, cookies = forwarded_credentials(request)
    return gateway_factory_for(
        services.settings.airflow_url,
        headers,
        cookies,
        timeout=services.settings.airflow_timeout,
    )


def _audit_decision(
    request: Request,
    services: Any,
    feature: str,
    *,
    model: str | None,
    detail: dict[str, Any],
    sources: list[str] | None = None,
    tools: list[str] | None = None,
) -> None:
    """A decision (approve / reject / applied) in the AI audit log and the audit trail."""
    try:
        audit_for(services).submit(
            AiAuditRecord(
                timestamp=audit_now(),
                user=request_user(request),
                role=request_role(request),
                feature=feature,
                model=model,
                llm_source="none",
                tools=sorted(set(tools or [])),
                sources=list(sources or [])[:100],
                detail=detail,
            )
        )
    except Exception:  # auditing must not break the request
        log.warning("AI audit record of %s failed", feature, exc_info=True)
    services.audit.record(
        feature.replace(":", "_"),
        user=request_user(request),
        dag_id=detail.get("dag_id"),
        **{k: v for k, v in detail.items() if k != "dag_id"},
    )


# --------------------------------------------------------------------------- assistant


@router.get("/assistant/info", operation_id="assistantInfo")
async def assistant_info(request: Request, services: ServicesDep) -> AssistantInfo:
    ai = _ai(services)

    def servers(mode: Literal["developer", "admin"], allowed: bool) -> list[McpServerOption]:
        if not allowed or not getattr(ai.assistants, mode).allow_external_mcp:
            return []
        return [
            McpServerOption(name=s.name, description=s.description, tools=list(s.tools))
            for s in servers_for(services, mode)
        ]

    can_admin = _can_admin(request)
    dev_servers, admin_servers = await asyncio.to_thread(
        lambda: (servers("developer", True), servers("admin", can_admin))
    )
    return AssistantInfo(
        developer=AssistantModeInfo(
            enabled=ai.assistants.developer.enabled, allowed=True, mcp_servers=dev_servers
        ),
        admin=AssistantModeInfo(
            enabled=ai.assistants.admin.enabled, allowed=can_admin, mcp_servers=admin_servers
        ),
        troubleshoot=ai.troubleshoot.enabled and has_permission(request, P.AIRFLOW_READ),
        compaction_trigger_chars=ai.assistants.compaction_trigger_chars,
        max_follow_ups=ai.assistants.max_follow_ups,
    )


@router.get("/assistant/threads", operation_id="listAssistantThreads")
async def list_threads(
    request: Request,
    services: ServicesDep,
    mode: Annotated[Literal["developer", "admin"] | None, Query()] = None,
) -> list[ThreadSummary]:
    user = _user(request, services)
    store = thread_store_for(services)
    items: list[ThreadSummary] = await asyncio.to_thread(store.list, user, mode)
    return items


def _thread_or_404(services: Any, user: str, thread_id: str) -> ChatThread:
    try:
        return thread_store_for(services).get(user, thread_id)
    except ThreadNotFound as exc:
        raise HTTPException(status_code=404, detail="no such thread") from exc


@router.get("/assistant/threads/{thread_id}", operation_id="getAssistantThread")
async def get_thread(thread_id: str, request: Request, services: ServicesDep) -> ChatThread:
    user = _user(request, services)
    thread: ChatThread = await asyncio.to_thread(_thread_or_404, services, user, thread_id)
    views = [a for m in thread.messages for a in m.approvals]
    await asyncio.to_thread(refresh_views, approval_store_for(services), user, views)
    return thread


@router.patch("/assistant/threads/{thread_id}", operation_id="renameAssistantThread")
async def rename_thread(
    thread_id: str, body: ThreadRename, request: Request, services: ServicesDep
) -> ThreadSummary:
    user = _user(request, services)
    await asyncio.to_thread(_thread_or_404, services, user, thread_id)
    thread = await asyncio.to_thread(thread_store_for(services).rename, user, thread_id, body.title)
    return thread.as_summary()


@router.delete("/assistant/threads/{thread_id}", operation_id="deleteAssistantThread")
async def delete_thread(thread_id: str, request: Request, services: ServicesDep) -> ThreadSummary:
    user = _user(request, services)
    thread = await asyncio.to_thread(_thread_or_404, services, user, thread_id)
    await asyncio.to_thread(thread_store_for(services).delete, user, thread_id)
    return thread.as_summary()


@router.post("/assistant/chat/stream", operation_id="assistantChatStream")
async def assistant_chat_stream(
    body: ChatTurnRequest, request: Request, services: ServicesDep
) -> StreamingResponse:
    """One assistant turn as a stream (tokens, tool calls/results, usage, then the
    ``assistant_turn`` result: the stored user + assistant messages)."""
    ai = _ai(services)
    profile = getattr(ai.assistants, body.mode)
    if not profile.enabled:
        raise HTTPException(status_code=404, detail=f"the {body.mode} assistant is disabled")
    if body.mode == "admin" and not _can_admin(request):
        raise HTTPException(
            status_code=403, detail="the admin assistant needs ai:admin or admin:panel"
        )
    user = _user(request, services)
    if body.thread_id:
        thread = await asyncio.to_thread(_thread_or_404, services, user, body.thread_id)
        if thread.mode != body.mode:
            raise HTTPException(status_code=409, detail="the thread belongs to another assistant")
        if body.regenerate and not any(m.role == "user" for m in thread.messages):
            raise HTTPException(status_code=422, detail="nothing to regenerate")
        if any(a.status == "resuming" for m in thread.messages for a in m.approvals):
            raise HTTPException(
                status_code=409, detail="a tool approval of this conversation is being resumed"
            )
    elif body.regenerate:
        raise HTTPException(status_code=422, detail="regenerate needs a thread_id")
    if not body.regenerate and not body.message.strip():
        raise HTTPException(status_code=422, detail="the message is empty")
    if body.mcp and not profile.allow_external_mcp:
        raise HTTPException(status_code=403, detail="external MCP servers are not allowed")
    role = request_role(request)
    # Airflow tools (runs, logs, connections…) only while features.airflow is on.
    airflow_on = bool(services.settings.feature("airflow"))
    factory = (
        _gateway_factory(request, services) if body.mode == "developer" and airflow_on else None
    )

    async def job(call: AiCall, emit: Emit) -> ChatTurnResult:
        state = TurnState()
        ctx = ChatContext(
            services=services,
            user=user,
            role=role,
            can=lambda p: has_permission(request, p),
            llm=call.llm,
            gateway_factory=factory,
            team=getattr(services.settings, "team", None),
        )
        call.detail = {"assistant": body.mode, "thread_id": body.thread_id}
        try:
            result = await run_turn(ctx, body, emit, state=state)
        finally:
            call.used_tools = list(state.sources.used_tools)
            call.sources = state.sources.refs()
        call.detail.update(
            thread_id=result.thread.id,
            regenerate=body.regenerate,
            proposals=[p.id for p in result.message.proposals],
            **_pending_detail(result.message.approvals),
        )
        return result

    response: StreamingResponse = await _streamed(
        request,
        services,
        f"assistant:{body.mode}",
        body.model,
        job,
        "assistant_turn",
        inputs=[body.message],
    )
    return response


@router.post(
    "/assistant/threads/{thread_id}/proposals/{proposal_id}/decision",
    operation_id="decideAssistantProposal",
)
async def decide_proposal(
    thread_id: str,
    proposal_id: str,
    body: ProposalDecision,
    request: Request,
    services: ServicesDep,
) -> Proposal:
    """Record that the user approved (applied in the editor), rejected or deployed a
    proposal. Approving a spec change needs ``spec:write``."""
    user = _user(request, services)
    store = thread_store_for(services)
    thread = await asyncio.to_thread(_thread_or_404, services, user, thread_id)
    proposal = thread.proposal(proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail="no such proposal")
    if (
        proposal.kind == "spec_patch"
        and body.decision != "reject"
        and not has_permission(request, P.SPEC_WRITE)
    ):
        raise HTTPException(status_code=403, detail="applying a change needs spec:write")
    status: dict[str, ProposalStatus] = {
        "approve": "approved",
        "reject": "rejected",
        "applied": "applied",
    }
    proposal.status = status[body.decision]
    proposal.decided_by = user
    proposal.decided_at = now_iso()
    await asyncio.to_thread(store.save, thread)
    dag_id = proposal.base_spec.dag_id if proposal.base_spec else None
    if dag_id is None and proposal.proposed_spec is not None:
        dag_id = proposal.proposed_spec.dag_id
    await asyncio.to_thread(
        _audit_decision,
        request,
        services,
        f"assistant:proposal_{body.decision}",
        model=next(
            (m.model for m in thread.messages if any(p.id == proposal_id for p in m.proposals)),
            None,
        ),
        detail={
            "dag_id": dag_id,
            "thread_id": thread_id,
            "proposal_id": proposal_id,
            "kind": proposal.kind,
            "title": proposal.title,
            "comment": body.comment,
            **({"outcome": body.detail} if body.detail else {}),
        },
    )
    return proposal


# --------------------------------------------------------------------------- tool approvals


def _pending_detail(approvals: list[ToolApprovalView]) -> dict[str, Any]:
    """Audit fields of a run that stopped for approval."""
    current = approvals[-1] if approvals else None
    if current is None or current.status != "pending":
        return {}
    return {"approval_pending": current.id, "approval_tools": [c.tool for c in current.calls]}


def _approval_or_404(services: Any, user: str, approval_id: str) -> ToolApproval:
    try:
        return approval_store_for(services).get(user, approval_id)
    except ApprovalNotFound as exc:
        raise HTTPException(status_code=404, detail="no such tool approval") from exc


@router.get("/approvals", operation_id="listToolApprovals")
async def list_tool_approvals(request: Request, services: ServicesDep) -> list[ToolApprovalView]:
    """The caller's tool approvals waiting for a decision (or being resumed)."""
    user = _user(request, services)
    items: list[ToolApprovalView] = await asyncio.to_thread(
        approval_store_for(services).pending, user
    )
    return items


@router.get("/approvals/{approval_id}", operation_id="getToolApproval")
async def get_tool_approval(
    approval_id: str, request: Request, services: ServicesDep
) -> ToolApprovalView:
    user = _user(request, services)
    approval = await asyncio.to_thread(_approval_or_404, services, user, approval_id)
    return approval.view()


def _ensure_agent(request: Request, services: Any, approval: ToolApproval) -> None:
    """The caller may still use the agent of ``approval`` (checked again on resume)."""
    ai = _ai(services)
    if approval.agent == "troubleshoot":
        _require_troubleshoot(request, services)
        return
    if not getattr(ai.assistants, approval.agent).enabled:
        raise HTTPException(status_code=404, detail=f"the {approval.agent} assistant is disabled")
    if approval.agent == "admin" and not _can_admin(request):
        raise HTTPException(
            status_code=403, detail="the admin assistant needs ai:admin or admin:panel"
        )


def _audit_tool_decisions(
    request: Request, services: Any, approval: ToolApproval, feature: str
) -> None:
    calls = {c.id: c for c in approval.calls}
    for d in approval.decisions:
        call = calls.get(d.id)
        _audit_decision(
            request,
            services,
            f"{feature}:tool_{'approved' if d.approved else 'denied'}",
            model=approval.model,
            tools=[call.tool] if call else [],
            detail={
                "dag_id": (approval.context.get("request") or {}).get("dag_id"),
                "approval_id": approval.id,
                "agent": approval.agent,
                "tool": call.tool if call else None,
                "tool_call_id": d.id,
                # free text typed by the user: never a registered secret in the audit trail
                "reason": mask(scrub_secrets(d.reason)) if d.reason else d.reason,
                **approval.ref,
            },
        )


def _mark_failed(
    services: Any, ctx: ChatContext | None, user: str, approval_id: str, error: str
) -> None:
    """Record that a resumed run failed (on the approval and on its message / analysis)."""
    store = approval_store_for(services)
    try:
        approval = store.finish(user, approval_id, "failed", error=error)
    except (ApprovalNotFound, DocumentConflict):
        log.warning("recording the failed tool approval %s failed", approval_id, exc_info=True)
        return
    if ctx is not None:
        mark_approval(ctx, approval)
        return
    analysis_id = approval.ref.get("analysis_id")
    if not analysis_id:
        return

    def record(analysis: TroubleshootAnalysis) -> None:
        for i, view in enumerate(analysis.approvals):
            if view.id == approval.id:
                analysis.approvals[i] = approval.view()

    try:
        analysis_store_for(services).mutate(user, analysis_id, record)
    except (LookupError, DocumentConflict):
        log.warning("marking analysis %s failed", analysis_id, exc_info=True)


@router.post("/approvals/{approval_id}/stream", operation_id="resumeToolApprovalStream")
async def resume_tool_approval(
    approval_id: str, body: ToolApprovalDecision, request: Request, services: ServicesDep
) -> StreamingResponse:
    """Approve or deny each pending tool call of a paused run and resume it as a stream.

    The approval is claimed first (compare-and-set): a second decision - also on another
    replica - gets 409, an expired approval 410. The stream ends with the result of the
    paused run's kind: ``assistant_turn`` (the completed chat message) or ``troubleshoot``
    (the analysis). Every decision is written to the AI audit log."""
    user = _user(request, services)
    ai = _ai(services)
    store = approval_store_for(services)
    approval = await asyncio.to_thread(_approval_or_404, services, user, approval_id)
    _ensure_agent(request, services, approval)
    limit = ai.approvals.max_reason_chars
    if any(d.reason and len(d.reason) > limit for d in body.decisions):
        raise HTTPException(status_code=422, detail=f"a reason may have at most {limit} characters")
    claimer = f"{replica_id()}:{new_claim_id()}"
    feature = "troubleshoot" if approval.agent == "troubleshoot" else "assistant"
    ctx_box: dict[str, ChatContext] = {}
    try:
        approval = await asyncio.to_thread(
            store.claim, user, approval_id, list(body.decisions), by=claimer
        )
    except ApprovalExpired as exc:
        expired = await asyncio.to_thread(_approval_or_404, services, user, approval_id)
        await asyncio.to_thread(
            _audit_decision,
            request,
            services,
            f"{feature}:tool_approval_expired",
            model=expired.model,
            detail={"approval_id": approval_id, "agent": expired.agent, **expired.ref},
        )
        raise HTTPException(status_code=410, detail=str(exc)) from exc
    except ApprovalBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await asyncio.to_thread(_audit_tool_decisions, request, services, approval, feature)

    async def resume_job(call: AiCall, emit: Emit) -> Any:
        call.detail = {"approval_id": approval.id, **approval.ref}
        try:
            if approval.agent == "troubleshoot":
                result: Any = await _resume_troubleshoot(
                    request, services, call, emit, approval, user
                )
            else:
                result = await _resume_chat(request, services, call, emit, approval, user, ctx_box)
        except BaseException as exc:
            error = "cancelled" if isinstance(exc, asyncio.CancelledError) else str(exc)[:500]
            # sync: a cancelled task cannot await any more
            _mark_failed(services, ctx_box.get("ctx"), user, approval.id, error)
            raise
        await asyncio.to_thread(store.finish, user, approval.id, "resolved")
        return result

    kind = "troubleshoot" if approval.agent == "troubleshoot" else "assistant_turn"
    model = (approval.context.get("request") or {}).get("model")
    try:
        response: StreamingResponse = await _streamed(
            request,
            services,
            f"{feature}:{approval.agent}" if feature == "assistant" else feature,
            model,
            resume_job,
            kind,
            inputs=decisions_text(list(body.decisions)),
        )
    except BaseException:
        # Refused before the stream started (no model, guardrail): nothing ran, so the
        # approval can be decided again.
        await asyncio.to_thread(store.release, user, approval.id, claimer)
        raise
    return response


async def _resume_chat(
    request: Request,
    services: Any,
    call: AiCall,
    emit: Emit,
    approval: ToolApproval,
    user: str,
    ctx_box: dict[str, ChatContext],
) -> ChatTurnResult:
    mode = approval.agent
    airflow_on = bool(services.settings.feature("airflow"))
    state = TurnState()
    ctx = ChatContext(
        services=services,
        user=user,
        role=request_role(request),
        can=lambda p: has_permission(request, p),
        llm=call.llm,
        gateway_factory=(
            _gateway_factory(request, services) if mode == "developer" and airflow_on else None
        ),
        team=getattr(services.settings, "team", None),
    )
    ctx_box["ctx"] = ctx
    try:
        result = await resume_turn(ctx, approval, emit, state=state)
    except ThreadNotFound as exc:
        raise HTTPException(status_code=404, detail="the conversation no longer exists") from exc
    finally:
        call.used_tools = list(state.sources.used_tools)
        call.sources = state.sources.refs()
    call.detail.update(
        assistant=mode,
        thread_id=result.thread.id,
        resumed=True,
        **_pending_detail(result.message.approvals),
    )
    return result


async def _resume_troubleshoot(
    request: Request,
    services: Any,
    call: AiCall,
    emit: Emit,
    approval: ToolApproval,
    user: str,
) -> TroubleshootAnalysis:
    ts = _ai(services).troubleshoot
    try:
        body = TroubleshootRequest.model_validate(approval.context.get("request") or {})
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail="the paused analysis cannot be restored"
        ) from exc
    # Never more permissive than when the analysis started, nor than the caller may now.
    stored = approval.context.get("actions_mode")
    stored_mode: ActionsMode = stored if stored in ("read_only", "hitl", "apply") else "read_only"
    actions_mode = effective_mode(
        ts.airflow_actions, stored_mode, allowed=_may_run_actions(request)
    )
    return await _troubleshoot_run(
        request,
        services,
        call,
        emit,
        body=body,
        user=user,
        actions_mode=actions_mode,
        factory=_gateway_factory(request, services),
        resume=approval,
    )


# --------------------------------------------------------------------------- troubleshoot


def _require_troubleshoot(request: Request, services: Any) -> None:
    if not _ai(services).troubleshoot.enabled:
        raise HTTPException(status_code=404, detail="AI troubleshooting is disabled")
    ensure_feature(request, "airflow")  # analyses runs, logs and config read from Airflow
    if not has_permission(request, P.AIRFLOW_READ):
        raise HTTPException(status_code=403, detail="troubleshooting needs airflow:read")


def _may_run_actions(request: Request) -> bool:
    """The caller may execute Airflow actions at all (run:manage, features.runs)."""
    return has_permission(request, ACTION_PERMISSION) and feature_enabled(request, ACTION_FEATURE)


def _action_feature(request: Request) -> Any:
    return lambda name: feature_enabled(request, name)


def _action_permission(request: Request) -> Any:
    return lambda permission: has_permission(request, permission)


def _fs_roots(services: Any) -> list[str]:
    from autoair.ai.assistants.fs_sandbox import sandbox_for

    sandbox = sandbox_for(services, _ai(services).troubleshoot.filesystem)
    return [str(r) for r in sandbox.roots]


@router.get("/troubleshoot/options", operation_id="troubleshootOptions")
async def troubleshoot_options(request: Request, services: ServicesDep) -> TroubleshootOptions:
    ai = _ai(services)
    ts = ai.troubleshoot
    roots = await asyncio.to_thread(_fs_roots, services)
    everything = TroubleshootScope(
        connections=True,
        variables=True,
        pools=True,
        xcom_keys=True,
        xcom_values=True,
        other_runs=True,
        import_errors=True,
        airflow_config=True,
        filesystem=True,
        research=True,
        online=True,
    )
    allowed = effective_scope(everything, ts, online=ai.research.online, fs=bool(roots))
    defaults = effective_scope(TroubleshootScope(), ts, online=ai.research.online, fs=bool(roots))
    servers: list[McpServerOption] = []
    if ts.allow_external_mcp:
        found = await asyncio.to_thread(servers_for, services, "troubleshoot")
        servers = [
            McpServerOption(name=s.name, description=s.description, tools=list(s.tools))
            for s in found
        ]
    return TroubleshootOptions(
        enabled=ts.enabled
        and has_permission(request, P.AIRFLOW_READ)
        and bool(services.settings.feature("airflow")),
        allowed=allowed,
        defaults=defaults,
        filesystem_roots=roots,
        online_available=ai.research.online,
        mcp_servers=servers,
        actions_mode=effective_mode(ts.airflow_actions, allowed=_may_run_actions(request)),
        allowed_actions=list(ts.allowed_actions),
        max_actions=ts.max_actions,
    )


@router.get("/mcp/servers/{name}/tools", operation_id="aiMcpServerTools")
async def mcp_server_tools(
    name: str,
    request: Request,
    services: ServicesDep,
    agent: Annotated[Literal["troubleshoot", "developer", "admin"], Query()] = "troubleshoot",
) -> list[McpToolInfo]:
    """Connect to an allowed external MCP server and list its tools (allow-list applied).
    The caller must be allowed to use ``agent`` (troubleshooting: airflow:read; admin
    assistant: ai:admin or admin:panel)."""
    if agent == "troubleshoot":
        _require_troubleshoot(request, services)
    elif agent == "admin" and not _can_admin(request):
        raise HTTPException(
            status_code=403, detail="the admin assistant needs ai:admin or admin:panel"
        )
    try:
        server = await asyncio.to_thread(server_by_name, services, name, agent)
        tools = await list_server_tools(services, server)
    except McpError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return [t for t in tools if t.allowed]


def _saved_spec(services: Any, dag_id: str) -> Any:
    from autoair.mcp.store import store_for

    try:
        return store_for(services).get(dag_id)
    except Exception:
        return None


def _troubleshoot_scrub(services: Any) -> Any:
    from autoair.ai.rules import scrub_secrets

    pipeline = guardrails_for(services)

    def scrub(text: str) -> str:
        return pipeline.sanitize(scrub_secrets(text))

    return scrub


async def _troubleshoot_run(
    request: Request,
    services: Any,
    call: AiCall,
    emit: Emit,
    *,
    body: TroubleshootRequest,
    user: str,
    actions_mode: ActionsMode,
    factory: Any,
    resume: ToolApproval | None = None,
) -> TroubleshootAnalysis:
    """One analysis run: collect the evidence (or restore it from a paused run), run the
    agent and build, store and audit the analysis. A run that stops for tool approval is
    stored as an ``awaiting_approval`` analysis with its pending approval."""
    from autoair.ai import prompts
    from autoair.ai.assistants.fs_sandbox import sandbox_for

    ai = _ai(services)
    ts = ai.troubleshoot
    sources = SourceCollector()
    pipeline = guardrails_for(services)
    scrub = _troubleshoot_scrub(services)
    previous: TroubleshootAnalysis | None = None
    if resume is None:
        sandbox = sandbox_for(services, ts.filesystem)
        scope = effective_scope(body.scope, ts, online=ai.research.online, fs=sandbox.enabled)
        spec = body.spec or await asyncio.to_thread(_saved_spec, services, body.dag_id)
    else:
        ctx = resume.context
        scope = TroubleshootScope.model_validate(ctx.get("scope") or {})
        spec = DagSpec.model_validate(ctx["spec"]) if ctx.get("spec") else None
        sources.restore(
            [Citation.model_validate(c) for c in ctx.get("citations", [])],
            ctx.get("used_tools", []),
        )
        previous = await asyncio.to_thread(
            _paused_analysis, services, user, resume.ref.get("analysis_id", "")
        )
    call.detail = {
        "dag_id": body.dag_id,
        "run_id": body.run_id,
        "task_id": body.task_id,
        "scope": scope.model_dump(),
        "mcp": [m.server for m in body.mcp],
        "rerun_of": body.rerun_of,
        "actions_mode": actions_mode,
        **({"approval_id": resume.id} if resume is not None else {}),
    }
    if resume is None:
        emit(StatusEvent(stage="collect", message="reading the run, task instances and logs"))
        try:
            async with factory() as gw:
                pack = await collect_evidence(
                    gw,
                    body,
                    spec,
                    settings=ts,
                    scope=scope,
                    sources=sources,
                    scrub=scrub,
                    registry=services.registry,
                )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except AirflowError as exc:
            raise HTTPException(
                status_code=502, detail=f"cannot read the run from Airflow: {exc.detail}"
            ) from exc
    else:
        pack = EvidencePack.model_validate(resume.context.get("pack") or {"text": ""})
    tools = troubleshoot_tools(
        services,
        body,
        scope=scope,
        gateway_factory=factory,
        sources=sources,
        scrub=scrub,
        spec=spec,
        code=pack.code,
    )
    allow = ai.guardrails.tool_allowlist
    role = request_role(request)
    shared = filter_tools([t for t in tools if t.name in _SHARED], role, allow)
    tools = [*(t for t in tools if t.name not in _SHARED), *shared]
    selections = list(body.mcp) if ts.allow_external_mcp else []
    async with attach_mcp_tools(services, selections, "troubleshoot") as (mcp, notes):
        notes = list(notes)
        # hitl: the user approves each Airflow action, so each write tool call too.
        offered, gated, approval_notes = gated_tools(
            [*tools, *mcp], ai.approvals, "troubleshoot", force_writes=actions_mode == "hitl"
        )
        notes += approval_notes
        wrapped = sources.wrap_all(offered)
        call.tools = [t.name for t in wrapped]
        for note in notes:
            emit(StatusEvent(stage="mcp", message=note))
        with prompts.prompt_context(
            user=user, team=getattr(services.settings, "team", None), assistant="troubleshoot"
        ):
            system = prompts.get("TROUBLESHOOT_SYSTEM")
        question = body.question or (
            "Find the root cause of this run's failure"
            + (f" in task {body.task_id}" if body.task_id else "")
            + " and propose the smallest safe fix."
        )
        emit(StatusEvent(stage="analyze", message="analysing"))
        history: list[Any] = []
        if resume is not None:
            from pydantic_ai.messages import ModelMessagesTypeAdapter

            history = list(ModelMessagesTypeAdapter.validate_python(resume.messages))
        task = AgentTask(
            llm=call.llm,
            task=question if resume is None else "",
            tools=wrapped,
            context=pipeline.untrusted(pack.text, "evidence pack") if resume is None else "",
            system=system,
            budget=Budget(
                max_steps=max(1, ts.max_steps - (resume.requests if resume else 0)),
                max_tokens=ai.agent.token_budget,
                max_cost_usd=ai.agent.cost_budget_usd,
                timeout_seconds=ts.timeout_seconds,
            ),
            max_tokens=ai.agent.max_tokens_per_call,
            tool_timeout=ai.agent.tool_timeout_seconds,
            max_tool_chars=ai.agent.max_tool_output_chars,
            catalog=services.catalog,
            registry=services.registry,
            policy=policy_of(services),
            node_ids={n.id for n in spec.nodes} if spec else set(),
            guardrails=pipeline,
            emit=emit,
            history=history,
            final_model=TroubleshootFinal,
            final_turn_grace_seconds=ai.agent.final_turn_grace_seconds,
            approval_required=gated,
            approval_argument_chars=ai.approvals.max_argument_chars,
        )
        if resume is not None:
            task.resume = ResumeState(
                calls=list(resume.calls),
                decisions=list(resume.decisions),
                decided_by=resume.decided_by or user,
            )
            task.step_offset = resume.steps
        try:
            result = await arun_agent(task)
        finally:
            call.used_tools = list(sources.used_tools)
            call.sources = sources.refs()
    if previous is not None:
        result.steps = [*previous.steps, *result.steps]
        result.usage = previous.usage + result.usage
    analysis = await asyncio.to_thread(
        build_analysis,
        services,
        body,
        user=user,
        scope=scope,
        pack=pack,
        spec=spec,
        result=result,
        sources=sources,
        notes=notes,
        actions_mode=actions_mode,
        can=_action_permission(request),
        feature=_action_feature(request),
    )
    if previous is not None:
        analysis.id = previous.id
        analysis.created_at = previous.created_at
        done = [resume.view().model_copy(update={"status": "resolved"})] if resume else []
        analysis.approvals = [a for a in previous.approvals if resume and a.id != resume.id]
        analysis.approvals += done
    if result.stopped == "approval":
        history_out = [*(resume.messages if resume else []), *dump_history(result.messages)]
        approval = new_approval(
            user=user,
            agent="troubleshoot",
            calls=result.pending,
            settings=ai.approvals,
            ref={"analysis_id": analysis.id},
            context={
                "request": body.model_dump(mode="json"),
                "actions_mode": actions_mode,
                "scope": scope.model_dump(mode="json"),
                "spec": spec.model_dump(mode="json", by_alias=True) if spec else None,
                "pack": pack.model_dump(mode="json"),
                "citations": [c.model_dump(mode="json") for c in sources.citations],
                "used_tools": list(sources.used_tools),
            },
            messages=history_out,
            requests=(resume.requests if resume else 0) + result.requests,
            steps=len(result.steps),
            model=result.model,
        )
        await asyncio.to_thread(approval_store_for(services).create, approval)
        analysis.approvals.append(approval.view())
        analysis.status = "awaiting_approval"
        analysis.airflow_actions = []
        call.detail.update(
            approval_pending=approval.id, approval_tools=[c.tool for c in approval.calls]
        )
        await asyncio.to_thread(analysis_store_for(services).save, analysis)
        call.detail.update(analysis_id=analysis.id, status=analysis.status)
        return analysis
    if analysis.airflow_actions:
        await asyncio.to_thread(
            _audit_decision,
            request,
            services,
            "troubleshoot:actions_proposed",
            model=analysis.model,
            detail=_decision_detail(
                analysis,
                mode=actions_mode,
                actions=[audit_view(a) for a in analysis.airflow_actions],
            ),
        )
    if actions_mode == "apply":
        await _apply_actions(request, services, analysis, factory, user, emit)
    await asyncio.to_thread(analysis_store_for(services).save, analysis)
    call.detail.update(
        analysis_id=analysis.id,
        status=analysis.status,
        hypotheses=len(analysis.hypotheses),
        findings=[f.id for f in analysis.findings],
        actions={a.id: a.status for a in analysis.airflow_actions},
    )
    return analysis


def _paused_analysis(services: Any, user: str, analysis_id: str) -> TroubleshootAnalysis:
    try:
        return analysis_store_for(services).get(user, analysis_id)
    except LookupError as exc:
        raise ValueError("the paused analysis no longer exists") from exc


@router.post("/troubleshoot/stream", operation_id="troubleshootStream")
async def troubleshoot_stream(
    body: TroubleshootRequest, request: Request, services: ServicesDep
) -> StreamingResponse:
    """Analyse a DAG run as a stream (evidence collection, tool calls, tokens), ending with
    the stored ``troubleshoot`` analysis. Read-only: proposals need an explicit decision;
    tool calls that need approval (``ai.approvals``) pause it (``awaiting_approval``)."""
    _require_troubleshoot(request, services)
    user = _user(request, services)
    ts = _ai(services).troubleshoot
    factory = _gateway_factory(request, services)
    if body.mcp and not ts.allow_external_mcp:
        raise HTTPException(status_code=403, detail="external MCP servers are not allowed")
    actions_mode = effective_mode(
        ts.airflow_actions, body.airflow_actions, allowed=_may_run_actions(request)
    )

    async def job(call: AiCall, emit: Emit) -> TroubleshootAnalysis:
        return await _troubleshoot_run(
            request,
            services,
            call,
            emit,
            body=body,
            user=user,
            actions_mode=actions_mode,
            factory=factory,
        )

    response: StreamingResponse = await _streamed(
        request, services, "troubleshoot", body.model, job, "troubleshoot", inputs=[body.question]
    )
    return response


@router.get("/troubleshoot/analyses", operation_id="listTroubleshootAnalyses")
async def list_analyses(
    request: Request,
    services: ServicesDep,
    dag_id: Annotated[str | None, Query(max_length=250)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 30,
) -> list[AnalysisSummary]:
    _require_troubleshoot(request, services)
    user = _user(request, services)
    items: list[AnalysisSummary] = await asyncio.to_thread(
        analysis_store_for(services).list, user, dag_id, limit
    )
    return items


def _analysis_or_404(services: Any, user: str, analysis_id: str) -> TroubleshootAnalysis:
    try:
        return analysis_store_for(services).get(user, analysis_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="no such analysis") from exc


@router.get("/troubleshoot/analyses/{analysis_id}", operation_id="getTroubleshootAnalysis")
async def get_analysis(
    analysis_id: str, request: Request, services: ServicesDep
) -> TroubleshootAnalysis:
    _require_troubleshoot(request, services)
    user = _user(request, services)
    analysis: TroubleshootAnalysis = await asyncio.to_thread(
        _analysis_or_404, services, user, analysis_id
    )
    await asyncio.to_thread(refresh_views, approval_store_for(services), user, analysis.approvals)
    return analysis


def _decision_detail(analysis: TroubleshootAnalysis, **extra: Any) -> dict[str, Any]:
    return {
        "dag_id": analysis.dag_id,
        "run_id": analysis.run_id,
        "task_id": analysis.task_id,
        "analysis_id": analysis.id,
        **extra,
    }


def _record_decision(
    store: AnalysisStore, user: str, changed: TroubleshootAnalysis
) -> TroubleshootAnalysis:
    """Write the status and the newest decision of ``changed`` onto the stored analysis
    (compare-and-set): action results recorded meanwhile by another replica are kept."""
    decision = changed.decisions[-1]

    def record(fresh: TroubleshootAnalysis) -> None:
        fresh.status = changed.status
        fresh.decisions.append(decision)

    return store.mutate(user, changed.id, record)


@router.post(
    "/troubleshoot/analyses/{analysis_id}/decision", operation_id="decideTroubleshootAnalysis"
)
async def decide_analysis(
    analysis_id: str, body: AnalysisDecision, request: Request, services: ServicesDep
) -> DecisionResult:
    """Approve (returns the patched, validated spec and its generated code for the normal
    deploy pipeline; needs ``spec:write``) or reject the analysis' proposed change."""
    _require_troubleshoot(request, services)
    user = _user(request, services)
    store = analysis_store_for(services)
    analysis = await asyncio.to_thread(_analysis_or_404, services, user, analysis_id)
    applied: AppliedPatches | None = None
    code_after: str | None = None
    if body.decision == "approve":
        if not has_permission(request, P.SPEC_WRITE):
            raise HTTPException(status_code=403, detail="applying a change needs spec:write")
        if analysis.base_spec is not None and any(f.patch for f in analysis.findings):
            applied = await asyncio.to_thread(
                apply_findings,
                analysis.base_spec,
                analysis.findings,
                catalog=services.catalog,
                registry=services.registry,
                policy=policy_of(services),
                only=body.finding_ids,
            )
            if applied.spec is not None:
                from autoair.codegen import render

                try:
                    code_after = await asyncio.to_thread(render, applied.spec, services.registry)
                except Exception:
                    code_after = None
        analysis.status = "approved"
    else:
        analysis.status = "rejected"
    analysis.decisions.append(
        Decision(
            user=user,
            decision=body.decision,
            comment=body.comment,
            finding_ids=list(body.finding_ids or [f.id for f in analysis.findings if f.patch]),
        )
    )
    analysis = await asyncio.to_thread(_record_decision, store, user, analysis)
    await asyncio.to_thread(
        _audit_decision,
        request,
        services,
        f"troubleshoot:{body.decision}",
        model=analysis.model,
        tools=analysis.tools_used,
        sources=[f"{c.kind}:{c.ref}" for c in analysis.sources],
        detail=_decision_detail(
            analysis,
            findings=analysis.decisions[-1].finding_ids,
            comment=body.comment,
            applied=applied.applied if applied else [],
            valid=applied.ok if applied else None,
        ),
    )
    return DecisionResult(analysis=analysis, applied=applied, code_after=code_after)


@router.post(
    "/troubleshoot/analyses/{analysis_id}/applied", operation_id="troubleshootAnalysisApplied"
)
async def analysis_applied(
    analysis_id: str, body: AnalysisApplied, request: Request, services: ServicesDep
) -> TroubleshootAnalysis:
    """Record that the approved change went through the deploy pipeline."""
    _require_troubleshoot(request, services)
    user = _user(request, services)
    store = analysis_store_for(services)
    analysis = await asyncio.to_thread(_analysis_or_404, services, user, analysis_id)
    if analysis.status not in ("approved", "applied"):
        raise HTTPException(status_code=409, detail="approve the analysis first")
    analysis.status = "applied" if body.deployed else analysis.status
    analysis.decisions.append(Decision(user=user, decision="applied", detail=body.detail))
    analysis = await asyncio.to_thread(_record_decision, store, user, analysis)
    await asyncio.to_thread(
        _audit_decision,
        request,
        services,
        "troubleshoot:applied",
        model=analysis.model,
        tools=analysis.tools_used,
        detail=_decision_detail(analysis, deployed=body.deployed, outcome=body.detail),
    )
    return analysis


async def _execute_one(
    request: Request,
    services: Any,
    analysis: TroubleshootAnalysis,
    action: AirflowAction,
    factory: Any,
    user: str,
    *,
    automatic: bool,
) -> tuple[ActionResult, BlockReason | None]:
    """Re-vet ``action`` against the current settings and the caller and run it with the
    caller's Airflow token; the result is not recorded yet."""
    ts = _ai(services).troubleshoot
    reason = block_reason(
        action,
        dag_id=analysis.dag_id,
        run_id=analysis.run_id,
        settings=ts,
        can=_action_permission(request),
        feature=_action_feature(request),
    )
    if reason is not None:
        result = blocked_result(reason)
    else:
        async with factory() as gw:
            result = await execute_action(gw, action, ts)
    result.by = user
    result.automatic = automatic
    return result, reason


async def _audit_executed(
    request: Request,
    services: Any,
    analysis: TroubleshootAnalysis,
    action: AirflowAction,
    result: ActionResult,
    *,
    automatic: bool,
) -> None:
    await asyncio.to_thread(
        _audit_decision,
        request,
        services,
        "troubleshoot:action_executed",
        model=analysis.model,
        detail=_decision_detail(
            analysis,
            airflow_action=audit_view(action),
            result=result.status,
            message=result.message,
            automatic=automatic,
        ),
    )


async def _run_action(
    request: Request,
    services: Any,
    analysis: TroubleshootAnalysis,
    action: AirflowAction,
    factory: Any,
    user: str,
    *,
    automatic: bool,
) -> ActionResult:
    """apply mode: run ``action`` of an analysis that is not stored yet (no other replica
    can see it), record the result on it and audit it."""
    result, reason = await _execute_one(
        request, services, analysis, action, factory, user, automatic=automatic
    )
    record_result(action, result)
    if reason is not None:
        action.blocked = reason
        action.status = "blocked"
    await _audit_executed(request, services, analysis, action, result, automatic=automatic)
    return result


async def _apply_actions(
    request: Request,
    services: Any,
    analysis: TroubleshootAnalysis,
    factory: Any,
    user: str,
    emit: Emit,
) -> None:
    """apply mode: run the vetted actions one after another, streaming each result."""
    for action in analysis.airflow_actions:
        if action.status != "proposed":
            continue
        label = f"{action.action} {action.task_id or action.run_id or ''}".strip()
        emit(StatusEvent(stage="action", message=f"{label}: running"))
        result = await _run_action(
            request, services, analysis, action, factory, user, automatic=True
        )
        emit(StatusEvent(stage="action", message=f"{label}: {result.status} - {result.message}"))


@router.post(
    "/troubleshoot/analyses/{analysis_id}/actions/{action_id}",
    operation_id="decideTroubleshootAction",
)
async def decide_action(
    analysis_id: str,
    action_id: str,
    body: ActionDecision,
    request: Request,
    services: ServicesDep,
) -> TroubleshootAnalysis:
    """Execute (hitl: the user's approval of this one action; needs ``run:manage`` and
    ``features.runs``) or reject one proposed Airflow action of an analysis. Executing an
    action that already succeeded or was skipped returns the analysis unchanged."""
    _require_troubleshoot(request, services)
    user = _user(request, services)
    store = analysis_store_for(services)
    analysis = await asyncio.to_thread(_analysis_or_404, services, user, analysis_id)
    action = next((a for a in analysis.airflow_actions if a.id == action_id), None)
    if action is None or not action.executable:
        raise HTTPException(status_code=404, detail="no such executable action")
    if body.decision == "reject":

        def reject(fresh: TroubleshootAnalysis) -> None:
            for a in fresh.airflow_actions:
                if a.id == action_id and a.status in ("proposed", "blocked", "failed"):
                    a.status = "rejected"

        if action.status in ("proposed", "blocked", "failed"):
            analysis = await asyncio.to_thread(store.mutate, user, analysis_id, reject)
            action = next(a for a in analysis.airflow_actions if a.id == action_id)
        await asyncio.to_thread(
            _audit_decision,
            request,
            services,
            "troubleshoot:action_rejected",
            model=analysis.model,
            detail=_decision_detail(
                analysis, airflow_action=audit_view(action), comment=body.comment
            ),
        )
        return analysis
    ts = _ai(services).troubleshoot
    mode = effective_mode(
        ts.airflow_actions, analysis.actions_mode, allowed=_may_run_actions(request)
    )
    if mode == "read_only":
        if not has_permission(request, ACTION_PERMISSION):
            raise HTTPException(
                status_code=403, detail="executing Airflow actions needs run:manage"
            )
        ensure_feature(request, ACTION_FEATURE)
        raise HTTPException(
            status_code=409,
            detail="Airflow actions are read-only for this analysis "
            "(ai.troubleshoot.airflow_actions)",
        )
    if action.status in ("succeeded", "skipped"):
        return analysis
    if action.status == "rejected":
        raise HTTPException(status_code=409, detail="the action was rejected")
    reason = block_reason(
        action,
        dag_id=analysis.dag_id,
        run_id=analysis.run_id,
        settings=ts,
        can=_action_permission(request),
        feature=_action_feature(request),
    )
    if reason is not None:
        raise HTTPException(status_code=409, detail=f"the action cannot run: {reason}")
    await asyncio.to_thread(
        _audit_decision,
        request,
        services,
        "troubleshoot:action_approved",
        model=analysis.model,
        detail=_decision_detail(analysis, airflow_action=audit_view(action), comment=body.comment),
    )
    # Claim the action (status running, compare-and-set on the stored analysis) before
    # Airflow is called: a second replica approving it at the same moment is refused here.
    try:
        analysis = await asyncio.to_thread(
            lambda: store.claim_action(
                user, analysis_id, action_id, user, timeout=ts.action_claim_timeout_seconds
            )
        )
    except ActionBusy as exc:
        if exc.state == "done":
            return await asyncio.to_thread(_analysis_or_404, services, user, analysis_id)
        raise HTTPException(
            status_code=409,
            detail={"code": f"action_{exc.state}", "message": f"the action is {exc.state}"},
        ) from exc
    except DocumentConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    action = next(a for a in analysis.airflow_actions if a.id == action_id)
    factory = _gateway_factory(request, services)
    try:
        result, reason = await _execute_one(
            request, services, analysis, action, factory, user, automatic=False
        )
    except BaseException:
        # Release the claim (as failed) so the action can be retried before the timeout.
        crashed = ActionResult(
            status="failed", code="error", message="execution failed unexpectedly", by=user
        )
        await asyncio.to_thread(store.finish_action, user, analysis_id, action_id, crashed)
        raise
    analysis = await asyncio.to_thread(
        lambda: store.finish_action(user, analysis_id, action_id, result, blocked=reason)
    )
    action = next(a for a in analysis.airflow_actions if a.id == action_id)
    await _audit_executed(request, services, analysis, action, result, automatic=False)
    return analysis


# --------------------------------------------------------------------------- upgrade / deploy


@router.post("/upgrade", operation_id="aiUpgradeDags")
async def upgrade(body: UpgradeRequest, request: Request, services: ServicesDep) -> UpgradeReport:
    """Airflow 2 → 3 idioms of one spec, some saved DAGs or all of them (ruff AIR3 + rules;
    an LLM pass for a single DAG). Proposals come back as a reviewable diff."""
    ai = _ai(services)
    specs: list[Any] = []
    notes: list[str] = []
    if body.spec is not None:
        specs.append(body.spec)
    else:
        from autoair.mcp.store import store_for

        store = store_for(services)
        ids = body.dag_ids
        if body.all:
            ids = [d.dag_id for d in await asyncio.to_thread(store.list)]
        if len(ids) > ai.upgrade.max_dags:
            notes.append(f"only the first {ai.upgrade.max_dags} of {len(ids)} DAGs were analysed")
            ids = ids[: ai.upgrade.max_dags]
        for dag_id in ids:
            spec = await asyncio.to_thread(store.get, dag_id)
            if spec is None:
                notes.append(f"{dag_id}: no saved spec")
            else:
                specs.append(spec)
    if not specs:
        raise HTTPException(status_code=422, detail="send a spec, dag_ids or all=true")
    single = len(specs) == 1
    if body.use_ai and not single:
        notes.append("the AI pass runs for single DAGs only; these results are rules + ruff")
    async with ai_call(
        request,
        services,
        "upgrade",
        model=body.model,
        required=False,
        use_ai=body.use_ai and single,
    ) as call:
        reports = []
        for spec in specs:
            reports.append(
                await asyncio.to_thread(
                    upgrade_dag,
                    spec,
                    services,
                    llm=call.llm if single else None,
                    rules=ai.upgrade.ruff_rules,
                )
            )
        call.detail = {"dags": [s.dag_id for s in specs]}
        usage = call.meter.total if call.meter else None
    if not any(r.ruff_ran for r in reports):
        notes.append("ruff is not available: AIR3 rules were skipped")
    return UpgradeReport(
        dags=reports,
        model=str(call.llm.model) if call.llm is not None else None,
        usage=usage if call.llm is not None else None,
        notes=notes,
    )


@router.post("/deploy-message", operation_id="aiDeployMessage")
async def generate_deploy_message(
    body: DeployMessageRequest, request: Request, services: ServicesDep
) -> DeployMessage:
    """A commit subject and PR description summarising the spec diff of a deploy."""
    async with ai_call(
        request, services, "deploy_message", model=body.model, required=False, use_ai=body.use_ai
    ) as call:
        result = await deploy_message(services, body, call.llm)
        call.detail = {"dag_id": body.spec.dag_id, "ai_used": result.ai_used}
    return result


# --------------------------------------------------------------------------- project rules


@router.get("/rules", operation_id="listAiRules")
async def list_rules(services: ServicesDep) -> list[ProjectRules]:
    rules: list[ProjectRules] = await asyncio.to_thread(rules_store_for(services).all)
    return rules


@router.get("/rules/effective", operation_id="effectiveAiRules")
async def effective_rules_endpoint(services: ServicesDep) -> EffectiveRules:
    from autoair.ai.assistants.project_rules import effective_rules

    team = getattr(services.settings, "team", None)
    text = await asyncio.to_thread(effective_rules, services, team)
    return EffectiveRules(team=team, text=text)


@router.put("/rules/{scope}", operation_id="putAiRules", dependencies=[AdminDep])
async def put_rules(
    scope: str, body: RulesUpdate, request: Request, services: ServicesDep
) -> ProjectRules:
    try:
        rules = await asyncio.to_thread(
            rules_store_for(services).put, scope, body, request_user(request)
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await asyncio.to_thread(
        services.audit.record,
        "ai_rules_put",
        user=request_user(request),
        scope=scope,
        enabled=body.enabled,
        chars=len(rules.content),
    )
    return rules


@router.delete("/rules/{scope}", operation_id="deleteAiRules", dependencies=[AdminDep])
async def delete_rules(scope: str, request: Request, services: ServicesDep) -> EffectiveRules:
    try:
        removed = await asyncio.to_thread(rules_store_for(services).delete, scope)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not removed:
        raise HTTPException(status_code=404, detail="no rules for this scope")
    await asyncio.to_thread(
        services.audit.record, "ai_rules_delete", user=request_user(request), scope=scope
    )
    return EffectiveRules(team=None, text="")


__all__ = ["router"]
