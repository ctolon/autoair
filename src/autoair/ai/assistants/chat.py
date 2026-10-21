"""One chat turn of an assistant (developer / admin): persisted, compacted, streamed.

A turn: load (or create) the thread, store the user's message, compact the history when
it got long, build the assistant's tool set (allow-listed), run the bounded tool loop
with the thread history, then store the answer with its tool calls, numbered citations,
suggested follow-ups, proposals (reviewable diffs / config snippets), usage and guardrail
notes. Streaming and auditing are the router's job (``/api/ai/assistant/chat/stream``).
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import re
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field
from pydantic_ai.capabilities import ProcessHistory

from autoair.ai import prompts
from autoair.ai.agent import AgentResult, AgentTask, arun_agent
from autoair.ai.assistants.compaction import (
    compact,
    dump_history,
    history_messages,
    trim_history,
)
from autoair.ai.assistants.dev_tools import (
    ADMIN_MEMORY_TAG,
    TurnState,
    developer_tools,
)
from autoair.ai.assistants.mcp_client import (
    McpSelection,
    attach_mcp_tools,
    default_selections,
)
from autoair.ai.assistants.threads import (
    ChatMessage,
    ChatThread,
    Mode,
    ThreadNotFound,
    ThreadSummary,
    ToolCallRecord,
    thread_store_for,
    title_from,
)
from autoair.ai.config import ai_settings_of
from autoair.ai.events import Emit, StatusEvent
from autoair.ai.guardrails import Budget, filter_tools, pipeline_for
from autoair.ai.tools import AgentTool
from autoair.ai.usage import track_usage
from autoair.spec import DagSpec
from autoair.validation import policy_from_settings

PROMPTS: dict[str, str] = {
    "developer": "DEVELOPER_ASSISTANT_SYSTEM",
    "admin": "ADMIN_ASSISTANT_SYSTEM",
}
_FOLLOW_UPS = re.compile(r"<follow_ups>(.*?)(?:</follow_ups>|$)", re.DOTALL | re.IGNORECASE)
_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s*")


class ChatTurnRequest(BaseModel):
    thread_id: str | None = Field(default=None, max_length=40, description="Continue a thread.")
    mode: Mode = "developer"
    message: str = Field(default="", max_length=20_000, description="The user's message.")
    spec: DagSpec | None = Field(default=None, description="The DAG open in the editor.")
    model: str | None = Field(default=None, max_length=200)
    regenerate: bool = Field(
        default=False, description="Answer the last user message again (replaces the answer)."
    )
    research: bool = Field(default=True, description="Developer: give it the research tools.")
    mcp: list[McpSelection] | None = Field(
        default=None,
        max_length=10,
        description="External MCP servers (and tools) to attach. Default: every server "
        "scoped to this assistant (ai.mcp_client.servers[].agents); [] attaches none.",
    )


class ChatFinal(BaseModel):
    """``final_answer`` of the assistants (used when the step budget forces an answer)."""

    answer: str = Field(description="The answer for the user (Markdown, cite sources [n]).")
    follow_ups: list[str] = Field(
        default_factory=list, description="Up to three short follow-up questions."
    )


class ChatTurnResult(BaseModel):
    thread: ThreadSummary
    user_message: ChatMessage
    message: ChatMessage


@dataclass
class ChatContext:
    """The caller of a turn (built by the router from the request)."""

    services: Any
    user: str
    role: str
    can: Callable[[str], bool]
    llm: Any
    gateway_factory: Callable[[], Any] | None = None
    team: str | None = None


def split_follow_ups(text: str) -> tuple[str, list[str]]:
    """The answer without its ``<follow_ups>`` block, and the questions in it."""
    match = _FOLLOW_UPS.search(text)
    if not match:
        return text.strip(), []
    questions = []
    for line in match.group(1).splitlines():
        q = _BULLET.sub("", line).strip()
        if q and len(q) <= 300:
            questions.append(q)
    return (text[: match.start()] + text[match.end() :]).strip(), questions


def allowed(name: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, p) for p in patterns)


def assistant_tools(
    ctx: ChatContext,
    mode: Mode,
    state: TurnState,
    spec: DagSpec | None,
    research: bool,
    external: list[AgentTool] | None = None,
) -> list[AgentTool]:
    """The tool set of ``mode`` for this caller (allow-listed, citation-wrapped).

    ``external``: tools of attached external MCP servers; they pass the same allow-lists
    (the developer assistant's role allow-list, the profile's ``tools``)."""
    ai = ai_settings_of(ctx.services.settings)
    profile = getattr(ai.assistants, mode)
    tools: list[AgentTool]
    if mode == "admin":
        from autoair.ai.assistants.admin_tools import admin_tools

        tools = admin_tools(
            ctx.services, state=state, can=ctx.can, user=ctx.user, memory=profile.memory
        )
        tools += external or []  # read-only tools only (see mcp_tools)
    else:
        tools = developer_tools(
            ctx.services,
            state=state,
            spec=spec,
            llm=ctx.llm,
            user=ctx.user,
            gateway_factory=ctx.gateway_factory,
            research=research,
            memory=profile.memory,
        )
        tools += external or []
        tools = filter_tools(tools, ctx.role, ai.guardrails.tool_allowlist)
    tools = [t for t in tools if allowed(t.name, profile.tools)]
    return state.sources.wrap_all(tools)


def mcp_selections(services: Any, body: ChatTurnRequest) -> list[McpSelection]:
    """The external MCP servers this turn attaches (none when the profile disallows it)."""
    profile = getattr(ai_settings_of(services.settings).assistants, body.mode)
    if not profile.allow_external_mcp:
        return []
    if body.mcp is None:
        return default_selections(services, body.mode)
    return list(body.mcp)


@contextlib.asynccontextmanager
async def mcp_tools(
    ctx: ChatContext, body: ChatTurnRequest, emit: Emit
) -> AsyncIterator[list[AgentTool]]:
    """Attach the turn's external MCP servers (scoped to the assistant; the admin
    assistant only gets tools the server marks read-only) for the duration of the turn."""
    selections = mcp_selections(ctx.services, body)
    if not selections:
        yield []
        return
    async with attach_mcp_tools(
        ctx.services, selections, body.mode, read_only=body.mode == "admin"
    ) as (tools, notes):
        for note in notes:
            emit(StatusEvent(stage="mcp", message=note))
        yield tools


def _context(ctx: ChatContext, mode: Mode, spec: DagSpec | None) -> str:
    import datetime

    from autoair.ai.memory import memory_for

    parts = [f"Today is {datetime.date.today().isoformat()}. The user is {ctx.user}."]
    if mode == "developer" and spec is not None:
        from autoair.ai.assist import compact_spec

        parts.append(
            "The DAG open in the editor (call get_current_spec for the full document):\n"
            + compact_spec(spec)
        )
    try:
        items, _ = memory_for(ctx.services).list(visible_to=(ctx.team, ctx.user), limit=200)
    except Exception:
        items = []
    mine = [
        i.text
        for i in items
        if (ADMIN_MEMORY_TAG in i.tags) == (mode == "admin") and i.kind != "fact"
    ][:30]
    if mine:
        parts.append("Remembered conventions/preferences:\n" + "\n".join(f"- {t}" for t in mine))
    return "\n\n".join(parts)


def _load_thread(ctx: ChatContext, body: ChatTurnRequest) -> ChatThread:
    store = thread_store_for(ctx.services)
    if body.thread_id:
        thread = store.get(ctx.user, body.thread_id)
        if thread.mode != body.mode:
            raise ThreadNotFound(body.thread_id)
        return thread
    if body.regenerate:
        raise ValueError("regenerate needs a thread_id")
    return ChatThread(user=ctx.user, mode=body.mode)


def _user_message(thread: ChatThread, body: ChatTurnRequest) -> tuple[ChatMessage, int]:
    """The message this turn answers and its index (regenerate drops the old answer)."""
    if body.regenerate:
        while thread.messages and thread.messages[-1].role == "assistant":
            thread.messages.pop()
        if not thread.messages:
            raise ValueError("nothing to regenerate")
        return thread.messages[-1], len(thread.messages) - 1
    text = body.message.strip()
    if not text:
        raise ValueError("the message is empty")
    message = ChatMessage(role="user", content=text)
    thread.messages.append(message)
    if len(thread.messages) == 1 or thread.title == "New conversation":
        thread.title = title_from(text)
    return message, len(thread.messages) - 1


def _assistant_message(
    result: AgentResult, state: TurnState, usage: Any, max_follow_ups: int
) -> ChatMessage:
    answer, follow_ups = split_follow_ups(result.answer)
    extra = result.extra.get("follow_ups") or []
    follow_ups = list(dict.fromkeys([*follow_ups, *(str(q) for q in extra)]))
    return ChatMessage(
        role="assistant",
        content=answer,
        model=result.model,
        usage=usage,
        tools=[
            ToolCallRecord(
                id=f"s{step.index}",
                name=step.tool,
                arguments=step.arguments,
                ok=step.ok,
                preview=step.output_preview,
                duration_ms=step.duration_ms,
            )
            for step in result.steps
        ],
        citations=state.sources.cited(answer),
        follow_ups=follow_ups[:max_follow_ups],
        proposals=list(state.proposals),
        guardrails=list(result.guardrails),
        stopped=None if result.stopped == "answer" else result.stopped,
        history=dump_history(result.messages) or None,
    )


async def run_turn(
    ctx: ChatContext, body: ChatTurnRequest, emit: Emit, *, state: TurnState | None = None
) -> ChatTurnResult:
    services = ctx.services
    ai = ai_settings_of(services.settings)
    cfg = ai.assistants
    profile = getattr(cfg, body.mode)
    store = thread_store_for(services)
    thread = await asyncio.to_thread(_load_thread, ctx, body)
    user_message, index = _user_message(thread, body)
    await asyncio.to_thread(store.save, thread)
    emit(StatusEvent(stage="thread", message=thread.id))
    state = state or TurnState()
    with track_usage() as meter:
        try:
            if await compact(
                thread,
                ctx.llm,
                upto=index,
                trigger_chars=cfg.compaction_trigger_chars,
                keep_messages=cfg.compaction_keep_messages,
                max_tokens=cfg.summary_max_tokens,
                emit=emit,
            ):
                await asyncio.to_thread(store.save, thread)
            # External MCP connections live for the turn (closed after the answer).
            async with mcp_tools(ctx, body, emit) as external:
                tools = assistant_tools(ctx, body.mode, state, body.spec, body.research, external)
                with prompts.prompt_context(user=ctx.user, team=ctx.team, assistant=body.mode):
                    system = prompts.get(PROMPTS[body.mode])
                task = AgentTask(
                    llm=ctx.llm,
                    task=user_message.content,
                    tools=tools,
                    context=_context(ctx, body.mode, body.spec),
                    system=system,
                    budget=Budget(
                        max_steps=profile.max_steps,
                        max_tokens=ai.agent.token_budget,
                        max_cost_usd=ai.agent.cost_budget_usd,
                        timeout_seconds=ai.agent.timeout_seconds,
                    ),
                    max_tokens=ai.agent.max_tokens_per_call,
                    tool_timeout=ai.agent.tool_timeout_seconds,
                    max_tool_chars=ai.agent.max_tool_output_chars,
                    catalog=services.catalog,
                    registry=services.registry,
                    policy=policy_from_settings(services.settings),
                    node_ids={n.id for n in body.spec.nodes} if body.spec else set(),
                    guardrails=pipeline_for(services),
                    emit=emit,
                    history=history_messages(thread, upto=index),
                    capabilities=[ProcessHistory(trim_history)],
                    final_model=ChatFinal,
                    final_turn_grace_seconds=ai.agent.final_turn_grace_seconds,
                )
                result = await arun_agent(task)
        except BaseException as exc:
            failed = ChatMessage(
                role="assistant",
                content="",
                error="cancelled" if isinstance(exc, asyncio.CancelledError) else str(exc)[:500],
                usage=meter.total,
            )
            thread.messages.append(failed)
            thread.usage = thread.usage + meter.total
            # sync: a cancelled task cannot await any more
            with contextlib.suppress(Exception):
                store.save(thread)
            raise
        usage = meter.total
    message = _assistant_message(result, state, usage, cfg.max_follow_ups)
    thread.messages.append(message)
    thread.usage = thread.usage + usage
    await asyncio.to_thread(store.save, thread)
    return ChatTurnResult(thread=thread.as_summary(), user_message=user_message, message=message)


__all__ = [
    "ChatContext",
    "ChatFinal",
    "ChatTurnRequest",
    "ChatTurnResult",
    "assistant_tools",
    "mcp_selections",
    "run_turn",
    "split_follow_ups",
]
