"""The LLM handle used by AutoAir's AI features, on top of pydantic-ai.

* :class:`ModelConfig` - provider, model, credentials and endpoint of one model (from
  ``ai.*``, an Airflow connection in the ``pydanticai`` / common.ai format, or a user's
  own settings).
* :class:`LLM` - a configured model plus AutoAir's limits, pricing and (optionally) the
  guardrail pipeline. It offers the small "prose / JSON / typed" surface the features need
  (``complete_text`` / ``complete_json`` / ``complete_structured``, each with an async
  twin, and ``stream_text``); the agents (:mod:`autoair.ai.agent`) run pydantic-ai agents on
  :meth:`LLM.pydantic_model`.

This module is import-light: pydantic-ai is only imported when a model is actually used
(:mod:`autoair.ai.models`), so the rules-only features and the MCP server load without the
``autoair[ai]`` extra.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import os
import threading
import weakref
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Literal, TypedDict, TypeVar

from pydantic import BaseModel, Field, SecretStr

from autoair.ai.config import AiSettings, ai_settings_of
from autoair.ai.usage import ModelPrice, price_for

if TYPE_CHECKING:
    from pydantic_ai.models import Model

    from autoair.ai.guardrails import GuardrailNote, GuardrailPipeline

T = TypeVar("T", bound=BaseModel)
R = TypeVar("R")

DEFAULT_MAX_TOKENS = 16_000


class LLMError(Exception):
    """The model could not produce a usable answer (refusal, truncation, bad output, API
    error). Messages never contain credentials."""


class ChatMessage(TypedDict):
    role: Literal["user", "assistant"]
    content: str


#: Default model per provider (others need ``ai.model``).
DEFAULT_MODELS: dict[str, str] = {
    "anthropic": "claude-sonnet-5-5",
    "openai": "gpt-4.1",
    "gemini": "gemini-2.5-pro",
    "ollama": "qwen3:14b",
}
DEFAULT_MODEL = DEFAULT_MODELS["anthropic"]

#: Models offered in the UI model picker when ``ai.models`` is not set.
SUGGESTED_MODELS: dict[str, list[str]] = {
    "anthropic": ["claude-sonnet-5-5", "claude-opus-5-5", "claude-haiku-5-5"],
}

#: Environment variables consulted for a provider's API key (after AUTOAIR_AI__API_KEY);
#: they also decide the provider when none is configured.
API_KEY_ENV: dict[str, tuple[str, ...]] = {
    "anthropic": ("AUTOAIR_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"),
    "openai": ("OPENAI_API_KEY",),
    "azure": ("AZURE_OPENAI_API_KEY",),
    "gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "ollama": ("OLLAMA_API_KEY",),
    "groq": ("GROQ_API_KEY",),
    "mistral": ("MISTRAL_API_KEY",),
    "cohere": ("CO_API_KEY", "COHERE_API_KEY"),
}

#: ``provider:`` prefixes accepted in model strings (AutoAir, pydantic-ai and Airflow
#: common.ai spellings) -> AutoAir provider name.
PROVIDER_ALIASES: dict[str, str] = {
    "anthropic": "anthropic",
    "claude": "anthropic",
    "openai": "openai",
    "openai-chat": "openai",
    "openai-responses": "openai",
    "openai-compatible": "openai",
    "vllm": "openai",
    "lmstudio": "openai",
    "azure": "azure",
    "gemini": "gemini",
    "google": "gemini",
    "google-gla": "gemini",
    "google-cloud": "google-cloud",
    "google-vertex": "google-cloud",
    "vertex": "google-cloud",
    "ollama": "ollama",
    "bedrock": "bedrock",
    "groq": "groq",
    "mistral": "mistral",
    "cohere": "cohere",
}

#: Install hint per provider (the pydantic-ai-slim extra behind it).
PROVIDER_EXTRAS: dict[str, str] = {
    "anthropic": "ai-anthropic",
    "openai": "ai-openai",
    "azure": "ai-openai",
    "gemini": "ai-google",
    "google-cloud": "ai-google",
    "ollama": "ai-ollama",
    "bedrock": "ai-bedrock",
    "groq": "ai-groq",
    "mistral": "ai-mistral",
    "cohere": "ai-cohere",
}


def split_model(value: str) -> tuple[str | None, str]:
    """``"openai:gpt-4.1"`` -> ``("openai", "gpt-4.1")``; unknown prefixes stay in the model
    (Ollama tags such as ``llama3.1:8b`` contain colons)."""
    prefix, sep, rest = value.partition(":")
    if sep and prefix.lower() in PROVIDER_ALIASES:
        return PROVIDER_ALIASES[prefix.lower()], rest
    return None, value


class ModelConfig(BaseModel):
    """Everything needed to build a model. ``api_key`` is a secret (never printed)."""

    provider: str = "anthropic"
    model: str | None = None
    api_key: SecretStr | None = None
    base_url: str | None = None
    api_version: str | None = None
    extra_headers: dict[str, str] = Field(default_factory=dict)
    timeout: float = Field(default=180, gt=0)
    max_retries: int = Field(default=3, ge=0)
    effort: str | None = None
    #: Further pydantic-ai model settings (e.g. ``anthropic_thinking``, ``extra_body``).
    settings: dict[str, Any] = Field(default_factory=dict)
    embedding_model: str | None = None
    price: ModelPrice | None = None
    #: Resolve + validate the endpoint host on every request and connect to the checked
    #: public IP (untrusted, e.g. per-user, endpoints; see :mod:`autoair.ai.net`).
    pin_public_ip: bool = False

    @property
    def resolved_model(self) -> str:
        return self.model or DEFAULT_MODELS.get(self.provider, "")


# --------------------------------------------------------------------------- the handle

#: Bound of :attr:`LLM._variants` (models picked per request with ``with_model``).
MAX_MODEL_VARIANTS = 16


class _Shared:
    """State shared by an LLM and its guarded copies: the resolved config, the pydantic-ai
    model of each event loop (HTTP clients are bound to their loop) and model variants."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.config: ModelConfig | None = None
        self.models: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, Any] = (
            weakref.WeakKeyDictionary()
        )
        self.clients: list[Any] = []
        self.variants: OrderedDict[str, LLM] = OrderedDict()


