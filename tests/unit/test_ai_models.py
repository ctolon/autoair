"""Models and providers built from AutoAir configuration (pydantic-ai), the common.ai
``pydanticai`` connection format, usage / pricing and the error mapping."""

from __future__ import annotations

import asyncio
import importlib.util
import json
from types import SimpleNamespace
from typing import Any

import httpx2
import pytest
from pydantic import SecretStr
from pydantic_ai.usage import RequestUsage

from autoair.ai import models
from autoair.ai.config import AiSettings, EmbeddingSettings
from autoair.ai.llm import (
    LLM,
    LLMError,
    ModelConfig,
    build_embedder,
    build_llm,
    config_from_connection,
    provider_config,
    split_model,
)
from autoair.ai.usage import Usage, price_for, track_usage

KEY = SecretStr("sk-test-1234567890")


def _model(provider: str, **kw: Any) -> Any:
    model, client = models.build_model(ModelConfig(provider=provider, api_key=KEY, **kw))
    return model, client


def test_build_model_per_provider() -> None:
    anthropic, client = _model("anthropic")
    assert type(anthropic).__name__ == "AnthropicModel"
    assert anthropic.model_name == "claude-sonnet-5-5"
    assert isinstance(client, httpx2.AsyncClient)

    openai, _ = _model("openai", model="gpt-4.1")
    assert type(openai).__name__ == "OpenAIResponsesModel"
    compatible, _ = _model("openai", model="qwen", base_url="http://vllm:8000/v1")
    assert type(compatible).__name__ == "OpenAIChatModel"  # Chat Completions servers
    assert str(compatible.base_url).startswith("http://vllm:8000/v1")

    azure, _ = _model(
        "azure", model="gpt-4.1", base_url="https://res.openai.azure.com", api_version="2025-01-01"
    )
    assert "OpenAI" in type(azure).__name__

    ollama, _ = models.build_model(
        ModelConfig(provider="ollama", model="qwen3", base_url="http://ollama:11434")
    )
    assert str(ollama.base_url).rstrip("/") == "http://ollama:11434/v1"  # OpenAI-compatible API


def test_build_gemini_model() -> None:
    pytest.importorskip("google.genai")
    gemini, client = _model("gemini")
    assert type(gemini).__name__ == "GoogleModel"
    assert gemini.model_name == "gemini-2.5-pro"
    assert isinstance(client, httpx2.AsyncClient)


def test_new_providers_need_a_model_and_their_extra() -> None:
    with pytest.raises(LLMError, match=r"set ai\.model"):
        _model("mistral")
    with pytest.raises(LLMError, match="unknown AI provider"):
        _model("nope", model="x")
    if importlib.util.find_spec("groq") is None:
        with pytest.raises(LLMError, match=r"autoair\[ai,ai-groq\]"):
            _model("groq", model="llama-3.3-70b")
    if importlib.util.find_spec("boto3") is not None:
        bedrock, client = _model("bedrock", model="us.anthropic.claude-x")
        assert type(bedrock).__name__ == "BedrockConverseModel"
        assert client is None  # boto, no httpx2 client


def test_provider_client_uses_outbound_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    from autoair.ai.net import PinnedAsyncTransport
    from autoair.net import tls

    calls: list[tuple[str, Any]] = []
    real = tls.async_client

    def spy(client: str, **kwargs: Any) -> httpx2.AsyncClient:
        calls.append((client, kwargs.get("transport")))
        return real(client, **kwargs)

    monkeypatch.setattr(tls, "async_client", spy)
    _model("anthropic", extra_headers={"X-Gateway": "1"})
    _model("openai", model="m", base_url="https://llm.example.com/v1", pin_public_ip=True)
    assert [c for c, _ in calls] == ["ai", "ai"]
    assert calls[0][1] is None
    assert isinstance(calls[1][1], PinnedAsyncTransport)


