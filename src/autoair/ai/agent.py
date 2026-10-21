"""AutoAir's tool-using agents on pydantic-ai.

An :class:`AgentTask` (model, task, typed tools, limits, guardrails) runs as a
``pydantic_ai.Agent`` whose deps are the run's :class:`RunState`:

* tools are :class:`AgentTool` definitions (typed Pydantic inputs; errors go back to the
  model as failed tool results, never fail the run); outputs are truncated, wrapped as
  untrusted data by the guardrails, served from a per-run cache when repeated, and every
  call has its own timeout - pydantic-ai runs the calls of one turn concurrently;
* the answer is a typed ``final_answer`` output (default :class:`FinalAnswer`, a custom
  ``final_model`` for the assistants / troubleshooting) or plain text; a proposed DagSpec
  is validated before return;
* limits: when the step, token or cost budget is spent - or only the final-turn grace
  period of the wall-clock budget is left - the function tools are withdrawn so the next
  turn must answer (``UsageLimits`` is the hard stop); on timeout the completed steps are
  returned with ``stopped="timeout"``;
* the agent proposes, the user applies: tools that change something (``AgentTool.kind``
  ``write``) and any other tool ``ai.approvals`` selects (``AgentTask.approval_required``)
  are pydantic-ai deferred tools - calling one stops the run with ``stopped="approval"``
  and the pending calls; :mod:`autoair.ai.approvals` stores the paused run and a later run
  with ``AgentTask.resume`` continues it with the user's decisions.

Progress is reported through the task's ``emit`` callback as :mod:`autoair.ai.events`
objects (tokens, tool calls/results, usage) so the API can stream it.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import json
import time
from collections.abc import AsyncIterable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError
from pydantic_ai import Tool, ToolOutput
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import (
    ApprovalRequired,
    ToolFailed,
    UnexpectedModelBehavior,
    UsageLimitExceeded,
)
from pydantic_ai.messages import (
    AgentStreamEvent,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    UserPromptPart,
)
from pydantic_ai.models import ModelRequestContext
from pydantic_ai.tools import (
    DeferredToolRequests,
    DeferredToolResults,
    RunContext,
    ToolApproved,
    ToolDefinition,
    ToolDenied,
)
from pydantic_ai.usage import UsageLimits

from autoair.ai import prompts
from autoair.ai.approvals import PendingToolCall, ToolDecision, parse_arguments, redact_arguments
from autoair.ai.assist import _RawFinding, _RawFindings, to_findings
from autoair.ai.events import (
    Emit,
    GuardrailEvent,
    StatusEvent,
    TokenEvent,
    ToolCallEvent,
    ToolResultEvent,
    UsageEvent,
)
from autoair.ai.findings import Finding
from autoair.ai.guardrails import Budget, GuardrailNote, GuardrailPipeline, StreamScrubber
from autoair.ai.llm import LLMError
from autoair.ai.models import agent_for, llm_errors, to_usage
from autoair.ai.problems import SpecProblem, check_spec
from autoair.ai.tools import MAX_TOOL_CHARS, AgentStep, AgentTool, render_output
from autoair.ai.usage import Usage, track_usage
from autoair.catalog import OperatorCatalog
from autoair.registry import HandlerRegistry
from autoair.spec import DagSpec
from autoair.validation import DEFAULT_POLICY, Policy

FINAL_TOOL = "final_answer"
FINAL_TURN_GRACE_SECONDS = 30.0
Stopped = Literal["answer", "max_steps", "budget", "timeout", "approval"]
#: Characters of a tool call's arguments shown for approval when the task sets no limit.
APPROVAL_ARGUMENT_CHARS = 4_000


class FinalAnswer(BaseModel):
    answer: str = Field(description="Concise answer for the user (Markdown).")
    findings: list[_RawFinding] = Field(
        default_factory=list,
        description="Issues found, each with severity, category, title, message, node_id, "
        "field and an optional JSON patch (op, path, value_json).",
    )
    spec: dict[str, Any] | None = Field(
        default=None, description="A complete proposed DagSpec, only if you produced one."
    )


class AgentResult(BaseModel):
    answer: str
    findings: list[Finding] = Field(default_factory=list)
    proposed_spec: DagSpec | None = None
    proposed_ok: bool | None = None
    problems: list[SpecProblem] = Field(default_factory=list)
    steps: list[AgentStep] = Field(default_factory=list)
    stopped: Stopped = "answer"
    model: str | None = None
    usage: Usage = Field(default_factory=Usage)
    runtime: str = "pydantic-ai"
    guardrails: list[str] = Field(
        default_factory=list, description="Guardrail actions taken during the run."
    )
    extra: dict[str, Any] = Field(
        default_factory=dict,
        description="Further fields of a custom final answer (AgentTask.final_model).",
    )
    #: The run's new pydantic-ai messages (chat threads store them as history).
    messages: list[Any] = Field(default_factory=list, exclude=True)
    pending: list[PendingToolCall] = Field(
        default_factory=list,
        description="stopped=approval: the tool calls waiting for the user's decision.",
    )
    requests: int = Field(default=0, description="Model requests of this run.")


def _noop(_event: BaseModel) -> None:
    return None


@dataclass
class ResumeState:
    """The user's decisions on the pending calls of a paused run."""

    calls: list[PendingToolCall]
    decisions: list[ToolDecision]
    decided_by: str = ""

    def results(self, guardrails: GuardrailPipeline | None = None) -> DeferredToolResults:
        """The decisions for pydantic-ai (denial reasons cleaned, see :func:`clean_reason`)."""
        approvals: dict[str, Any] = {}
        for d in self.decisions:
            if d.approved:
                approvals[d.id] = ToolApproved()
            else:
                text = "The user denied this tool call; do not try it again."
                if d.reason:
                    text += f" Their reason: {clean_reason(d.reason, guardrails)}"
                approvals[d.id] = ToolDenied(text)
        return DeferredToolResults(approvals=approvals)


