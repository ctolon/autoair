"""Conversation history for the model, with context compaction of long threads.

The model sees: the rolling summary of older turns (if any), then the recent turns as the
pydantic-ai messages stored with each answer (tool calls and results included; older
threads without them are rendered as text). When the verbatim part grows beyond
``ai.assistants.compaction_trigger_chars`` the older messages (all but the last
``compaction_keep_messages``) are folded into the summary with one model call
(``COMPACT_SYSTEM``) and the thread remembers how many leading messages the summary covers.
Proposals and tool calls are represented compactly so a follow-up like "apply the second
one" still makes sense. During a turn, :func:`trim_history` (a pydantic-ai history
processor) shortens the tool results of earlier turns.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from typing import Any

from pydantic import ValidationError
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolReturnPart,
    UserPromptPart,
)

from autoair.ai.assistants.threads import ChatMessage, ChatThread
from autoair.ai.events import Emit, StatusEvent

#: Characters of one message sent verbatim in the history (long answers are cut).
MAX_MESSAGE_CHARS = 8_000


def render_message(message: ChatMessage, limit: int = MAX_MESSAGE_CHARS) -> str:
    text = message.content.strip()
    if len(text) > limit:
        text = text[:limit] + " … [truncated]"
    notes: list[str] = []
    if message.role == "assistant":
        if message.tools:
            notes.append("tools used: " + ", ".join(sorted({t.name for t in message.tools})))
        for p in message.proposals:
            notes.append(f"proposal {p.id} ({p.kind}, {p.status}): {p.title}")
        if message.error:
            notes.append(f"error: {message.error}")
    if notes:
        text += "\n\n[" + "; ".join(notes) + "]"
    return text or "(empty)"


#: Characters of an earlier turn's tool result kept in the history (trim_history).
MAX_OLD_TOOL_CHARS = 2_000


def _user(text: str) -> ModelRequest:
    return ModelRequest(parts=[UserPromptPart(text)])


def _assistant(text: str) -> ModelResponse:
    return ModelResponse(parts=[TextPart(text)])


def stored_history(message: ChatMessage) -> list[ModelMessage]:
    """The pydantic-ai messages stored with an answer (``[]`` if none or unreadable)."""
    if not message.history:
        return []
    try:
        return list(ModelMessagesTypeAdapter.validate_python(message.history))
    except ValidationError:
        return []


def history_messages(thread: ChatThread, *, upto: int | None = None) -> list[ModelMessage]:
    """The thread as pydantic-ai message history (summary first). ``upto``: only messages
    before that index (the current turn's user message is the run's prompt)."""
    msgs = thread.messages[thread.summarized : upto]
    out: list[ModelMessage] = []
    if thread.summary:
        out.append(_user("Summary of the earlier conversation:\n" + thread.summary))
        out.append(_assistant("Understood - I will continue from there."))
    expect = "user"
    i = 0
    while i < len(msgs):
        m = msgs[i]
        answer = msgs[i + 1] if i + 1 < len(msgs) else None
        stored = (
            stored_history(answer)
            if m.role == "user" and expect == "user" and answer is not None
            else []
        )
        if stored:
            out.extend(stored)
            i += 2
            continue
        if m.role != expect:
            # Keep strict alternation (a lost assistant turn, or two user turns in a row).
            out.append(_assistant("(no answer)") if expect == "assistant" else _user("(continue)"))
        text = render_message(m)
        out.append(_user(text) if m.role == "user" else _assistant(text))
        expect = "assistant" if m.role == "user" else "user"
        i += 1
    if out and isinstance(out[-1], ModelRequest):
        out.append(_assistant("(no answer)"))
    return out


def _trim_part(part: Any) -> Any:
    if (
        isinstance(part, ToolReturnPart)
        and isinstance(part.content, str)
        and len(part.content) > MAX_OLD_TOOL_CHARS
    ):
        return dataclasses.replace(
            part, content=part.content[:MAX_OLD_TOOL_CHARS] + " … [truncated]"
        )
    return part


def trim_history(messages: list[ModelMessage]) -> list[ModelMessage]:
    """History processor: tool results of earlier turns (before the current prompt) are
    cut to :data:`MAX_OLD_TOOL_CHARS` characters - the answers built on them stay."""
    current = max(
        (
            i
            for i, m in enumerate(messages)
            if isinstance(m, ModelRequest) and any(isinstance(p, UserPromptPart) for p in m.parts)
        ),
        default=0,
    )
    out: list[ModelMessage] = []
    for i, m in enumerate(messages):
        if i < current and isinstance(m, ModelRequest):
            parts: list[Any] = [_trim_part(p) for p in m.parts]
            m = dataclasses.replace(m, parts=parts)
        out.append(m)
    return out


def dump_history(messages: Sequence[ModelMessage]) -> list[dict[str, Any]]:
    """JSON-ready form of a run's messages (stored with the answer)."""
    dumped: list[dict[str, Any]] = ModelMessagesTypeAdapter.dump_python(list(messages), mode="json")
    return dumped


def history_chars(thread: ChatThread, upto: int | None = None) -> int:
    msgs = thread.messages[thread.summarized : upto]
    return len(thread.summary) + sum(len(render_message(m)) for m in msgs)


def _transcript(summary: str, messages: Sequence[ChatMessage]) -> str:
    parts = []
    if summary:
        parts.append("Earlier summary:\n" + summary)
    for m in messages:
        parts.append(f"{m.role.upper()}: {render_message(m, 3_000)}")
    return "\n\n".join(parts)


async def compact(
    thread: ChatThread,
    llm: Any,
    *,
    upto: int,
    trigger_chars: int,
    keep_messages: int,
    max_tokens: int = 1_500,
    emit: Emit | None = None,
) -> bool:
    """Fold old messages (before ``upto - keep_messages``) into ``thread.summary`` when the
    history is longer than ``trigger_chars``. Returns whether it compacted."""
    from autoair.ai import prompts

    if history_chars(thread, upto) <= trigger_chars:
        return False
    cut = max(thread.summarized, upto - keep_messages)
    if cut - thread.summarized < 2:
        return False
    folded = thread.messages[thread.summarized : cut]
    if emit is not None:
        emit(StatusEvent(stage="compact", message=f"summarising {len(folded)} earlier messages"))
    summary = await llm.acomplete_text(
        prompts.get("COMPACT_SYSTEM"),
        [{"role": "user", "content": _transcript(thread.summary, folded)}],
        max_tokens,
    )
    thread.summary = summary.strip()[:12_000]
    thread.summarized = cut
    return True


__all__ = [
    "compact",
    "dump_history",
    "history_chars",
    "history_messages",
    "render_message",
    "stored_history",
    "trim_history",
]