def test_model_settings_effort_retries_and_limits() -> None:
    llm = LLM(
        ModelConfig(provider="anthropic", api_key=KEY, effort="max", max_retries=1, timeout=30),
        max_output_tokens=4000,
    )
    settings = llm.model_settings(10_000)
    assert settings["max_tokens"] == 4000
    assert settings["thinking"] == "xhigh"
    assert (settings["max_retries"], settings["timeout"]) == (1, 30)
    off = LLM(ModelConfig(provider="anthropic", api_key=KEY, effort="none")).model_settings()
    assert off["thinking"] is False


def test_request_against_openai_compatible_server_with_usage_and_pricing() -> None:
    bodies: list[Any] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        bodies.append(json.loads(request.content))
        return httpx2.Response(
            200,
            json={
                "id": "c",
                "object": "chat.completion",
                "created": 1,
                "model": "qwen",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "hello"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1000,
                    "completion_tokens": 100,
                    "total_tokens": 1100,
                    "prompt_tokens_details": {"cached_tokens": 200},
                },
            },
        )

    llm = LLM(
        ModelConfig(provider="openai", model="qwen", base_url="http://vllm/v1", api_key=KEY),
        pricing={"qwen": {"input": 3, "output": 15, "cache_read": 0.3}},
        transport=httpx2.MockTransport(handler),
    )
    with track_usage() as meter:
        assert llm.complete_text("sys", "hi") == "hello"
    usage = meter.total
    assert (usage.requests, usage.input_tokens, usage.cache_read_tokens) == (1, 800, 200)
    assert usage.cost_usd == pytest.approx((800 * 3 + 100 * 15 + 200 * 0.3) / 1_000_000)
    assert bodies[0]["messages"][0] == {"role": "system", "content": "sys"}


def test_to_usage_excludes_cached_input() -> None:
    usage = models.to_usage(
        RequestUsage(
            input_tokens=100, output_tokens=5, cache_read_tokens=30, cache_write_tokens=10
        ),
        price_for("m", {"m": {"input": 1, "output": 2}}),
    )
    assert (usage.input_tokens, usage.cache_read_tokens, usage.cache_write_tokens) == (60, 30, 10)
    assert usage.total_tokens == 105
    assert usage.cost_usd == pytest.approx((60 + 30 + 10 + 5 * 2) / 1_000_000)
    assert (Usage() + Usage()).cost_usd is None


def test_missing_sdk_is_an_install_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(name: str) -> Any:
        raise ImportError("Please install `anthropic`")

    monkeypatch.setattr(models, "infer_provider_class", broken)
    with pytest.raises(LLMError, match=r"pip install 'autoair\[ai,ai-anthropic\]'"):
        _model("anthropic")


# --------------------------------------------------------------------------- configuration


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "AZURE_OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "OLLAMA_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


def _ai(**fields: Any) -> SimpleNamespace:
    return SimpleNamespace(ai=AiSettings(**fields))


def test_provider_config_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    assert provider_config(SimpleNamespace()) is None
    cfg = provider_config(_ai(model="ollama:llama3.1:8b"))
    assert cfg is not None
    assert (cfg.provider, cfg.model) == ("ollama", "llama3.1:8b")
    assert provider_config(_ai(provider="openai")) is None
    cfg = provider_config(_ai(provider="openai", base_url="http://vllm:8000/v1"))
    assert cfg is not None
    assert cfg.base_url == "http://vllm:8000/v1"
    azure = provider_config(
        _ai(provider="openai", api_key=SecretStr("k"), api_version="2025-01-01")
    )
    assert azure is not None
    assert azure.provider == "azure"
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    cfg = provider_config(SimpleNamespace())
    assert cfg is not None
    assert cfg.provider == "gemini"
    assert cfg.resolved_model == "gemini-2.5-pro"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a-key")
    cfg = provider_config(_ai(effort="none"))
    assert cfg is not None
    assert cfg.provider == "anthropic"
    llm = build_llm(_ai(max_output_tokens=4000))
    assert isinstance(llm, LLM)
    assert llm.model == "claude-sonnet-5-5"
    assert llm.max_output_tokens == 4000
    assert llm.with_model("claude-haiku-5-5").model == "claude-haiku-5-5"
    assert llm.with_model("claude-haiku-5-5") is llm.with_model("claude-haiku-5-5")