def clean_reason(reason: str, guardrails: GuardrailPipeline | None = None) -> str:
    """A denial reason is free text the user typed; it reaches the model's context and the
    stored steps: secrets are scrubbed, registered secrets masked, the guardrails applied."""
    from autoair.ai.rules import scrub_secrets
    from autoair.secret_mask import mask

    text = mask(scrub_secrets(reason))
    return guardrails.sanitize(text) if guardrails is not None else text


@dataclass
class AgentTask:
    """Everything one agent run needs."""

    llm: Any
    task: str
    tools: Sequence[AgentTool]
    context: str = ""
    system: str = ""
    budget: Budget = field(default_factory=Budget)
    max_tokens: int = 8_000
    tool_timeout: float = 90
    max_tool_chars: int = MAX_TOOL_CHARS
    catalog: OperatorCatalog | None = None
    registry: HandlerRegistry | None = None
    policy: Policy = DEFAULT_POLICY
    node_ids: set[str] | None = None
    guardrails: GuardrailPipeline | None = None
    emit: Emit = _noop
    stream: bool = True
    final_turn_grace_seconds: float = FINAL_TURN_GRACE_SECONDS
    #: Earlier conversation turns (pydantic-ai messages), sent before the task.
    history: list[ModelMessage] = field(default_factory=list)
    #: Further pydantic-ai capabilities of the run (e.g. a history processor).
    capabilities: list[Any] = field(default_factory=list)
    #: Type of the ``final_answer`` output (default :class:`FinalAnswer`); it must have an
    #: ``answer`` field and defaults for everything else.
    final_model: type[BaseModel] | None = None
    #: Tools whose calls wait for the user's approval (the run stops with them pending).
    approval_required: frozenset[str] = frozenset()
    #: Continue a paused run: ``history`` ends with the pending calls, these decide them.
    resume: ResumeState | None = None
    #: Steps recorded before this run (a resumed run numbers its steps after them).
    step_offset: int = 0
    #: Characters of a pending call's arguments kept for display (secrets masked).
    approval_argument_chars: int = APPROVAL_ARGUMENT_CHARS

    def __post_init__(self) -> None:
        if not self.system:
            self.system = prompts.get("AGENT_SYSTEM")

    def grace_seconds(self) -> float:
        """Wall-clock time reserved for the final (forced answer) turn: at most a quarter of
        the run's budget so short budgets still leave room for research."""
        return max(0.0, min(self.final_turn_grace_seconds, self.budget.timeout_seconds / 4))


