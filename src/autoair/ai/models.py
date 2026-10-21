"""pydantic-ai glue: models and providers from AutoAir configuration, one-shot calls,
streaming, usage accounting, guardrails and embeddings.

Imported only when a model is used (``autoair[ai]``). Every provider gets an
``httpx2.AsyncClient`` built from ``outbound.*`` (CA bundle, client certificate, proxies;
:func:`autoair.net.tls.async_client`), pinned to validated public addresses for untrusted
per-user endpoints. Groq and Cohere still use the legacy ``httpx`` client of their SDKs, so
they keep the SDK's own client (``outbound.*`` does not apply to them); Bedrock talks to AWS
through boto.
"""

from __future__ import annotations

import contextlib
import dataclasses
import inspect
import json
import weakref
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

import httpx2
import pydantic_ai
from pydantic_ai import Agent, StructuredDict
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import (
    AgentRunError,
    ContentFilterError,
    ModelAPIError,
    ModelHTTPError,
    UsageLimitExceeded,
    UserError,
)
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelResponsePart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import Model, ModelRequestContext, infer_model
from pydantic_ai.providers import Provider, infer_provider_class
from pydantic_ai.settings import ModelSettings
from pydantic_ai.tools import RunContext
from pydantic_ai.usage import RequestUsage

from autoair.ai.guardrails import UNTRUSTED_RULE, StreamScrubber
from autoair.ai.llm import (
    LLM,
    PROVIDER_EXTRAS,
    ChatMessage,
    LLMError,
    ModelConfig,
    run_sync,
)
from autoair.ai.usage import ModelPrice, Usage, record_usage
from autoair.net import tls

# AutoAir owns its console output: no first-run banner on the server's terminal.
pydantic_ai.BANNER_ENABLED = False

#: AutoAir provider -> pydantic-ai provider (model id prefix).
PYDANTIC_PROVIDERS: dict[str, str] = {
    "anthropic": "anthropic",
    "openai": "openai",
    "azure": "azure",
    "gemini": "google",
    "google-cloud": "google-cloud",
    "ollama": "ollama",
    "bedrock": "bedrock",
    "groq": "groq",
    "mistral": "mistral",
    "cohere": "cohere",
}
#: Providers whose SDK needs a legacy ``httpx`` client (AutoAir passes none).
LEGACY_HTTPX = frozenset({"groq", "cohere"})
#: Providers without an HTTP client parameter.
NO_HTTP_CLIENT = frozenset({"bedrock"})

DEFAULT_EMBEDDING_MODELS: dict[str, str] = {
    "openai": "text-embedding-3-small",
    "gemini": "gemini-embedding-001",
    "ollama": "nomic-embed-text",
    "cohere": "embed-v4.0",
}


# --------------------------------------------------------------------------- models


def _missing_extra(config: ModelConfig, exc: BaseException) -> LLMError:
    extra = PROVIDER_EXTRAS.get(config.provider, "ai-all")
    return LLMError(
        f"the {config.provider} provider needs pip install 'autoair[ai,{extra}]' ({exc})"
    )


def http_client(config: ModelConfig, transport: Any = None) -> httpx2.AsyncClient:
    """The provider's HTTP client with AutoAir's outbound TLS/proxy settings."""
    if config.pin_public_ip:
        from autoair.ai.net import PinnedAsyncTransport

        transport = PinnedAsyncTransport(transport)
    return tls.async_client(
        "ai",
        transport=transport,
        timeout=httpx2.Timeout(config.timeout, connect=min(15.0, config.timeout)),
        headers=dict(config.extra_headers),
    )