def test_split_model() -> None:
    assert split_model("openai:gpt-4.1") == ("openai", "gpt-4.1")
    assert split_model("google-gla:gemini-2.5-pro") == ("gemini", "gemini-2.5-pro")
    assert split_model("google-vertex:gemini-2.5-pro") == ("google-cloud", "gemini-2.5-pro")
    assert split_model("llama3.1:8b") == (None, "llama3.1:8b")


def test_common_ai_pydanticai_connection() -> None:
    """One connection serves Airflow's common.ai tasks and AutoAir."""
    conn = SimpleNamespace(
        conn_type="pydanticai",
        password="sk-ant-x",
        host=None,
        extra_dejson={"model": "anthropic:claude-haiku-5-5"},
    )
    cfg = config_from_connection(conn)
    assert (cfg.provider, cfg.model) == ("anthropic", "claude-haiku-5-5")
    assert cfg.api_key is not None
    assert cfg.api_key.get_secret_value() == "sk-ant-x"
    gemini = config_from_connection(
        SimpleNamespace(
            conn_type="pydanticai",
            password="g",
            host="https://gateway.example.com",
            extra_dejson={"model": "google-gla:gemini-2.5-flash"},
        )
    )
    assert (gemini.provider, gemini.model, gemini.base_url) == (
        "gemini",
        "gemini-2.5-flash",
        "https://gateway.example.com",
    )


def test_config_from_connection_variants() -> None:
    conn = SimpleNamespace(
        conn_type="openai",
        password="sk-c",
        host="https://res.openai.azure.com",
        extra_dejson={"model": "gpt-4.1-mini", "api_version": "2025-01-01", "headers": {"X": "1"}},
    )
    cfg = config_from_connection(conn)
    assert (cfg.provider, cfg.model, cfg.api_version) == ("azure", "gpt-4.1-mini", "2025-01-01")
    assert cfg.extra_headers == {"X": "1"}
    ollama = config_from_connection(
        SimpleNamespace(
            conn_type="generic",
            password=None,
            host="ollama.internal",
            schema="http",
            port=11434,
            extra_dejson={"model": "ollama:qwen3", "temperature": 0.2},
        )
    )
    assert ollama.provider == "ollama"
    assert ollama.base_url == "http://ollama.internal:11434"
    assert ollama.settings == {"temperature": 0.2}
    with pytest.raises(LLMError, match="unsupported provider"):
        config_from_connection(SimpleNamespace(extra_dejson={"provider": "bogus"}))


def test_airflow_connection_llm_gemini(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("airflow.sdk")
    monkeypatch.setenv(
        "AIRFLOW_CONN_MY_GEMINI",
        json.dumps(
            {
                "conn_type": "google_gemini",
                "password": "g",
                "extra": json.dumps({"model": "gemini-2.5-flash"}),
            }
        ),
    )
    llm = LLM(conn_id="my_gemini")
    assert llm.provider == "anthropic"  # unresolved default
    resolved = asyncio.run(llm.aresolve())
    assert resolved is not None
    assert (resolved.provider, resolved.model) == ("gemini", "gemini-2.5-flash")
    assert llm.provider == "gemini"


def test_build_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_env(monkeypatch)
    assert build_embedder(SimpleNamespace()) is None
    embedder = build_embedder(
        _ai(embedding=EmbeddingSettings(provider="ollama", model="nomic-embed-text"))
    )
    assert embedder is not None
    assert embedder.embedding_model == "nomic-embed-text"