@dataclass
class RunState:
    """The agent's deps: limits, recorded steps and the per-run tool cache."""

    task: AgentTask
    steps: list[AgentStep] = field(default_factory=list)
    cache: dict[str, str] = field(default_factory=dict)
    notes: list[GuardrailNote] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    #: Why the function tools were withdrawn (the next turn must answer).
    forced: Literal["steps", "budget", "time"] | None = None
    forced_reason: str = ""

    def limit(self, requests: int) -> None:
        """Withdraw the tools when the next model request is the last one allowed."""
        if self.forced is not None:
            return
        budget = self.task.budget
        reason = budget.exceeded(
            steps=requests + 1, tokens=self.usage.total_tokens, cost=self.usage.cost_usd
        )
        if reason is not None:
            self.forced = "steps" if reason.startswith("step") else "budget"
            if reason.startswith("time"):
                self.forced = "time"
        elif budget.remaining_seconds() <= self.task.grace_seconds():
            self.forced, reason = "time", "time limit nearly reached"
        self.forced_reason = reason or ""

    async def call(self, tool: AgentTool, call_id: str, arguments: dict[str, Any]) -> str:
        """Run one tool call: validated, cached, time-boxed, truncated, untrusted-wrapped."""
        task = self.task
        task.emit(ToolCallEvent(id=call_id, name=tool.name, arguments=arguments))
        started = time.monotonic()
        key = f"{tool.name}:{json.dumps(arguments, sort_keys=True, default=str)}"
        ok, text = True, ""
        if key in self.cache:
            text = "(same call as before) " + self.cache[key]
        else:
            ok, text = await self._run(tool, arguments)
            if ok:
                self.cache[key] = text
        ms = int((time.monotonic() - started) * 1000)
        self.steps.append(
            AgentStep(
                index=task.step_offset + len(self.steps),
                tool=tool.name,
                arguments=arguments,
                ok=ok,
                output_preview=text[:300],
                duration_ms=ms,
            )
        )
        task.emit(
            ToolResultEvent(id=call_id, name=tool.name, ok=ok, preview=text[:300], duration_ms=ms)
        )
        if not ok:
            raise ToolFailed(text)
        if task.guardrails is not None:
            text = task.guardrails.untrusted(text, f"tool:{tool.name}", self.notes)
        return text

    async def _run(self, tool: AgentTool, arguments: dict[str, Any]) -> tuple[bool, str]:
        task = self.task
        try:
            args = tool.input_model.model_validate(arguments)
        except ValidationError as exc:
            return False, f"invalid arguments: {exc}"
        timeout = min(task.tool_timeout, max(1.0, task.budget.remaining_seconds()))
        try:
            if inspect.iscoroutinefunction(tool.handler):
                value = await asyncio.wait_for(tool.handler(args), timeout)
            else:
                value = await asyncio.wait_for(asyncio.to_thread(tool.handler, args), timeout)
        except LLMError:
            raise
        except asyncio.TimeoutError:
            return False, f"tool timed out after {timeout:.0f}s"
        except Exception as exc:  # tool failures are reported to the model
            return False, f"{type(exc).__name__}: {exc}"
        return True, render_output(value, task.max_tool_chars)


def pydantic_tool(tool: AgentTool, *, approval: bool = False) -> Tool[RunState]:
    """``tool`` for pydantic-ai; with ``approval`` a call is deferred until approved."""

    async def run(ctx: RunContext[RunState], **arguments: Any) -> str:
        if approval and not ctx.tool_call_approved:
            raise ApprovalRequired(metadata={"kind": tool.kind})
        return await ctx.deps.call(tool, ctx.tool_call_id or tool.name, arguments)

    tool_: Tool[RunState] = Tool.from_schema(
        run,
        name=tool.name,
        description=tool.description,
        json_schema=tool.json_schema(),
        takes_ctx=True,
    )
    return tool_