def build_provider(
    config: ModelConfig, transport: Any = None
) -> tuple[str, Provider[Any], httpx2.AsyncClient | None]:
    """``(pydantic-ai provider name, provider, the HTTP client it owns)``."""
    name = PYDANTIC_PROVIDERS.get(config.provider)
    if name is None:
        raise LLMError(f"unknown AI provider {config.provider!r}")
    try:
        cls = infer_provider_class(name)
    except ImportError as exc:
        raise _missing_extra(config, exc) from exc
    params = inspect.signature(cls.__init__).parameters
    key = config.api_key.get_secret_value() if config.api_key else None
    kwargs: dict[str, Any] = {}
    if key and "api_key" in params:
        kwargs["api_key"] = key
    if config.base_url:
        if name == "azure":
            kwargs["azure_endpoint"] = config.base_url
        elif "base_url" in params:
            kwargs["base_url"] = config.base_url
        else:
            raise LLMError(f"the {config.provider} provider takes no base URL")
    if name == "azure" and config.api_version:
        kwargs["api_version"] = config.api_version
    if name == "ollama":
        # pydantic-ai speaks Ollama's OpenAI-compatible API under /v1.
        url = (config.base_url or "http://localhost:11434").rstrip("/")
        kwargs["base_url"] = url if url.endswith("/v1") else f"{url}/v1"
    client: httpx2.AsyncClient | None = None
    if name not in LEGACY_HTTPX | NO_HTTP_CLIENT and "http_client" in params:
        client = kwargs["http_client"] = http_client(config, transport)
    try:
        provider = cls(**kwargs)
    except ImportError as exc:
        raise _missing_extra(config, exc) from exc
    except UserError as exc:
        raise LLMError(f"{config.provider}: {exc}") from exc
    return name, provider, client


def build_model(
    config: ModelConfig, *, transport: Any = None
) -> tuple[Model, httpx2.AsyncClient | None]:
    """The pydantic-ai model of ``config`` and the HTTP client it uses."""
    model_name = config.resolved_model
    if not model_name:
        raise LLMError(f"set ai.model: the {config.provider} provider has no default model")
    name, provider, client = build_provider(config, transport)
    if name == "openai" and config.base_url:
        name = "openai-chat"  # OpenAI-compatible servers speak Chat Completions
    try:
        return infer_model(f"{name}:{model_name}", provider_factory=lambda _: provider), client
    except ImportError as exc:
        raise _missing_extra(config, exc) from exc
    except UserError as exc:
        raise LLMError(f"{config.provider}: {exc}") from exc


# --------------------------------------------------------------------------- errors


def _redact(text: str, llm: LLM) -> str:
    config = llm.config
    key = config.api_key.get_secret_value() if config and config.api_key else None
    return text.replace(key, "***") if key else text


def _blocked(exc: BaseException) -> BaseException | None:
    """The SSRF block (:class:`autoair.ai.net.BlockedAddress`) behind an SDK error."""
    from autoair.ai.net import BlockedAddress

    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, BlockedAddress):
            return current
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return None


@contextlib.contextmanager
def llm_errors(llm: LLM) -> Iterator[None]:
    """pydantic-ai / SDK errors as :class:`LLMError` (credentials redacted)."""
    try:
        yield
    except (LLMError, UsageLimitExceeded):
        raise
    except ModelHTTPError as exc:
        status = exc.status_code
        detail = _redact(str(exc.body or exc.message)[:500], llm)
        if status in (401, 403):
            raise LLMError(f"{llm.provider}: authentication failed ({status}): {detail}") from exc
        if status == 429:
            raise LLMError(f"{llm.provider}: rate limited: {detail}") from exc
        raise LLMError(f"{llm.provider}: HTTP {status}: {detail}") from exc
    except ContentFilterError as exc:
        raise LLMError(f"the model declined the request ({exc.message})") from exc
    except (ModelAPIError, AgentRunError, UserError) as exc:
        blocked = _blocked(exc)
        if blocked is not None:
            raise LLMError(f"{llm.provider}: endpoint refused: {blocked}") from exc
        raise LLMError(_redact(f"{llm.provider}: {exc}", llm)) from exc
    except ImportError as exc:
        config = llm.config
        if config is None:
            raise
        raise _missing_extra(config, exc) from exc


# --------------------------------------------------------------------------- usage


def to_usage(usage: RequestUsage, price: ModelPrice | None) -> Usage:
    """pydantic-ai counts cached tokens inside ``input_tokens``; AutoAir does not."""
    cached = usage.cache_read_tokens + usage.cache_write_tokens
    return Usage(
        requests=1,
        input_tokens=max(0, usage.input_tokens - cached),
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_tokens,
    ).priced(price)


# --------------------------------------------------------------------------- guardrails


def _sanitize_json(value: Any, fn: Any) -> Any:
    if isinstance(value, str):
        return fn(value)
    if isinstance(value, list):
        return [_sanitize_json(v, fn) for v in value]
    if isinstance(value, dict):
        return {k: _sanitize_json(v, fn) for k, v in value.items()}
    return value


