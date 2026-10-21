---
title: Providers and models
description: Configure the LLM provider, models, credentials, per-user models and embeddings for AutoAir's AI features.
sidebar_label: Providers and models
---

AutoAir's models are [pydantic-ai](https://ai.pydantic.dev) models, the same library and connection
format as Airflow's `apache-airflow-providers-common-ai`. The AI is an optional extra: install
`autoair[ai]` plus the extra of each provider you use (they map to `pydantic-ai-slim` extras, which
bring the provider SDK):

```bash
pip install 'autoair[ai,ai-anthropic]'     # or ai-openai, ai-google, ai-ollama, ai-bedrock, ai-groq,
                                           # ai-mistral, ai-cohere; ai-all for every provider
```

Without `autoair[ai]` the AI is off: the admin panel and `/api/meta` (`ai_unavailable_reason`) show
which extra to install, and the AI API (including the rules-only review and leak scan) is not mounted.
A provider whose SDK is missing fails its first call with the extra to install.

| Provider (`ai.provider`) | Extra | API | Default model | Embeddings |
|---|---|---|---|---|
| `anthropic` | `ai-anthropic` | Messages | `claude-sonnet-5-5` | no |
| `openai` | `ai-openai` | Responses (api.openai.com); Chat Completions with `ai.base_url` (vLLM, LM Studio, LiteLLM and other compatible servers) | `gpt-4.1` | `text-embedding-3-small` |
| `azure` | `ai-openai` | Azure OpenAI (`ai.base_url` = resource endpoint, `ai.model` = deployment) | – | no |
| `gemini` | `ai-google` | Gemini API | `gemini-2.5-pro` | `gemini-embedding-001` |
| `google-cloud` | `ai-google` | Vertex AI (Google Cloud credentials) | – | no |
| `ollama` | `ai-ollama` | Ollama's OpenAI-compatible API (`/v1` is appended to `ai.base_url`, default `http://localhost:11434`) | `qwen3:14b` | `nomic-embed-text` |
| `bedrock` | `ai-bedrock` | Bedrock Converse (AWS credentials of the environment) | – | set `ai.embedding.model` |
| `groq`, `mistral`, `cohere` | `ai-groq`, `ai-mistral`, `ai-cohere` | the provider's API | – | `cohere`: `embed-v4.0` |

Providers without a default model need `ai.model`. The provider SDKs handle timeouts
(`ai.timeout_seconds`, default 180 s), retries with backoff that honour `Retry-After`
(`ai.max_retries`, default 3; pydantic-ai versions without the per-request `max_retries` setting keep
the SDK default), streaming, tool calling and structured output; `ai.max_output_tokens` (default 16000)
bounds every call. Errors are mapped to `LLM error: …` (HTTP 502) with credentials redacted.

**Outgoing connections.** Anthropic, OpenAI / Azure / Ollama, Google and Mistral get AutoAir's
`httpx2` client, so the [outbound settings](/docs/reference/configuration#outbound) (CA bundle, client
certificate, proxies; client name `ai`) apply to LLM calls. The Groq and Cohere SDKs still need a
legacy `httpx` client and use their own (the environment's `HTTPS_PROXY` / `SSL_CERT_FILE` apply,
`outbound.*` does not); Bedrock uses boto.

## Central model

In the default `central` mode everyone uses one server-side model.