@dataclass
class RunControl(AbstractCapability[RunState]):
    """Step / token / cost / time limits (tools withdrawn for a forced final turn) and
    progress events of the run."""

    @classmethod
    def get_serialization_name(cls) -> str | None:
        return None

    async def prepare_tools(
        self, ctx: RunContext[RunState], tool_defs: list[ToolDefinition]
    ) -> list[ToolDefinition]:
        state = ctx.deps
        state.limit(ctx.usage.requests)
        return [] if state.forced else tool_defs

    async def before_model_request(
        self, ctx: RunContext[RunState], request_context: ModelRequestContext
    ) -> ModelRequestContext:
        state = ctx.deps
        state.limit(ctx.usage.requests)
        state.task.emit(StatusEvent(stage="model", message=f"step {ctx.usage.requests + 1}"))
        messages = list(request_context.messages)
        if state.forced and messages and isinstance(messages[-1], ModelRequest):
            nudge = UserPromptPart(f"Budget reached ({state.forced_reason}): answer now.")
            last = messages[-1]
            messages[-1] = dataclasses.replace(last, parts=[*last.parts, nudge])
            request_context.messages = messages
        return request_context

    async def after_model_request(
        self,
        ctx: RunContext[RunState],
        *,
        request_context: ModelRequestContext,
        response: ModelResponse,
    ) -> ModelResponse:
        state = ctx.deps
        state.usage = state.usage + to_usage(response.usage, state.task.llm.price)
        state.task.emit(UsageEvent(usage=state.usage))
        return response


def _token_handler(
    task: AgentTask,
) -> Callable[[RunContext[RunState], AsyncIterable[AgentStreamEvent]], Any]:
    llm = task.llm
    scrubber = StreamScrubber(llm.guard, llm.notes) if getattr(llm, "guard", None) else None

    def emit(text: str) -> None:
        if scrubber is not None:
            text = scrubber.feed(text)
        if text:
            task.emit(TokenEvent(text=text))

    async def handler(ctx: RunContext[RunState], events: AsyncIterable[AgentStreamEvent]) -> None:
        async for event in events:
            if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart):
                emit(event.part.content)
            elif isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
                emit(event.delta.content_delta)
        if scrubber is not None:
            tail = scrubber.flush()
            if tail:
                task.emit(TokenEvent(text=tail))

    return handler


def finish_result(
    task: AgentTask,
    final: BaseModel | None,
    *,
    steps: list[AgentStep],
    stopped: Stopped,
    usage: Usage,
    notes: Sequence[GuardrailNote] = (),
) -> AgentResult:
    if final is None:
        answer = (
            "The assistant ran out of time before giving an answer; the steps it completed "
            "are listed below."
            if stopped == "timeout"
            else "The assistant stopped before giving an answer."
        )
        final = (task.final_model or FinalAnswer).model_validate({"answer": answer})
    raw_findings: list[_RawFinding] = list(getattr(final, "findings", None) or [])
    proposed: dict[str, Any] | None = getattr(final, "spec", None)
    result = AgentResult(
        answer=str(getattr(final, "answer", "")),
        steps=steps,
        stopped=stopped,
        model=str(task.llm.model),
        usage=usage,
        guardrails=sorted({f"{n.guardrail}:{n.action}" for n in notes}),
    )
    if not isinstance(final, FinalAnswer):
        result.extra = final.model_dump(mode="json", exclude={"answer", "findings", "spec"})
    ids = set(task.node_ids or ())
    if proposed:
        check = check_spec(
            proposed, catalog=task.catalog, registry=task.registry, policy=task.policy
        )
        result.proposed_spec, result.proposed_ok, result.problems = (
            check.spec,
            check.ok,
            check.problems,
        )
        ids |= {str(n.get("id")) for n in proposed.get("nodes") or [] if isinstance(n, dict)}
    result.findings = to_findings(_RawFindings(summary="", findings=raw_findings), ids)
    return result


def pending_calls(task: AgentTask, requests: DeferredToolRequests) -> list[PendingToolCall]:
    """The deferred calls for the approval card: arguments redacted and cut."""
    from autoair.ai.rules import scrub_secrets

    pipeline = task.guardrails

    def scrub(text: str) -> str:
        text = scrub_secrets(text)
        return pipeline.sanitize(text) if pipeline is not None else text

    by_name = {t.name: t for t in task.tools}
    out = []
    for part in requests.approvals:
        tool = by_name.get(part.tool_name)
        arguments, cut = redact_arguments(
            parse_arguments(part.args), scrub, task.approval_argument_chars
        )
        out.append(
            PendingToolCall(
                id=part.tool_call_id,
                tool=part.tool_name,
                kind=tool.kind if tool is not None else "write",
                description=(tool.description if tool is not None else "")[:300],
                arguments=arguments,
                truncated=cut,
            )
        )
    return out