def _tool_args(part: ToolCallPart) -> dict[str, Any]:
    try:
        return part.args_as_dict()
    except (ValueError, TypeError):
        return {}


@dataclass
class AutoAirCalls(AbstractCapability[Any]):
    """Hooks on every model request of an AutoAir call: usage is recorded
    (:func:`autoair.ai.usage.track_usage`), truncated answers become errors and - with a
    guardrail pipeline - outgoing text is redacted / masked and answers are scrubbed and
    checked by the external guardrails before anything else sees them."""

    llm: LLM
    usage: list[Usage] = field(default_factory=list)

    @classmethod
    def get_serialization_name(cls) -> str | None:
        return None

    def _in(self, text: str) -> str:
        assert self.llm.guard is not None
        return self.llm.guard.sanitize(text, self.llm.notes)

    def _part(self, part: Any) -> Any:
        if isinstance(part, (UserPromptPart, ToolReturnPart, TextPart)) and isinstance(
            part.content, str
        ):
            return dataclasses.replace(part, content=self._in(part.content))
        if isinstance(part, ToolCallPart):
            return dataclasses.replace(part, args=_sanitize_json(_tool_args(part), self._in))
        return part

    def _message(self, message: ModelMessage) -> ModelMessage:
        parts: list[Any] = [self._part(p) for p in message.parts]
        updated: ModelMessage = dataclasses.replace(message, parts=parts)  # type: ignore[arg-type]
        return updated

    async def before_model_request(
        self, ctx: RunContext[Any], request_context: ModelRequestContext
    ) -> ModelRequestContext:
        if self.llm.guard is None:
            return request_context
        messages = [self._message(m) for m in request_context.messages]
        return dataclasses.replace(request_context, messages=messages)

    async def _check(self, text: str) -> str:
        guard = self.llm.guard
        assert guard is not None
        text = guard.sanitize_output(text, self.llm.notes)
        if text and guard.has_external:
            result = await guard.check(text, "output")
            self.llm.notes.extend(result.notes)
            text = result.text
        return text

    async def after_model_request(
        self,
        ctx: RunContext[Any],
        *,
        request_context: ModelRequestContext,
        response: ModelResponse,
    ) -> ModelResponse:
        usage = to_usage(response.usage, self.llm.price)
        self.usage.append(usage)
        record_usage(usage)
        calls = [p for p in response.parts if isinstance(p, ToolCallPart)]
        if response.finish_reason == "length" and not calls:
            raise LLMError("the model's answer was truncated (max_tokens reached)")
        guard = self.llm.guard
        if guard is None:
            return response
        parts: list[ModelResponsePart] = []
        for part in response.parts:
            if isinstance(part, TextPart):
                part = dataclasses.replace(part, content=await self._check(part.content))
            elif isinstance(part, ToolCallPart):
                args = _sanitize_json(
                    _tool_args(part), lambda t: guard.sanitize_output(t, self.llm.notes)
                )
                if guard.has_external and args:
                    # Structured answers are checked as a whole: a guardrail may block them.
                    await guard.check(json.dumps(args, ensure_ascii=False, default=str), "output")
                part = dataclasses.replace(part, args=args)
            parts.append(part)
        return dataclasses.replace(response, parts=parts)


# --------------------------------------------------------------------------- one-shot calls


def instructions(llm: LLM, system: str) -> str:
    if llm.guard is not None and UNTRUSTED_RULE not in system:
        return f"{system}\n\n{UNTRUSTED_RULE}"
    return system


def conversation(messages: str | Sequence[ChatMessage]) -> tuple[str, list[ModelMessage]]:
    """The last user message (the prompt) and the earlier turns as message history."""
    if isinstance(messages, str):
        return messages, []
    items = list(messages)
    last = max((i for i, m in enumerate(items) if m["role"] == "user"), default=-1)
    prompt = items[last]["content"] if last >= 0 else ""
    history: list[ModelMessage] = []
    for i, m in enumerate(items):
        if i == last:
            continue
        if m["role"] == "user":
            history.append(ModelRequest(parts=[UserPromptPart(m["content"])]))
        else:
            history.append(ModelResponse(parts=[TextPart(m["content"])]))
    return prompt, history


