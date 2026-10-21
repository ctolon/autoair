"""Streaming, retries, error mapping and embeddings of pydantic-ai models against fake HTTP
servers (httpx2 mock transports, no network)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx2
import pytest
from pydantic import SecretStr

from autoair.ai.config import GuardrailSettings
from autoair.ai.guardrails import GuardrailNote, GuardrailPipeline
from autoair.ai.llm import LLM, LLMError, ModelConfig
from autoair.ai.models import Embedder
from autoair.ai.testing import FakeLLM
from autoair.ai.usage import track_usage

KEY = "sk-secret-key-123456"


def sse(*events: tuple[str | None, Any]) -> bytes:
    out = []
    for event, data in events:
        payload = data if isinstance(data, str) else json.dumps(data)
        out.append((f"event: {event}\n" if event else "") + f"data: {payload}\n\n")
    return "".join(out).encode()


def llm(provider: str, handler: Any, **kw: Any) -> LLM:
    config = ModelConfig(
        provider=provider,
        api_key=SecretStr(KEY),
        max_retries=kw.pop("retries", 0),
        **kw,
    )
    return LLM(config, transport=httpx2.MockTransport(handler))


async def collect(model: LLM, prompt: str = "hi") -> tuple[list[str], int]:
    with track_usage() as meter:
        pieces = [p async for p in model.stream_text("s", prompt, 100)]
    return pieces, meter.total.total_tokens


def openai_chunks(*texts: str, usage: dict[str, int] | None = None) -> bytes:
    chunks: list[Any] = [
        {
            "id": "c",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "gpt-x",
            "choices": [{"index": 0, "delta": {"content": t}, "finish_reason": None}],
        }
        for t in texts
    ]
    chunks.append(
        {
            "id": "c",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "gpt-x",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
    )
    chunks.append(
        {
            "id": "c",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "gpt-x",
            "choices": [],
            "usage": usage or {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
        }
    )
    return sse(*((None, c) for c in chunks), (None, "[DONE]"))


def test_openai_compatible_stream_and_usage() -> None:
    seen: list[Any] = []

    def handler(req: httpx2.Request) -> httpx2.Response:
        seen.append((req.url.path, json.loads(req.content)))
        return httpx2.Response(
            200,
            content=openai_chunks("Hi ", "there"),
            headers={"content-type": "text/event-stream"},
        )

    model = llm("openai", handler, base_url="http://vllm.local/v1", model="gpt-x")
    pieces, tokens = asyncio.run(collect(model))
    assert "".join(pieces) == "Hi there"
    assert tokens == 3
    path, body = seen[0]
    assert path == "/v1/chat/completions"  # OpenAI-compatible: Chat Completions
    assert body["stream"] is True
    assert body["model"] == "gpt-x"


def test_anthropic_stream_text() -> None:
    def handler(req: httpx2.Request) -> httpx2.Response:
        body = sse(
            (
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": "m",
                        "type": "message",
                        "role": "assistant",
                        "model": "claude-x",
                        "content": [],
                        "stop_reason": None,
                        "usage": {"input_tokens": 10, "output_tokens": 0},
                    },
                },
            ),
            (
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
            ),
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "Hel"},
                },
            ),
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "lo"},
                },
            ),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            (
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn"},
                    "usage": {"output_tokens": 7},
                },
            ),
            ("message_stop", {"type": "message_stop"}),
        )
        return httpx2.Response(200, content=body, headers={"content-type": "text/event-stream"})

    pieces, tokens = asyncio.run(collect(llm("anthropic", handler)))
    assert "".join(pieces) == "Hello"
    assert tokens == 17


def test_retries_then_auth_error_is_redacted() -> None:
    calls: list[int] = []

    def handler(req: httpx2.Request) -> httpx2.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx2.Response(
                429, json={"error": {"message": "slow down"}}, headers={"retry-after-ms": "1"}
            )
        return httpx2.Response(401, json={"error": {"message": f"bad key {KEY}"}})

    model = llm("openai", handler, base_url="http://gw.local/v1", model="m", retries=2)
    with pytest.raises(LLMError) as info:
        model.complete_text("s", "q")
    assert len(calls) == 2  # 429 retried by the SDK, 401 not
    assert KEY not in str(info.value)
    assert "authentication failed" in str(info.value)


def test_rate_limit_and_http_errors_are_mapped() -> None:
    def handler(req: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(429, json={"error": {"message": "quota"}})

    with pytest.raises(LLMError, match="rate limited"):
        llm("openai", handler, base_url="http://gw.local/v1", model="m").complete_text("s", "q")

    def bad(req: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(400, json={"error": {"message": "bad request"}})

    with pytest.raises(LLMError, match="HTTP 400"):
        asyncio.run(collect(llm("openai", bad, base_url="http://gw.local/v1", model="m")))


def test_embeddings_through_pydantic_ai() -> None:
    seen: list[Any] = []

    def handler(req: httpx2.Request) -> httpx2.Response:
        seen.append(json.loads(req.content))
        return httpx2.Response(
            200,
            json={
                "object": "list",
                "model": "text-embedding-3-small",
                "data": [{"object": "embedding", "index": 0, "embedding": [1, 2]}],
                "usage": {"prompt_tokens": 1, "total_tokens": 1},
            },
        )

    config = ModelConfig(provider="openai", api_key=SecretStr(KEY), model="embeddings")
    embedder = Embedder(config, transport=httpx2.MockTransport(handler))
    assert embedder.embedding_model == "text-embedding-3-small"
    assert asyncio.run(embedder.aembed(["x"])) == [[1.0, 2.0]]
    assert embedder.embed(["x"]) == [[1.0, 2.0]]  # sync, from a thread without a loop
    assert seen[0]["model"] == "text-embedding-3-small"


def test_fake_llm_streams_and_guarded_stream_scrubs_secrets() -> None:
    fake = FakeLLM(["one two three"])
    pieces, _ = asyncio.run(collect(fake))
    assert len(pieces) > 1
    assert "".join(pieces) == "one two three"

    secret = "sk-ant-" + "Z" * 40
    notes: list[GuardrailNote] = []
    guarded = FakeLLM([f"the key is {secret} - keep it safe"]).guarded(
        GuardrailPipeline(GuardrailSettings()), notes
    )
    text = "".join(asyncio.run(collect(guarded))[0])
    assert secret not in text
    assert "keep it safe" in text