| Setting | Default | Meaning |
|---|---|---|
| `AUTOAIR_AI__PROVIDER` | inferred | `anthropic`, `openai`, `azure`, `gemini`, `google-cloud`, `ollama`, `bedrock`, `groq`, `mistral` or `cohere` |
| `AUTOAIR_AI__MODEL` | provider default | Model id. A `provider:` prefix also selects the provider (`openai:gpt-4.1`, `google-gla:gemini-2.5-pro`, `ollama:llama3.1:8b`) |
| `AUTOAIR_AI__MODELS` | `[]` | JSON list of additional models users may pick per request (shown in the AI menu's **Model** group) |
| `AUTOAIR_AI__API_KEY` | – | Key of the configured provider (secret) |
| `AUTOAIR_AI__ANTHROPIC_API_KEY` | – | Anthropic key, alternative to `api_key` |
| `AUTOAIR_AI__BASE_URL` | provider default | OpenAI-compatible base URL, Azure OpenAI endpoint, Ollama URL, or a gateway in front of Anthropic/Gemini |
| `AUTOAIR_AI__API_VERSION` | – | Azure OpenAI `api-version` (with `provider=openai` it selects Azure) |
| `AUTOAIR_AI__EXTRA_HEADERS` | `{}` | JSON map of extra HTTP headers for LLM gateways (secret) |
| `AUTOAIR_AI__EFFORT` | – | Reasoning effort (`low`, `medium`, `high`, `xhigh`, `max`; `none` turns thinking off): pydantic-ai's `thinking` setting |
| `AUTOAIR_AI__LLM_CONN_ID` | – | Airflow connection with the model; wins over the variables above |
| `AUTOAIR_AI__PRICING` | `{}` | USD per million tokens per model (prefix match); enables cost reporting and cost budgets |

**Provider inference.** When `ai.provider` is not set, AutoAir uses the `provider:` prefix of
`ai.model`; otherwise `anthropic` if `AUTOAIR_AI__API_KEY` or `AUTOAIR_AI__ANTHROPIC_API_KEY` is set;
otherwise the first of Anthropic, OpenAI and Gemini whose standard environment variable holds a key:

| Provider | Environment variables read for the key |
|---|---|
| `anthropic` | `AUTOAIR_ANTHROPIC_API_KEY`, `ANTHROPIC_API_KEY` |
| `openai` / `azure` | `OPENAI_API_KEY` / `AZURE_OPENAI_API_KEY` |
| `gemini` | `GEMINI_API_KEY`, `GOOGLE_API_KEY` |
| `ollama`, `groq`, `mistral`, `cohere` | `OLLAMA_API_KEY` (optional), `GROQ_API_KEY`, `MISTRAL_API_KEY`, `CO_API_KEY` |

Accepted model prefixes: AutoAir's provider names plus `claude`, `openai-chat`, `openai-responses`,
`openai-compatible`, `vllm`, `lmstudio`, `google`, `google-gla`, `google-vertex`, `vertex`. An unknown
prefix stays part of the model id, so Ollama tags such as `llama3.1:8b` work.

**What happens when it is incomplete.** Anthropic, Gemini, OpenAI and Azure need a key or a
`base_url`; Ollama needs neither. Without a usable configuration no model is built: LLM features answer
503, the rules-only features keep working, and the AI menu shows **Set up AI…**. Provider errors during
a call surface as HTTP 502 (`LLM error: …`); a model not on the allowed list is 422.

**Model choice per request.** Users can switch models in the AI menu when more than one is
selectable. For the central model that list is `ai.models`, or, when empty, the provider's suggestions
(Anthropic: `claude-sonnet-5-5`, `claude-opus-5-5`, `claude-haiku-5-5`).

### One Airflow connection for Airflow and AutoAir

`AUTOAIR_AI__LLM_CONN_ID` reads the model from an Airflow connection in the format of the common.ai
provider (`conn_type` `pydanticai`), so the same connection serves Airflow's LLM operators and AutoAir.
The connection is read lazily on first use (off the event loop), so a missing connection never breaks
start-up; it surfaces as an LLM error on the first call.

| Connection field | Used as |
|---|---|
| `password` | API key |
| `host` (+ `schema` as scheme, `port`) | Base URL; `extra.base_url` also works |
| `extra.model` | Model as `"<provider>:<model>"`: `anthropic:claude-sonnet-5-5`, `openai:gpt-4.1`, `google-gla:gemini-2.5-pro`, `ollama:qwen3` |
| `extra.provider` or `conn_type` | Provider when the model has no prefix. Connection types: `anthropic`, `openai`, `azure_openai`, `google_gemini`, `gemini`, `ollama`, `bedrock` / `aws`, `groq`, `mistral`, `cohere` |
| `extra.api_version` | Azure OpenAI |
| `extra.headers` | Extra HTTP headers |
| `extra.reasoning_effort` (or `effort`) | Reasoning effort |
| `extra.temperature`, `top_p`, `thinking`, `extra_body`, `service_tier` | pydantic-ai model settings |
| `extra.embedding_model` | Embedding model |

```bash
airflow connections add autoair_llm \
  --conn-type pydanticai \
  --conn-password "$ANTHROPIC_API_KEY" \
  --conn-extra '{"model": "anthropic:claude-sonnet-5-5"}'

AUTOAIR_AI__LLM_CONN_ID=autoair_llm
```

This needs AutoAir to read Airflow connections: plugin mode, or standalone with Airflow installed and
access to its metadata database or secrets backend.

## Examples per provider

**Anthropic** (`autoair[ai,ai-anthropic]`)

```bash
AUTOAIR_AI__PROVIDER=anthropic
AUTOAIR_AI__API_KEY=sk-ant-...
AUTOAIR_AI__MODEL=claude-sonnet-5-5
AUTOAIR_AI__MODELS='["claude-opus-5-5","claude-haiku-5-5"]'
AUTOAIR_AI__PRICING='{"claude-sonnet-5-5": {"input": 3, "output": 15, "cache_read": 0.3, "cache_write": 3.75}}'
```

**OpenAI** (`autoair[ai,ai-openai]`)

```bash
AUTOAIR_AI__PROVIDER=openai
OPENAI_API_KEY=sk-...
AUTOAIR_AI__MODEL=gpt-4.1
```

**Azure OpenAI** (`base_url` is the resource endpoint, the model the deployment name):

```bash
AUTOAIR_AI__PROVIDER=azure
AUTOAIR_AI__BASE_URL=https://acme.openai.azure.com
AUTOAIR_AI__MODEL=gpt-4-1
AUTOAIR_AI__API_VERSION=2025-04-01-preview
AUTOAIR_AI__API_KEY=...
```

**Self-hosted OpenAI-compatible server** (vLLM, LM Studio, LiteLLM; no key needed when the server
does not require one):

```bash
AUTOAIR_AI__PROVIDER=openai
AUTOAIR_AI__BASE_URL=http://vllm:8000/v1
AUTOAIR_AI__MODEL=qwen3-32b
```

**Gemini** (`autoair[ai,ai-google]`)

```bash
AUTOAIR_AI__PROVIDER=gemini
GEMINI_API_KEY=...
AUTOAIR_AI__MODEL=gemini-2.5-pro
```

**Bedrock** (`autoair[ai,ai-bedrock]`; AWS credentials and region from the environment)

```bash
AUTOAIR_AI__PROVIDER=bedrock
AUTOAIR_AI__MODEL=<a Bedrock model id>
AWS_DEFAULT_REGION=eu-central-1
```

**Ollama** (`autoair[ai,ai-ollama]`; fully local, combine with Ollama embeddings for an air-gapped
setup):

```bash
AUTOAIR_AI__MODEL=ollama:qwen3:14b
AUTOAIR_AI__BASE_URL=http://ollama:11434
AUTOAIR_AI__EMBEDDING__PROVIDER=ollama
AUTOAIR_AI__EMBEDDING__MODEL=nomic-embed-text
```

The same in YAML:

```yaml
ai:
  provider: ollama
  model: qwen3:14b
  base_url: http://ollama:11434
  embedding:
    provider: ollama
    model: nomic-embed-text
```

## Per-user models

`ai.llm_mode` decides whose model a request uses:

| Mode | Behaviour |
|---|---|
| `central` (default) | Everyone uses the central model. **My AI model…** is hidden. |
| `per_user` | Every user configures their own model under **AI → My AI model…** (shown as **Use my own model…** until one is saved). Until they do, LLM features answer 428 *Configure your AI model first*. |
| `both` | The user's own model when set, else the central one. |

In the **My AI model** dialog a user picks a provider and model, optionally an endpoint, and pastes an
API key, or references an allow-listed Airflow connection instead of a key. **Test** sends one request
with the unsaved form. The API is `GET/PUT/DELETE /api/ai/me/model` and `POST /api/ai/me/model/test`;
it requires a signed-in user (401 otherwise).

Key handling:

- Keys are encrypted at rest with Fernet using `ai.user_llm.encryption_key`. Without it, AutoAir
  creates a key file `ai-user-llm.key` (mode 0600) in the state folder and logs a warning. Set the key
  explicitly when several replicas or the [AI service](/docs/ai/ai-service) share the store:
  `python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'`.
- Keys are never returned (only `api_key_set` and the last four characters) and never logged.
- Settings are stored per user under `ai.user_llm.store_url` (default `<state>/ai-users`), one file
  per user named by the SHA-256 of the user id.

Administrator restrictions (`ai.user_llm.*`):

| Setting | Default | Effect |
|---|---|---|
| `allowed_providers` | `anthropic`, `openai`, `gemini`, `ollama` | Providers users may choose |
| `allowed_models` | `["*"]` | fnmatch patterns of model ids users may choose |
| `allowed_base_urls` | `[]` | URL prefixes users may set as endpoint. Empty = provider default endpoints only (custom endpoints are refused) |
| `allow_private_networks` | `false` | Allow user endpoints that resolve to private, loopback or link-local addresses |
| `allowed_conn_ids` | `[]` | fnmatch patterns of Airflow connections users may reference. Empty = not allowed, because the server reads the connection on the user's behalf |
| `encryption_key` | key file | Fernet key (secret) |
| `store_url` | `<state>/ai-users` | Where user settings live |

A violation is rejected with 400 and a reason (*provider … is not allowed here*, *this endpoint is not
in the allowed list*, *endpoint refused: …*).

```bash
# Users bring their own keys: OpenAI or Anthropic only, through the company gateway
AUTOAIR_AI__LLM_MODE=both
AUTOAIR_AI__USER_LLM__ALLOWED_PROVIDERS='["openai","anthropic"]'
AUTOAIR_AI__USER_LLM__ALLOWED_BASE_URLS='["https://llm-gateway.acme.io/v1"]'
AUTOAIR_AI__USER_LLM__ENCRYPTION_KEY=<fernet key>
```

### Private-network restrictions

User endpoints are untrusted input, so they pass AutoAir's outbound URL policy (SSRF protection):

1. The scheme must be `http` or `https`; credentials in the URL are refused.
2. The URL must start with one of `allowed_base_urls` at a path boundary.
3. Unless `allow_private_networks` is true, every address the host resolves to must be public: not
   loopback, private (RFC 1918 / ULA), link-local, CGNAT (`100.64.0.0/10`), multicast, reserved,
   unspecified, or a cloud metadata address (`169.254.169.254`, `fd00:ec2::254`).
4. The check is repeated at use time and the connection is pinned to the validated IP (with the
   original `Host` header and TLS server name), so DNS rebinding cannot redirect a request to an
   internal address after the check.

The central model (`ai.base_url`) is administrator configuration and is not subject to this policy,
so an internal vLLM or Ollama host works without `allow_private_networks`.

## Embeddings

Retrieval is keyword-only (BM25) by default. Setting an embedding provider switches it to hybrid
retrieval (BM25 + dense vectors, fused with Reciprocal Rank Fusion); see
[Retrieval and research](/docs/ai/research-and-knowledge).

| Setting | Default | Meaning |
|---|---|---|
| `AUTOAIR_AI__EMBEDDING__PROVIDER` | – | `openai`, `gemini`, `ollama`, `cohere` or `bedrock` (pydantic-ai embeddings; Anthropic has no embeddings API) |
| `AUTOAIR_AI__EMBEDDING__MODEL` | provider default | Embedding model id |
| `AUTOAIR_AI__EMBEDDING__BASE_URL` | the chat provider's URL when it is the same provider | Embeddings endpoint |
| `AUTOAIR_AI__EMBEDDING__API_KEY` | the chat key when it is the same provider, else the provider's environment variable | Secret |
| `AUTOAIR_AI__EMBEDDING__BATCH_SIZE` | `64` | Texts per embedding call |
| `AUTOAIR_AI__EMBEDDING__MAX_CHARS` | `1500` | Characters of each chunk that are embedded |

Vectors are cached per text hash in memory and on disk (`<cache>/ai-embeddings.sqlite3`, at most
`ai.retrieval.embedding_cache_max_entries`), so only new or changed chunks are embedded again.

```bash
# Claude for chat, OpenAI embeddings
AUTOAIR_AI__API_KEY=sk-ant-...
AUTOAIR_AI__EMBEDDING__PROVIDER=openai
AUTOAIR_AI__EMBEDDING__API_KEY=sk-...
```

## Related pages

- [`ai` settings](/docs/reference/configuration#ai), [`ai.user_llm`](/docs/reference/configuration#aiuser_llm), [`ai.embedding`](/docs/reference/configuration#aiembedding)
- [Guardrails and AI audit](/docs/ai/guardrails) (cost reporting and budgets)
- [AI features](/docs/ai/introduction)
