"""Progress events of AI runs and their Server-Sent-Events encoding.

Agent runtimes, writers and streamed assist features yield :data:`AiEvent` objects; the
API turns them into ``text/event-stream`` frames (``event: <type>`` + JSON ``data``) and
the frontend renders them (tokens as they arrive, tool calls as progress, the final
structured result). Every stream ends with exactly one ``result`` or ``error`` event
followed by ``done``.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, TypeAdapter

from autoair.ai.usage import Usage

log = logging.getLogger("autoair.ai.events")


class _Event(BaseModel):
    ts: float = Field(default_factory=time.time, description="Unix time of the event.")


class StatusEvent(_Event):
    type: Literal["status"] = "status"
    stage: str = Field(description="Machine-readable stage, e.g. research, plan, write.")
    message: str = ""


class TokenEvent(_Event):
    type: Literal["token"] = "token"
    text: str


class ToolCallEvent(_Event):
    type: Literal["tool_call"] = "tool_call"
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolResultEvent(_Event):
    type: Literal["tool_result"] = "tool_result"
    id: str
    name: str
    ok: bool
    preview: str = ""
    duration_ms: int = 0


class GuardrailEvent(_Event):
    type: Literal["guardrail"] = "guardrail"
    guardrail: str
    action: Literal["redacted", "masked", "flagged", "blocked", "denied"]
    detail: str = ""


class UsageEvent(_Event):
    type: Literal["usage"] = "usage"
    usage: Usage


class ResultEvent(_Event):
    type: Literal["result"] = "result"
    kind: str = Field(description="Result type, e.g. agent, writer, explain_code.")
    data: dict[str, Any]


class ErrorEvent(_Event):
    type: Literal["error"] = "error"
    message: str
    status: int = 500


class DoneEvent(_Event):
    type: Literal["done"] = "done"
    cancelled: bool = False


AiEvent = Annotated[
    StatusEvent
    | TokenEvent
    | ToolCallEvent
    | ToolResultEvent
    | GuardrailEvent
    | UsageEvent
    | ResultEvent
    | ErrorEvent
    | DoneEvent,
    Field(discriminator="type"),
]
EVENT_ADAPTER: TypeAdapter[AiEvent] = TypeAdapter(AiEvent)

#: Callback that receives events while a (sync or async) run is in progress.
Emit = Callable[[BaseModel], None]


def encode_sse(event: BaseModel) -> bytes:
    """One SSE frame: ``event: <type>`` and the JSON payload."""
    payload = event.model_dump_json(exclude_none=True)
    kind = getattr(event, "type", "message")
    return f"event: {kind}\ndata: {payload}\n\n".encode()


def parse_sse_frames(text: str) -> list[dict[str, Any]]:
    """Decode SSE frames (tests and the proxy's diagnostics)."""
    out: list[dict[str, Any]] = []
    for frame in text.split("\n\n"):
        data = [line[5:].lstrip() for line in frame.splitlines() if line.startswith("data:")]
        if data:
            out.append(json.loads("\n".join(data)))
    return out


class EventChannel:
    """Bridge from emitters (also worker threads) to an async iterator of events.

    Bounded: at most ``maxsize`` events wait for the consumer. Beyond that

    * worker-thread emitters block (up to ``block_timeout`` seconds) until the consumer
      catches up - real backpressure;
    * ``await channel.put(event)`` waits for space;
    * synchronous emits on the event loop (which must not block) spill into an overflow
      where consecutive tokens are merged into one event and, past ``maxsize`` pending
      overflow events, progress events (status / tool calls) are dropped. The final
      ``result`` / ``error`` / ``done`` events are never dropped, and order is kept.
    """

    def __init__(self, maxsize: int = 1_000, *, block_timeout: float = 30.0) -> None:
        self.maxsize = max(1, maxsize)
        self._queue: asyncio.Queue[BaseModel | None] = asyncio.Queue(self.maxsize)
        self._overflow: collections.deque[BaseModel | None] = collections.deque()
        self._loop = asyncio.get_running_loop()
        self._block_timeout = block_timeout
        self.closed = False
        self.dropped = 0

    def _on_loop(self) -> bool:
        try:
            return asyncio.get_running_loop() is self._loop
        except RuntimeError:
            return False

    def _offer(self, item: BaseModel | None) -> None:
        """Enqueue on the loop thread without blocking (spilling into the overflow)."""
        if not self._overflow and not self._queue.full():
            self._queue.put_nowait(item)
            return
        last = self._overflow[-1] if self._overflow else None
        if isinstance(item, TokenEvent) and isinstance(last, TokenEvent):
            self._overflow[-1] = TokenEvent(text=last.text + item.text, ts=last.ts)
            return
        if len(self._overflow) >= self.maxsize and isinstance(item, (StatusEvent, ToolCallEvent)):
            self.dropped += 1
            return
        self._overflow.append(item)

    def _refill(self) -> None:
        while self._overflow and not self._queue.full():
            self._queue.put_nowait(self._overflow.popleft())

    def _put(self, item: BaseModel | None) -> None:
        if self._on_loop():
            self._offer(item)
            return
        # Worker thread: wait for room (backpressure), then hand over to the loop.
        try:
            fut = asyncio.run_coroutine_threadsafe(self._wait_room(), self._loop)
            with contextlib.suppress(Exception):
                fut.result(timeout=self._block_timeout)
            self._loop.call_soon_threadsafe(self._offer, item)
        except RuntimeError:  # loop already closed
            return

    async def _wait_room(self) -> None:
        while self._overflow or self._queue.full():
            if self.closed:
                return
            await asyncio.sleep(0.01)

    async def put(self, event: BaseModel) -> None:
        """Emit, waiting while the channel is full."""
        if self.closed:
            return
        await self._wait_room()
        self._offer(event)

    def emit(self, event: BaseModel) -> None:
        if not self.closed:
            self._put(event)

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self._put(None)

    def pending(self) -> int:
        return self._queue.qsize() + len(self._overflow)

    async def __aiter__(self) -> AsyncIterator[BaseModel]:
        try:
            while True:
                item = await self._queue.get()
                self._refill()
                if item is None:
                    return
                yield item
        finally:  # consumer gone: later emits are no-ops and never block a worker
            self.closed = True


# --------------------------------------------------------------------------- jobs


def error_status(exc: BaseException) -> int:
    from autoair.ai.guardrails import GuardrailViolation
    from autoair.ai.llm import LLMError
    from autoair.ai.user_llm import UserLlmError
    from autoair.ai.writers import PythonCodeDisabled
    from autoair.persistence.documents import DocumentConflict

    if isinstance(exc, DocumentConflict):
        return 409
    if isinstance(exc, PythonCodeDisabled):  # before LLMError (it subclasses it)
        return 403
    if isinstance(exc, UserLlmError):
        return 400
    if isinstance(exc, GuardrailViolation):
        return 422
    if isinstance(exc, LLMError):
        return 502
    if isinstance(exc, asyncio.TimeoutError):
        return 504
    status = getattr(exc, "status_code", None)
    return int(status) if isinstance(status, int) else 500


Job = Callable[[Emit], Awaitable[BaseModel | dict[str, Any]]]


async def stream_job(
    job: Job,
    kind: str,
    *,
    on_finish: Callable[[BaseModel | dict[str, Any] | None, BaseException | None], None]
    | None = None,
) -> AsyncIterator[BaseModel]:
    """Run ``job(emit)`` and yield its events, then a ``result`` (or ``error``) and
    ``done``. Closing the iterator (client disconnect, cancel button) cancels the job."""
    channel = EventChannel()
    outcome: dict[str, Any] = {}

    async def runner() -> None:
        try:
            outcome["value"] = await job(channel.emit)
        except asyncio.CancelledError:
            outcome["cancelled"] = True
            raise
        except Exception as exc:
            outcome["error"] = exc
        finally:
            channel.close()

    task = asyncio.create_task(runner())
    finished = False
    recorded = False

    def record(value: Any, error: BaseException | None) -> None:
        nonlocal recorded
        if recorded or on_finish is None:
            return
        recorded = True
        try:
            on_finish(value, error)
        except Exception:  # an audit failure must not break the stream
            log.warning("recording the %s job failed", kind, exc_info=True)

    try:
        async for event in channel:
            yield event
        await asyncio.gather(task, return_exceptions=True)
        error = outcome.get("error")
        value = outcome.get("value")
        record(value, error)
        if error is not None:
            detail = getattr(error, "detail", None) or str(error) or type(error).__name__
            yield ErrorEvent(message=str(detail), status=error_status(error))
        elif value is not None:
            data = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
            yield ResultEvent(kind=kind, data=data)
        finished = True
        yield DoneEvent()
    finally:
        if not finished and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        # Always audit: also a job that completed while the client was disconnecting
        # (the channel drained, but the consumer went away before the result was sent).
        if outcome.get("cancelled") or ("value" not in outcome and "error" not in outcome):
            record(None, asyncio.CancelledError())
        else:
            record(outcome.get("value"), outcome.get("error"))


__all__ = [
    "EVENT_ADAPTER",
    "AiEvent",
    "DoneEvent",
    "Emit",
    "ErrorEvent",
    "EventChannel",
    "GuardrailEvent",
    "ResultEvent",
    "StatusEvent",
    "TokenEvent",
    "ToolCallEvent",
    "ToolResultEvent",
    "UsageEvent",
    "encode_sse",
    "error_status",
    "parse_sse_frames",
    "stream_job",
]