def _denied_steps(task: AgentTask) -> list[AgentStep]:
    """Steps for the calls the user denied (they never ran)."""
    resume = task.resume
    if resume is None:
        return []
    calls = {c.id: c for c in resume.calls}
    steps: list[AgentStep] = []
    for d in resume.decisions:
        call = calls.get(d.id)
        if d.approved or call is None:
            continue
        args = call.arguments if isinstance(call.arguments, dict) else {}
        note = f"denied by {resume.decided_by}" if resume.decided_by else "denied"
        steps.append(
            AgentStep(
                index=task.step_offset + len(steps),
                tool=call.tool,
                arguments=args,
                ok=False,
                output_preview=(
                    f"{note}: {clean_reason(d.reason, task.guardrails)}" if d.reason else note
                )[:300],
            )
        )
    return steps


def _response_text(messages: Sequence[ModelMessage]) -> str:
    return "".join(
        p.content
        for m in messages
        if isinstance(m, ModelResponse)
        for p in m.parts
        if isinstance(p, TextPart)
    ).strip()


async def arun_agent(task: AgentTask) -> AgentResult:
    """Run ``task`` as a pydantic-ai agent and return its (possibly partial) result."""
    final_model = task.final_model or FinalAnswer
    state = RunState(task)
    # Denied calls never ran; they are recorded first so the numbering continues after them.
    state.steps.extend(_denied_steps(task))
    budget = task.budget
    final: BaseModel | None = None
    stopped: Stopped = "answer"
    messages: list[ModelMessage] = []
    pending: list[PendingToolCall] = []
    requests = 0
    llm = task.llm
    outputs: list[Any] = [
        ToolOutput(final_model, name=FINAL_TOOL, description="Finish: give the final answer."),
        str,
    ]
    if task.approval_required or task.resume is not None:
        outputs.append(DeferredToolRequests)
    with track_usage() as meter, llm_errors(llm):
        model = await llm.pydantic_model()
        agent = agent_for(
            llm,
            model,
            task.system,
            outputs,
            max_tokens=task.max_tokens,
            retries=2,
            deps_type=RunState,
            tools=[pydantic_tool(t, approval=t.name in task.approval_required) for t in task.tools],
            capabilities=[RunControl(), *task.capabilities],
        )
        run = agent.run(
            None if task.resume is not None else f"{task.context}\n\n{task.task}".strip(),
            message_history=task.history,
            deferred_tool_results=(
                task.resume.results(task.guardrails) if task.resume is not None else None
            ),
            deps=state,
            # The hard stop behind the forced final turn (output retries need a little room).
            usage_limits=UsageLimits(request_limit=budget.max_steps + 2),
            event_stream_handler=_token_handler(task) if task.stream else None,
        )
        try:
            result = await asyncio.wait_for(run, max(0.001, budget.remaining_seconds()))
        except asyncio.TimeoutError:
            stopped = "timeout"
        except UsageLimitExceeded:
            stopped = "max_steps"
        except UnexpectedModelBehavior:
            # E.g. the model kept calling withdrawn tools in its forced final turn.
            if state.forced is None:
                raise
            stopped = (
                "budget"
                if state.forced == "budget"
                else "timeout"
                if state.forced == "time"
                else "max_steps"
            )
        else:
            output = result.output
            messages = list(result.new_messages())
            requests = result.usage.requests
            if isinstance(output, DeferredToolRequests):
                pending = pending_calls(task, output)
                stopped = "approval"
                final = final_model.model_validate({"answer": _response_text(messages)})
                task.emit(
                    StatusEvent(
                        stage="approval",
                        message=f"{len(pending)} tool call(s) wait for your approval",
                    )
                )
            elif isinstance(output, str):
                final = final_model.model_validate({"answer": output.strip() or "(no answer)"})
            else:
                final = output
            if stopped == "approval":
                pass
            elif state.forced == "budget":
                stopped = "budget"
            elif state.forced == "time" or budget.remaining_seconds() <= 0:
                stopped = "timeout"
    for note in state.notes:
        task.emit(GuardrailEvent(guardrail=note.guardrail, action=note.action, detail=note.detail))
    out = finish_result(
        task, final, steps=state.steps, stopped=stopped, usage=meter.total, notes=state.notes
    )
    out.messages = messages
    out.pending = pending
    out.requests = requests
    return out


__all__ = [
    "FINAL_TOOL",
    "AgentResult",
    "AgentTask",
    "FinalAnswer",
    "ResumeState",
    "RunControl",
    "RunState",
    "arun_agent",
    "finish_result",
    "pending_calls",
    "pydantic_tool",
]
