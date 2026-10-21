"""A scripted LLM for tests and demos, on pydantic-ai's ``FunctionModel``.

:class:`FakeLLM` answers with ``responses`` in order and records every model request as
a :class:`FakeCall`. A response is

* a ``str`` - the answer text (parsed as JSON for a structured-only answer),
* a ``dict`` / Pydantic model - a structured answer (the output tool is called with it),
* :func:`tool_calls` - tool calls (plus optional text),
* a pydantic-ai ``ModelResponse``,
* or a callable ``(FakeCall) -> any of the above``.

It streams like a real model (word by word, then the tool calls).
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from autoair.ai.llm import LLM, ChatMessage, LLMError


@dataclass(frozen=True)
class ToolCalls:
    calls: tuple[tuple[str, dict[str, Any]], ...]
    text: str = ""


def tool_calls(*calls: tuple[str, dict[str, Any]], text: str = "") -> ToolCalls:
    """A scripted response that calls tools (``("name", {args})`` each)."""
    return ToolCalls(tuple(calls), text)


@dataclass
class FakeCall:
    """One model request as the fake saw it."""

    kind: Literal["text", "json", "chat"]
    system: str
    messages: list[ChatMessage]
    json_schema: dict[str, Any] | None
    tools: list[str]
    info: AgentInfo
    history: list[ModelMessage]

    @property
    def tool_results(self) -> list[ToolReturnPart | RetryPromptPart]:
        """Tool results (and failed calls) sent with this request."""
        last = self.history[-1] if self.history else None
        if not isinstance(last, ModelRequest):
            return []
        return [p for p in last.parts if isinstance(p, (ToolReturnPart, RetryPromptPart))]


FakeResponse = Any


def _flatten(messages: list[ModelMessage]) -> list[ChatMessage]:
    out: list[ChatMessage] = []
    for m in messages:
        if isinstance(m, ModelRequest):
            text = "\n".join(
                str(p.content)
                for p in m.parts
                if isinstance(p, (UserPromptPart, ToolReturnPart, RetryPromptPart))
            )
            if text:
                out.append({"role": "user", "content": text})
        else:
            text = "".join(p.content for p in m.parts if isinstance(p, TextPart))
            if text:
                out.append({"role": "assistant", "content": text})
    return out


@dataclass
class _Script:
    responses: list[FakeResponse]
    calls: list[FakeCall] = field(default_factory=list)

    def call_of(self, messages: list[ModelMessage], info: AgentInfo) -> FakeCall:
        schema = info.output_tools[0].parameters_json_schema if info.output_tools else None
        kind: Literal["text", "json", "chat"] = (
            "chat" if info.function_tools else "json" if schema is not None else "text"
        )
        return FakeCall(
            kind=kind,
            system=info.instructions or "",
            messages=_flatten(messages),
            json_schema=schema,
            tools=[t.name for t in info.function_tools],
            info=info,
            history=list(messages),
        )

    def respond(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        call = self.call_of(messages, info)
        self.calls.append(call)
        if not self.responses:
            raise LLMError("FakeLLM has no scripted responses left")
        out = self.responses.pop(0)
        if callable(out) and not isinstance(out, type):
            out = out(call)
        return _response(out, info)


def _response(out: Any, info: AgentInfo) -> ModelResponse:
    if isinstance(out, ModelResponse):
        return out
    if isinstance(out, ToolCalls):
        parts: list[Any] = [TextPart(out.text)] if out.text else []
        parts += [
            ToolCallPart(name, args, tool_call_id=f"t{i}")
            for i, (name, args) in enumerate(out.calls)
        ]
        return ModelResponse(parts=parts)
    if isinstance(out, BaseModel):
        out = out.model_dump(mode="json")
    if isinstance(out, str) and info.output_tools and not info.allow_text_output:
        try:
            parsed = json.loads(out)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            out = parsed
    if isinstance(out, dict):
        if info.output_tools:
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, out)])
        return ModelResponse(parts=[TextPart(json.dumps(out))])
    return ModelResponse(parts=[TextPart(str(out))])


class FakeLLM(LLM):
    """Scripted :class:`~autoair.ai.llm.LLM` (``responses`` are consumed in order)."""

    def __init__(
        self,
        responses: list[FakeResponse] | None = None,
        *,
        model_name: str = "fake-model",
        max_output_tokens: int = 16_000,
        pricing: dict[str, dict[str, float]] | None = None,
    ) -> None:
        self.script = _Script(list(responses or []))
        self.model_name = model_name

        async def function(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            return self.script.respond(messages, info)

        async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[Any]:
            response = self.script.respond(messages, info)
            for part in response.parts:
                if isinstance(part, TextPart):
                    for piece in re.findall(r"\S+\s*|\s+", part.content):
                        yield piece
                        await asyncio.sleep(0)
            calls = [p for p in response.parts if isinstance(p, ToolCallPart)]
            if calls:
                yield {
                    i: DeltaToolCall(c.tool_name, c.args_as_json_str(), tool_call_id=c.tool_call_id)
                    for i, c in enumerate(calls)
                }

        super().__init__(
            model=FunctionModel(function, stream_function=stream, model_name=model_name),
            max_output_tokens=max_output_tokens,
            pricing=pricing,
        )

    @property
    def responses(self) -> list[FakeResponse]:
        return self.script.responses

    @property
    def calls(self) -> list[FakeCall]:
        return self.script.calls

    @property
    def model(self) -> str:
        return self.model_name

    @property
    def provider(self) -> str:
        return "fake"

    def with_model(self, model: str | None) -> FakeLLM:
        if model:
            self.model_name = model
        return self


__all__ = ["FakeCall", "FakeLLM", "ToolCalls", "tool_calls"]