class LLM:
    """A model AutoAir can call: configured explicitly, read lazily from an Airflow
    connection (``conn_id``; a missing connection never breaks start-up), or wrapping a
    ready pydantic-ai model (tests: ``FunctionModel`` / ``TestModel``)."""

    def __init__(
        self,
        config: ModelConfig | None = None,
        *,
        conn_id: str | None = None,
        model_name: str | None = None,
        model: Model | None = None,
        max_output_tokens: int = DEFAULT_MAX_TOKENS,
        pricing: Mapping[str, Mapping[str, float]] | None = None,
        defaults: ModelConfig | None = None,
        transport: Any = None,
    ) -> None:
        if config is None and conn_id is None and model is None:
            raise ValueError("an LLM needs a config, an Airflow connection or a model")
        self._config = config
        self.conn_id = conn_id
        #: Model of a connection-based LLM (else the connection's ``extra.model``).
        self._model_override = model_name
        self._defaults = defaults
        self._fixed = model
        self.max_output_tokens = max_output_tokens
        self.pricing = dict(pricing or {})
        #: httpx2 transport for the provider client (tests, pinned endpoints).
        self.transport = transport
        self.guard: GuardrailPipeline | None = None
        self.notes: list[GuardrailNote] = []
        self._shared = _Shared()

    def __repr__(self) -> str:  # never show the key
        return f"LLM(provider={self.provider!r}, model={self.model!r})"

    # -- identity ------------------------------------------------------------------------

    @property
    def model(self) -> str:
        if self._fixed is not None:
            return str(self._fixed.model_name)
        config = self._shared.config or self._config
        if config is not None:
            return config.resolved_model
        return self._model_override or f"(from connection {self.conn_id})"

    @property
    def provider(self) -> str:
        if self._fixed is not None:
            return str(getattr(self._fixed, "system", None) or "test")
        config = self._shared.config or self._config or self._defaults
        return config.provider if config is not None else "anthropic"

    @property
    def config(self) -> ModelConfig | None:
        """The model's config when known without I/O (an Airflow connection not read yet:
        ``None``)."""
        return self._shared.config or self._config

    @property
    def price(self) -> ModelPrice | None:
        config = self._shared.config or self._config
        if config is not None and config.price is not None:
            return config.price
        return price_for(self.model, self.pricing)

    # -- configuration -------------------------------------------------------------------

    def resolve(self) -> ModelConfig | None:
        """The model's config (reads the Airflow connection on first use; blocking).
        ``None`` for a wrapped pydantic-ai model."""
        if self._fixed is not None:
            return None
        shared = self._shared
        if shared.config is None:
            with shared.lock:
                if shared.config is None:
                    shared.config = self._config or self._read_connection()
        return shared.config

    async def aresolve(self) -> ModelConfig | None:
        if self._fixed is not None or self._shared.config is not None or self.conn_id is None:
            return self.resolve()
        # Reading the connection may hit the metadata DB / a secrets backend: off-loop.
        return await asyncio.to_thread(self.resolve)

    def _read_connection(self) -> ModelConfig:
        assert self.conn_id is not None
        from autoair._compat import AirflowConnectionError, get_airflow_connection

        try:
            conn = get_airflow_connection(self.conn_id)
        except AirflowConnectionError as exc:
            raise LLMError(f"cannot load Airflow connection {self.conn_id!r}: {exc}") from exc
        return config_from_connection(conn, model=self._model_override, defaults=self._defaults)

    def _copy(self) -> LLM:
        clone = object.__new__(type(self))
        clone.__dict__.update(self.__dict__)
        return clone

    def with_model(self, model: str | None) -> LLM:
        """The same provider/credentials with another model (cached; never blocks - an
        Airflow connection is still read lazily)."""
        if not model or model == self.model or self._fixed is not None:
            return self
        shared = self._shared
        with shared.lock:
            variant = shared.variants.get(model)
            if variant is None:
                config = self._shared.config or self._config
                variant = LLM(
                    config.model_copy(update={"model": model}) if config is not None else None,
                    conn_id=None if config is not None else self.conn_id,
                    model_name=model,
                    max_output_tokens=self.max_output_tokens,
                    pricing=self.pricing,
                    defaults=self._defaults,
                    transport=self.transport,
                )
                shared.variants[model] = variant
                while len(shared.variants) > MAX_MODEL_VARIANTS:
                    # Not closed: an in-flight request may still use it (GC frees it).
                    shared.variants.popitem(last=False)
            else:
                shared.variants.move_to_end(model)
        if self.guard is not None:
            return variant.guarded(self.guard, self.notes)
        return variant

    def guarded(self, pipeline: GuardrailPipeline, notes: list[GuardrailNote] | None = None) -> LLM:
        """A copy whose calls pass the guardrails (inputs redacted / masked, the untrusted
        data rule in the instructions, answers scrubbed and checked); ``notes`` collects
        what they did."""
        clone = self._copy()
        clone.guard = pipeline
        clone.notes = notes if notes is not None else []
        return clone

    def model_settings(self, max_tokens: int | None = None) -> dict[str, Any]:
        """pydantic-ai ``ModelSettings`` of a call (``max_tokens`` bounded by
        ``ai.max_output_tokens``)."""
        limit = min(max_tokens or self.max_output_tokens, self.max_output_tokens)
        out: dict[str, Any] = {"max_tokens": limit}
        config = self._shared.config or self._config
        if config is None:
            return out
        out["timeout"] = config.timeout
        out["max_retries"] = config.max_retries
        if config.extra_headers:
            out["extra_headers"] = dict(config.extra_headers)
        effort = (config.effort or "").lower()
        if effort in ("none", "off", "false"):
            out["thinking"] = False
        elif effort:
            out["thinking"] = "xhigh" if effort == "max" else effort
        out.update(config.settings)
        return out

    async def pydantic_model(self) -> Model:
        """The pydantic-ai model for the running event loop."""
        if self._fixed is not None:
            return self._fixed
        config = await self.aresolve()
        assert config is not None
        loop = asyncio.get_running_loop()
        shared = self._shared
        model = shared.models.get(loop)
        if model is None:
            from autoair.ai.models import build_model

            model, client = build_model(config, transport=self.transport)
            with shared.lock:
                shared.models[loop] = model
                if client is not None:
                    shared.clients.append(weakref.ref(client))
        return model

    async def aclose(self) -> None:
        """Close the HTTP clients of this model and its variants (errors are ignored)."""
        shared = self._shared
        with shared.lock:
            refs, shared.clients = shared.clients, []
            variants = list(shared.variants.values())
            shared.variants.clear()
            shared.models.clear()
        for ref in refs:
            client = ref()
            if client is not None:
                # another loop's client, or already closed
                with contextlib.suppress(Exception):
                    await client.aclose()
        for variant in variants:
            await variant.aclose()

    # -- calls ---------------------------------------------------------------------------

    async def acomplete_text(
        self, system: str, messages: str | Sequence[ChatMessage], max_tokens: int = 8_000
    ) -> str:
        from autoair.ai.models import complete

        text: str = await complete(self, system, messages, str, max_tokens=max_tokens)
        if not text.strip():
            raise LLMError("the model returned an empty answer")
        return text.strip()

    async def acomplete_json(
        self,
        system: str,
        messages: str | Sequence[ChatMessage],
        json_schema: Mapping[str, Any],
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> dict[str, Any]:
        """A JSON object matching ``json_schema`` (validated by the schema only)."""
        from autoair.ai.models import complete, dict_output

        out: dict[str, Any] = await complete(
            self, system, messages, dict_output(json_schema), max_tokens=max_tokens
        )
        return out

    async def acomplete_structured(
        self,
        system: str,
        messages: str | Sequence[ChatMessage],
        output: type[T],
        *,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        repairs: int = 1,
    ) -> T:
        """An instance of ``output``; validation errors go back to the model up to
        ``repairs`` times (pydantic-ai output retries)."""
        from autoair.ai.models import complete

        out: T = await complete(
            self, system, messages, output, max_tokens=max_tokens, retries=repairs
        )
        return out

    def stream_text(
        self, system: str, messages: str | Sequence[ChatMessage], max_tokens: int = 8_000
    ) -> AsyncIterator[str]:
        """Answer text as it is generated (secrets scrubbed across chunks when guarded)."""
        from autoair.ai.models import stream_text

        return stream_text(self, system, messages, max_tokens=max_tokens)

    def complete_text(
        self, system: str, messages: str | Sequence[ChatMessage], max_tokens: int = 8_000
    ) -> str:
        return run_sync(lambda: self.acomplete_text(system, messages, max_tokens))

    def complete_json(
        self,
        system: str,
        messages: str | Sequence[ChatMessage],
        json_schema: Mapping[str, Any],
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> dict[str, Any]:
        return run_sync(lambda: self.acomplete_json(system, messages, json_schema, max_tokens))

    def complete_structured(
        self,
        system: str,
        messages: str | Sequence[ChatMessage],
        output: type[T],
        *,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        repairs: int = 1,
    ) -> T:
        return run_sync(
            lambda: self.acomplete_structured(
                system, messages, output, max_tokens=max_tokens, repairs=repairs
            )
        )


def run_sync(call: Callable[[], Awaitable[R]]) -> R:
    """Run an async LLM call from synchronous code: on the application's event loop when
    called from one of its worker threads (shared connection pools), else in a new loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError("synchronous LLM call on the event loop: use the async method")
    import anyio.from_thread

    context = contextvars.copy_context()

    async def in_context() -> R:
        # The caller's context variables (e.g. the track_usage meter) apply on the loop too.
        for var, value in context.items():
            var.set(value)
        return await call()

    try:
        return anyio.from_thread.run(in_context)
    except RuntimeError as exc:
        if "worker thread" not in str(exc):
            raise
    return asyncio.run(in_context())


# --------------------------------------------------------------------------- configuration

#: Airflow ``conn_type`` -> provider (``pydanticai`` connections name it in the model).
CONN_TYPES = {
    "anthropic": "anthropic",
    "openai": "openai",
    "azure_openai": "azure",
    "google_gemini": "gemini",
    "gemini": "gemini",
    "ollama": "ollama",
    "bedrock": "bedrock",
    "aws": "bedrock",
    "groq": "groq",
    "mistral": "mistral",
    "cohere": "cohere",
}

#: ``extra`` keys of a connection that become pydantic-ai model settings.
_SETTING_KEYS = ("temperature", "top_p", "thinking", "extra_body", "service_tier")


def config_from_connection(
    conn: Any, *, model: str | None = None, defaults: ModelConfig | None = None
) -> ModelConfig:
    """Model settings from an Airflow connection.

    The format of ``apache-airflow-providers-common-ai`` (``conn_type`` ``pydanticai``):
    ``password`` is the API key, ``host`` the base URL, ``extra.model`` the model as
    ``"<provider>:<model>"`` (``anthropic:claude-...``, ``openai:gpt-...``,
    ``google-gla:gemini-...``, ``ollama:qwen3``), so one connection serves Airflow tasks and
    AutoAir. ``extra.provider`` / the ``conn_type`` may also name the provider;
    ``extra.api_version`` selects Azure OpenAI.
    """
    extra: dict[str, Any] = dict(getattr(conn, "extra_dejson", {}) or {})
    base = defaults or ModelConfig()
    raw_model = model or str(extra.get("model") or "")
    prefix, name = split_model(raw_model) if raw_model else (None, "")
    provider = (
        prefix
        or (
            PROVIDER_ALIASES.get(str(extra["provider"]).lower(), str(extra["provider"]))
            if extra.get("provider")
            else None
        )
        or CONN_TYPES.get(str(getattr(conn, "conn_type", "") or "").lower())
        or base.provider
    )
    if provider not in PROVIDER_EXTRAS:
        raise LLMError(f"connection uses unsupported provider {provider!r}")
    api_key = getattr(conn, "password", None) or None
    host = getattr(conn, "host", None) or None
    if host and not str(host).startswith(("http://", "https://")):
        scheme = str(getattr(conn, "schema", "") or "https")
        port = getattr(conn, "port", None)
        host = f"{scheme}://{host}" + (f":{port}" if port else "")
    raw_headers = extra.get("headers")
    headers: dict[str, Any] = raw_headers if isinstance(raw_headers, dict) else {}
    api_version = extra.get("api_version") or base.api_version
    if provider == "openai" and api_version:
        provider = "azure"
    return base.model_copy(
        update={
            "provider": provider,
            "model": name or (base.model if base.provider == provider else None),
            "api_key": SecretStr(str(api_key)) if api_key else None,
            "base_url": host or extra.get("base_url"),
            "api_version": api_version,
            "extra_headers": {**base.extra_headers, **{str(k): str(v) for k, v in headers.items()}},
            "effort": extra.get("reasoning_effort") or extra.get("effort") or base.effort,
            "embedding_model": extra.get("embedding_model") or base.embedding_model,
            "settings": {**base.settings, **{k: extra[k] for k in _SETTING_KEYS if k in extra}},
        }
    )


def _env_key(name: str) -> str | None:
    return next((os.environ[e] for e in API_KEY_ENV.get(name, ()) if os.environ.get(e)), None)


def provider_config(settings: object | None = None) -> ModelConfig | None:
    """Model settings of the central model (``None`` when nothing is configured).

    The provider is ``ai.provider``, a ``provider:`` prefix of ``ai.model``, or the first
    provider whose API key is set (Anthropic, OpenAI, Gemini). Ollama, Bedrock and key-less
    OpenAI-compatible servers need ``ai.provider`` explicitly.
    """
    return provider_config_from(ai_settings_of(settings))


def provider_config_from(ai: AiSettings) -> ModelConfig | None:
    prefix, model_name = split_model(ai.model) if ai.model else (None, None)
    provider: str | None = ai.provider or prefix
    generic_key = ai.api_key.get_secret_value() if ai.api_key else None
    anthropic_key = ai.anthropic_api_key.get_secret_value() if ai.anthropic_api_key else None

    def key_for(name: str) -> str | None:
        if generic_key:
            return generic_key
        if name == "anthropic" and anthropic_key:
            return anthropic_key
        return _env_key(name)

    if provider is None:
        if generic_key or anthropic_key:
            provider = "anthropic"
        else:
            provider = next((p for p in ("anthropic", "openai", "gemini") if key_for(p)), None)
        if provider is None:
            return None
    api_key = key_for(provider)
    base_url = ai.base_url
    if api_key is None and not base_url and provider in ("anthropic", "gemini", "openai", "azure"):
        return None
    if provider == "openai" and ai.api_version:
        provider = "azure"
    return ModelConfig(
        provider=provider,
        model=model_name,
        api_key=SecretStr(api_key) if api_key else None,
        base_url=base_url,
        api_version=ai.api_version,
        extra_headers=dict(ai.extra_headers),
        timeout=ai.timeout_seconds,
        max_retries=ai.max_retries,
        effort=ai.effort,
    )


def llm_from_config(config: ModelConfig, ai: AiSettings | None = None) -> LLM:
    """An :class:`LLM` for ``config`` priced and bounded by the ``ai`` settings."""
    ai = ai or AiSettings()
    return LLM(config, max_output_tokens=ai.max_output_tokens, pricing=ai.pricing)


def build_llm(settings: object | None = None) -> LLM | None:
    """The central LLM, or ``None`` when AI features are not configured or the
    ``autoair[ai]`` extra is not installed. ``ai.llm_conn_id`` (an Airflow connection) wins
    over ``ai.*`` keys."""
    from autoair.ai.availability import ai_runtime_available

    if not ai_runtime_available():
        return None
    ai = ai_settings_of(settings)
    config = provider_config_from(ai)
    if ai.llm_conn_id:
        return LLM(
            conn_id=ai.llm_conn_id,
            model_name=ai.model,
            defaults=config or ModelConfig(timeout=ai.timeout_seconds, max_retries=ai.max_retries),
            max_output_tokens=ai.max_output_tokens,
            pricing=ai.pricing,
        )
    if config is None:
        return None
    return llm_from_config(config, ai)


def build_embedder(settings: object | None = None) -> Any:
    """Embeddings for retrieval (``ai.embedding.*``) through pydantic-ai, or ``None``."""
    from autoair.ai.availability import ai_runtime_available

    ai = ai_settings_of(settings)
    emb = ai.embedding
    if not emb.provider or not ai_runtime_available():
        return None
    base = provider_config_from(ai)
    same = base is not None and base.provider == emb.provider
    key: SecretStr | None = emb.api_key or (base.api_key if same and base else None)
    if key is None:
        env = _env_key(emb.provider)
        key = SecretStr(env) if env else None
    config = ModelConfig(
        provider=emb.provider,
        model="embeddings",
        api_key=key,
        base_url=emb.base_url or (base.base_url if same and base else None),
        embedding_model=emb.model,
        timeout=min(ai.timeout_seconds, 60),
    )
    from autoair.ai.models import Embedder

    try:
        return Embedder(config)
    except LLMError:
        return None


__all__ = [
    "API_KEY_ENV",
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_MODEL",
    "DEFAULT_MODELS",
    "LLM",
    "PROVIDER_ALIASES",
    "PROVIDER_EXTRAS",
    "SUGGESTED_MODELS",
    "ChatMessage",
    "LLMError",
    "ModelConfig",
    "build_embedder",
    "build_llm",
    "config_from_connection",
    "llm_from_config",
    "provider_config",
    "run_sync",
    "split_model",
]