def agent_for(
    llm: LLM,
    model: Model,
    system: str,
    output_type: Any,
    *,
    max_tokens: int,
    retries: int = 1,
    deps_type: type[Any] = object,
    tools: Sequence[Any] = (),
    capabilities: Sequence[Any] = (),
) -> Agent[Any, Any]:
    """A pydantic-ai agent for one AutoAir call (usage, guardrails, limits)."""
    agent: Agent[Any, Any] = Agent(
        model,
        output_type=output_type,
        instructions=instructions(llm, system),
        model_settings=cast("ModelSettings", llm.model_settings(max_tokens)),
        retries=retries,
        deps_type=deps_type,
        tools=tools,
        capabilities=[AutoAirCalls(llm), *capabilities],
    )
    return agent


def dict_output(json_schema: Mapping[str, Any]) -> Any:
    """Structured output given as a JSON schema (``dict`` results)."""
    return StructuredDict(dict(json_schema), name="answer")


async def complete(
    llm: LLM,
    system: str,
    messages: str | Sequence[ChatMessage],
    output_type: Any,
    *,
    max_tokens: int,
    retries: int = 1,
) -> Any:
    prompt, history = conversation(messages)
    with llm_errors(llm):
        model = await llm.pydantic_model()
        agent = agent_for(llm, model, system, output_type, max_tokens=max_tokens, retries=retries)
        result = await agent.run(prompt, message_history=history)
    return result.output


async def stream_text(
    llm: LLM, system: str, messages: str | Sequence[ChatMessage], *, max_tokens: int
) -> AsyncIterator[str]:
    prompt, history = conversation(messages)
    scrubber = StreamScrubber(llm.guard, llm.notes) if llm.guard is not None else None
    with llm_errors(llm):
        model = await llm.pydantic_model()
        agent = agent_for(llm, model, system, str, max_tokens=max_tokens)
        async with agent.run_stream(prompt, message_history=history) as result:
            async for delta in result.stream_text(delta=True, debounce_by=None):
                text = scrubber.feed(delta) if scrubber is not None else delta
                if text:
                    yield text
    tail = scrubber.flush() if scrubber is not None else ""
    if tail:
        yield tail


# --------------------------------------------------------------------------- embeddings


class Embedder:
    """Embeddings through pydantic-ai (OpenAI-compatible, Gemini, Ollama, Cohere, Bedrock),
    with the same HTTP client settings as the chat models."""

    def __init__(self, config: ModelConfig, *, transport: Any = None) -> None:
        if config.provider not in PYDANTIC_PROVIDERS:
            raise LLMError(f"provider {config.provider!r} has no embeddings")
        self.config = config
        self.embedding_model = config.embedding_model or DEFAULT_EMBEDDING_MODELS.get(
            config.provider, ""
        )
        if not self.embedding_model:
            raise LLMError("set ai.embedding.model")
        self.transport = transport
        self._embedders: weakref.WeakKeyDictionary[Any, Any] = weakref.WeakKeyDictionary()

    def _embedder(self) -> Any:
        import asyncio

        from pydantic_ai.embeddings import Embedder as PydanticEmbedder
        from pydantic_ai.embeddings import infer_embedding_model

        loop = asyncio.get_running_loop()
        embedder = self._embedders.get(loop)
        if embedder is None:
            name, provider, _ = build_provider(self.config, self.transport)
            try:
                model = infer_embedding_model(
                    f"{name}:{self.embedding_model}", provider_factory=lambda _: provider
                )
            except (ImportError, UserError, ValueError) as exc:
                raise LLMError(f"embeddings: {exc}") from exc
            embedder = self._embedders[loop] = PydanticEmbedder(model)
        return embedder

    async def aembed(self, texts: Sequence[str]) -> list[list[float]]:
        try:
            result = await self._embedder().embed_documents(list(texts))
        except (ModelAPIError, AgentRunError) as exc:
            raise LLMError(f"embeddings: {exc}") from exc
        return [list(v) for v in result.embeddings]

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return run_sync(lambda: self.aembed(texts))


__all__ = [
    "PYDANTIC_PROVIDERS",
    "AutoAirCalls",
    "Embedder",
    "agent_for",
    "build_model",
    "build_provider",
    "complete",
    "conversation",
    "dict_output",
    "http_client",
    "instructions",
    "llm_errors",
    "stream_text",
    "to_usage",
]
