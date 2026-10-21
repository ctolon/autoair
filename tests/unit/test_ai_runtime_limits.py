"""Regression tests for the agent runtime limits: the time budget (grace period for the
final turn, partial results on timeout), auditing of fully drained streams, error mapping
and the removed LangGraph settings."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import BaseModel
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from autoair.ai.agent import AgentTask, arun_agent
from autoair.ai.events import TokenEvent, error_status, stream_job
from autoair.ai.guardrails import Budget
from autoair.ai.llm import LLM
from autoair.ai.tools import AgentTool


class Q(BaseModel):
    q: str = ""


LOOKUP = AgentTool("lookup", "look something up", Q, lambda a: "ok")


def slow_llm(delays: list[float], answer: str) -> LLM:
    """First turn calls ``lookup``, the next one answers; each turn sleeps first."""
    n = {"i": 0}

    async def fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        i = n["i"]
        n["i"] += 1
        await asyncio.sleep(delays[min(i, len(delays) - 1)])
        if i == 0 and info.function_tools:
            return ModelResponse(parts=[ToolCallPart("lookup", {"q": "1"}, tool_call_id="c1")])
        return ModelResponse(parts=[TextPart(answer)])

    return LLM(model=FunctionModel(fn, model_name="slow"))


def test_timeout_returns_partial_result_with_steps() -> None:
    task = AgentTask(
        llm=slow_llm([0.0, 5.0], "late"),
        task="t",
        tools=[LOOKUP],
        stream=False,
        budget=Budget(timeout_seconds=1.2),
    )
    result = asyncio.run(arun_agent(task))  # must not raise LLMError (-> 502)
    assert result.stopped == "timeout"
    assert [s.tool for s in result.steps] == ["lookup"]


def test_research_timeout_still_gets_graced_final_turn() -> None:
    task = AgentTask(
        llm=slow_llm([1.6, 0.0], "graced"),
        task="t",
        tools=[LOOKUP],
        stream=False,
        budget=Budget(timeout_seconds=2.0),
    )
    assert task.grace_seconds() == pytest.approx(0.5)
    result = asyncio.run(arun_agent(task))
    assert result.answer == "graced"
    assert result.stopped == "timeout"


def test_drained_stream_is_audited_when_client_disconnects() -> None:
    finished: list[tuple[Any, Any]] = []

    async def job(emit: Any) -> dict[str, bool]:
        for i in range(3):
            emit(TokenEvent(text=str(i)))
        return {"ok": True}

    async def main() -> None:
        events = stream_job(job, "x", on_finish=lambda v, e: finished.append((v, e)))
        # Read the tokens (the job finishes meanwhile), then disconnect before the result.
        for _ in range(3):
            await events.__anext__()
        await asyncio.sleep(0.05)
        await events.aclose()  # type: ignore[attr-defined]

    asyncio.run(main())
    assert finished == [({"ok": True}, None)]


def test_cancelled_stream_is_audited_once() -> None:
    finished: list[Any] = []

    async def job(emit: Any) -> dict[str, bool]:
        emit(TokenEvent(text="a"))
        await asyncio.sleep(10)
        return {"ok": True}

    async def main() -> None:
        events = stream_job(job, "x", on_finish=lambda v, e: finished.append(type(e).__name__))
        await events.__anext__()
        await events.aclose()  # type: ignore[attr-defined]

    asyncio.run(main())
    assert finished == ["CancelledError"]


def test_error_status_maps_python_disabled_and_user_llm_errors() -> None:
    from autoair.ai.llm import LLMError
    from autoair.ai.user_llm import UserLlmError
    from autoair.ai.writers import PythonCodeDisabled

    assert error_status(PythonCodeDisabled("python is off")) == 403
    assert error_status(UserLlmError("bad endpoint")) == 400
    assert error_status(LLMError("model down")) == 502
