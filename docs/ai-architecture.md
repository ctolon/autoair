# AI architecture

Reference for contributors and operators. Task-oriented guide on the documentation site: [AI overview](https://ctolon.github.io/autoair/docs/ai/overview), [AI service](https://ctolon.github.io/autoair/docs/ai/ai-service).

How AutoAir's AI backend is built, why, and how to run it. Configuration details are in
[AI providers](https://ctolon.github.io/autoair/docs/ai/providers) (every field: [configuration-reference.md](configuration-reference.md),
section `ai`); the MCP server is described in [mcp.md](mcp.md).

```
 browser ──/api/ai/*──▶ AutoAir server (plugin or `autoair serve`)
                         │  auth + IAM (ai:use) ── in-process AI router ─┐
                         │                    or ── proxy (ai.service_url)┼─▶ AI service (`autoair ai-serve`)
                         │                                               │      same router, own process/pod
                         └─/mcp ── MCP server (in-process, or proxied) ──┘      + /mcp
 AI router ─▶ LLM resolution (central / per-user) ─▶ LLM (+ guardrails) ─▶ pydantic-ai model + provider
           ─▶ pydantic-ai Agent (typed deps, tools, output) ─▶ tools (catalog, validation, Airflow, research, MCP)
           ─▶ writers (validate → repair)                 ─▶ retrieval (BM25 + dense, RRF, rerank)
           ─▶ AI audit log
```

Package map (`src/autoair/ai/`):

| Module | Role |
|---|---|
| `config.py` | `AiSettings` (the `ai` settings section) |
| `availability.py` | Whether the `autoair[ai]` extra (pydantic-ai) is installed, and the install hint |
| `llm.py` | `ModelConfig` and the `LLM` handle (central / connection / per-user model, limits, pricing, guardrails; `complete_text` / `complete_json` / `complete_structured` / `stream_text`); import-light |
| `models.py` | pydantic-ai glue: models and providers from the configuration (httpx2 clients with the outbound settings), the `AutoAirCalls` capability (usage, guardrails), error mapping, embeddings |
| `agent.py`, `tools.py` | The agent runtime (`AgentTask` → pydantic-ai `Agent` with `RunState` deps and the `RunControl` capability) and the tool definitions (`AgentTool`) |
| `testing.py` | `FakeLLM`: a scripted model on pydantic-ai's `FunctionModel` (tests, demos) |
| `events.py` | SSE event model and `stream_job` (cancellation, result/error/done framing) |
| `guardrails.py` | guardrail pipeline, `Guardrail` protocol, `StreamScrubber` |
| `rag.py`, `chunking.py`, `query.py`, `rerank.py`, `corpus.py` | Retrieval |
| `research/` | Air-gapped and online research, and its tools |
| `writers.py` | Specialised code writers with validate-and-repair loops |
| `user_llm.py`, `net.py`, `access.py` | Per-user models (encrypted keys), SSRF policy, caller identity and AI role |
| `knowledge.py`, `prompts.py` | Knowledge base overrides, prompt overrides |
| `audit_log.py` | Audit log of AI calls |
| `service.py`, `proxy.py` | The separate AI service and the main server's proxy |

## 1. Separate service or in-process? Both — in-process by default

| Criterion | In-process (default) | Separate AI service |
|---|---|---|
| Operations | nothing to deploy; works in the Airflow plugin | one more Deployment / container, service-to-service secret |
| Isolation | LLM calls share the API server's event loop and memory | slow or heavy AI work (agent runs, rerank models, research indexing) cannot starve the builder API; crashes stay contained |
| Scaling | scales with the API server | scale AI independently (more replicas for streams, GPU node for a local cross-encoder) |
| Dependencies | optional extras land in the Airflow image | pydantic-ai and the provider SDKs / sentence-transformers / guardrail libraries live only in the AI image; the Airflow image stays lean (a proxy-mode main server needs no `autoair[ai]`) |
| Security | AI credentials in the API server | provider keys and the per-user key store can live only in the AI pod; network policies can restrict egress to that pod |
| Latency | none added | one extra hop (streams pass through unbuffered) |

The plugin case (AutoAir inside Airflow's API server, no extra pods) is the most common and
must work without extra setup, so **in-process is the default**. Setting
`ai.service_url` switches the main server to **proxy mode**; the AI service is the same
code (`autoair.ai.service.create_ai_service`, `autoair ai-serve`), so behaviour is identical
in both modes and there is a single test suite (`tests/integration/test_ai_service.py` runs
the main app proxying to the AI app in-process through `httpx2.ASGITransport`).

**Proxy details** (`autoair/ai/proxy.py`):

* the main server authenticates the caller first (Airflow auth / bearer verifier + IAM
  `ai:use`), then forwards the request with the caller's `Authorization` / Airflow cookie
  (the service still calls Airflow on the user's behalf, so Airflow RBAC applies);
* with `ai.service.shared_secret` the proxy adds an HMAC-signed identity
  (`x-autoair-ai-identity` / `x-autoair-ai-signature`: user, AI role, timestamp, 5-minute
  validity). The service trusts signed requests and verifies *unsigned* bearer tokens
  against Airflow (`ai.service.verify_tokens`), so it can also be called directly (MCP
  clients, automation). A forged or stale signature is rejected (401);
* responses are streamed back unchanged (`aiter_raw`), the upstream request is asked for
  `identity` encoding so the main server's gzip middleware still applies (SSE is never
  compressed); when the client disconnects the upstream request is closed;
* `/mcp` can be proxied too (`ai.service.proxy_mcp`).

The AI service needs the same configuration as the main server (operator catalog, saved
specs, memory, knowledge base, per-user store): same environment variables and shared
state (object-storage URLs or a shared volume). Use `AUTOAIR_CATALOG_FILE` so both build
the identical catalog.

## 2. Async end to end, streaming

* **Models**: every call goes through [pydantic-ai](https://ai.pydantic.dev) (§2a): its
  provider SDK clients are async, retry with backoff (`ai.max_retries`), stream natively
  and report usage; AutoAir hands each provider an `httpx2.AsyncClient` (one per event
  loop) built from `outbound.*`.
* **No event-loop blocking**: model calls, research fetches, query embeddings and rerank
  APIs are awaited; CPU/file work (BM25 index builds, catalog validation, ruff/ShellCheck
  subprocesses, reading Airflow connections) runs in worker threads; sync features that
  predate the async layer run in the thread pool, with context (usage meters) propagated.
* **Agent tools run concurrently** when the model asks for several in one turn.
* **SSE endpoints**: `POST /api/ai/ask/stream`, `/api/ai/explain-code/stream`,
  `/api/ai/write/stream`. Frames are `event: <type>` + JSON `data`:

  | type | payload |
  |---|---|
  | `status` | `stage` (`model`, `research`, `plan`, `write`, `validate`, `repair`, `environment`) + `message` |
  | `token` | `text` (answer tokens as they arrive) |
  | `tool_call` / `tool_result` | id, name, arguments / ok, preview, duration |
  | `guardrail` | guardrail, action (`redacted`, `masked`, `flagged`, `blocked`, `denied`) |
  | `usage` | running token/cost totals |
  | `result` | `kind` (`agent`, `writer`, `explain_code`) + the structured result |
  | `error` | `message`, `status` (502 LLM, 422 guardrail, 504 timeout) |
  | `done` | end of stream |

  Problems detectable up front (not configured, model not allowed, per-user model missing)
  are normal HTTP errors before the stream starts. Keep-alive comments are sent every
  `ai.stream_heartbeat_seconds`; `X-Accel-Buffering: no` disables proxy buffering. Closing
  the connection (the UI's *Cancel*) cancels the run (`events.stream_job`), and the audit
  log records it as `cancelled`.

## 2a. Models, providers and connections (pydantic-ai)

AutoAir's AI is built on **pydantic-ai** (`pydantic-ai-slim`), the library Airflow's
`apache-airflow-providers-common-ai` uses, so both share models, provider names and the
connection format. The AI is an optional extra: `pip install 'autoair[ai]'` plus one
provider extra per model family (they map to pydantic-ai-slim extras):

| AutoAir extra | pydantic-ai-slim extra | `ai.provider` |
|---|---|---|
| `ai-anthropic` | `anthropic` | `anthropic` |
| `ai-openai` | `openai` | `openai` (api.openai.com: Responses API; with `ai.base_url`: any OpenAI-compatible server over Chat Completions - vLLM, LM Studio, gateways), `azure` |
| `ai-google` | `google` | `gemini` (Gemini API), `google-cloud` (Vertex AI) |
| `ai-ollama` | `openai` | `ollama` (Ollama's OpenAI-compatible API; `/v1` is appended to `ai.base_url`) |
| `ai-bedrock`, `ai-groq`, `ai-mistral`, `ai-cohere` | same names | `bedrock`, `groq`, `mistral`, `cohere` |
| `ai-all` | all of the above | |

The floor is `pydantic-ai-slim>=2.43,<3`, compatible with the common.ai provider (which
needs `>=2.0`), so AutoAir installs next to it in the same Airflow environment. Without the
extra the core never imports pydantic-ai: `features.ai` is kept off (the admin panel's
*Features* tab marks it *Not installed* and refuses to switch it on), `/api/meta` reports
`ai_enabled: false` with `ai_unavailable_reason` (the install hint, also shown on the admin
*Overview*), only `GET /api/ai/status` is mounted (it carries
the same `reason`), the MCP tools that need AutoAir's model (`generate_dag_from_prompt`,
`fix_dag_spec`, `write_code`) and every other AI-tagged MCP tool are hidden, and
`autoair ai-serve` exits with the hint; the builder itself is unaffected. A proxy-mode main
server (`ai.service_url`) needs no AI extra itself.

* **Model configuration** (`ModelConfig`): `ai.provider` / `ai.model` (also as
  `"provider:model"`), `ai.api_key` (or the provider's usual variable, e.g.
  `ANTHROPIC_API_KEY`), `ai.base_url`, `ai.api_version` (Azure), `ai.extra_headers`,
  `ai.timeout_seconds`, `ai.max_retries` (the SDK's retries; pydantic-ai versions without the
  `max_retries` model setting keep the SDK default of 2), `ai.effort` (pydantic-ai's
  unified `thinking` setting: `low` … `xhigh`, `max` = `xhigh`, `none` = off) and
  `ai.max_output_tokens` (`max_tokens` bound of every call).
* **Airflow connection** (`ai.llm_conn_id`, or a per-user `conn_id`): the common.ai
  `pydanticai` format - `extra.model` = `"<provider>:<model>"` (`anthropic:claude-…`,
  `openai:gpt-…`, `google-gla:gemini-…`, `ollama:qwen3`), `password` = API key, `host` =
  base URL; `extra.api_version` selects Azure, `extra.headers` adds headers, and
  `temperature` / `thinking` / `extra_body` become model settings. One connection serves
  Airflow's LLM operators and AutoAir. It is read lazily (first use, off the event loop),
  so a missing connection never breaks start-up.
* **HTTP**: anthropic, openai/azure/ollama, google and mistral get AutoAir's
  `httpx2.AsyncClient` (`autoair.net.tls.async_client("ai")`: CA bundle, client
  certificate, proxies of `outbound.*`), pinned to validated public addresses for
  untrusted per-user endpoints (`autoair.ai.net.PinnedAsyncTransport`). The Groq and Cohere
  SDKs still need a legacy `httpx` client, so they use the SDK's own client
  (`outbound.*` does not apply; the environment's proxy and CA variables do); Bedrock talks
  to AWS through boto.
* **Usage and cost**: the `AutoAirCalls` capability (`after_model_request`) turns each
  response's `RequestUsage` into AutoAir's `Usage` (cached tokens reported separately) priced
  with `ai.pricing`, records it in the `track_usage` meters (feature results, audit log,
  thread totals) and turns truncated answers (`finish_reason=length`) into errors.
* **Structured output**: `complete_structured(system, prompt, Model)` uses the model as
  pydantic-ai `output_type` (tool output; validation errors go back to the model as output
  retries), `complete_json` a JSON schema (`StructuredDict`), `stream_text` streams text.
  Synchronous callers (thread-pool features, the MCP server's sync tools) run on the
  application's event loop through `anyio.from_thread`, with their context variables.
* **Embeddings** (`ai.embedding.provider` = `openai` / `gemini` / `ollama` / `cohere` /
  `bedrock`): pydantic-ai's `Embedder` with the same provider and HTTP client setup.
* **Errors**: provider HTTP errors (401/403, 429, …), refusals (content filter), exhausted
  retries and a missing provider SDK (with the extra to install) become `LLMError` (HTTP
  502) with credentials redacted.

## 3. The agent runtime (pydantic-ai `Agent`)

`arun_agent(AgentTask) -> AgentResult` (`agent.py`) runs one pydantic-ai `Agent` per task:

* **Deps**: the run's typed `RunState` (task, recorded steps, per-run tool cache, guardrail
  notes, usage, why the tools were withdrawn).
* **Tools**: `AgentTool`s (`tools.py`: name, description, Pydantic input model, sync or
  async handler; external MCP tools carry the server's JSON schema) become pydantic-ai tools
  (`Tool.from_schema`). Each call is validated with the input model, time-boxed
  (`ai.agent.tool_timeout_seconds`), cached when repeated, truncated
  (`max_tool_output_chars`) and wrapped as untrusted data; failures are reported to the model
  as failed tool results (`ToolFailed`) and never fail the run. pydantic-ai runs the calls of
  one turn concurrently.
* **Output**: `[ToolOutput(final_model, name="final_answer"), str]` - a typed answer
  (`FinalAnswer`, or the assistants' / troubleshooting's own models) or plain text; a
  proposed DagSpec is validated before return.
* **Limits** (`RunControl` capability): when the step budget (`ai.agent.max_steps`, the
  profiles' `max_steps`), the token or cost budget is spent - or only the final-turn grace
  period (`final_turn_grace_seconds`) of the wall-clock budget is left - `prepare_tools`
  withdraws the function tools and a short instruction asks for the answer, so the last turn
  must answer (`stopped` = `answer`, `budget` or `timeout`). `UsageLimits(request_limit)` is
  the hard stop (`stopped=max_steps`); on timeout the completed steps are returned with
  `stopped=timeout`.
* **Events**: an `event_stream_handler` turns text deltas into `token` events (scrubbed
  across chunks by the `StreamScrubber`); tool calls/results, status and usage events come
  from the tool wrapper and the capability.
* **History**: chat turns pass the thread as `message_history` (pydantic-ai messages stored
  with each answer, §13) with a `ProcessHistory` processor that shortens earlier turns' tool
  results.

Features whose context is known up front (review, leak scan, fix, explain, writers) use
single structured calls or a validate-and-repair loop instead of an open agent — cheaper,
faster and deterministic to test.

## 4. Guardrails

Applied by the `AutoAirCalls` capability on every model request of a guarded `LLM`
(`llm.guarded(pipeline, notes)`: every feature) - outgoing prompts, earlier answers and tool
results are redacted / masked in `before_model_request`, answers and structured outputs are
scrubbed and checked by the external guardrails in `after_model_request`, the untrusted-data
rule is added to the instructions - and by the agent runtime and the stream scrubber:

| Guardrail | Where | Setting |
|---|---|---|
| Structured outputs validated with Pydantic, repaired with the validation error | `acomplete_structured`, writers, generator | — |
| Secret redaction of everything sent (leak-scan patterns + site regexes) | outgoing text, tool results, logs | `ai.guardrails.redact_secrets`, `redact_patterns` |
| PII masking (e-mail, phone, Luhn-checked cards, IBAN, SSN, IPv4) | outgoing text | `pii_masking`, `pii_entities` |
| Prompt-injection defence: untrusted content (retrieved docs, tool outputs, task logs, web pages) wrapped in nonce-tagged `<untrusted-data>` blocks the system prompt declares to be data; look-alike closing tags removed; injection phrasing flagged or dropped | runtimes, fix, review, writers, research | `prompt_injection` = `off` / `flag` / `block` |
| Secrets in model answers redacted | answers, JSON outputs, streamed tokens | `block_secret_output` |
| Tool allow-lists per AI role (`viewer`, `editor`, `admin`) | agent tool selection | `tool_allowlist` |
| Steps, token / cost budgets, timeouts | agent runtime | `ai.agent.*` |
| Input size limit | pipeline | `max_input_chars` |
| Output policy: generated code passes syntax, ruff, ShellCheck, import availability, catalog schema / AutoAir validation and the leak rules before it is presented as valid | writers | `check_generated_code` |
| Audit log of every AI call: user, role, feature, provider/model, source (central/user), tokens, cost, tools offered, guardrail actions, duration, outcome | router | `ai.audit.*` (`GET /api/ai/audit`) |

AI roles come from the IAM principal: `admin` with `ai:admin` (knowledge base, audit,
prompts), `editor` with `code:execute` (tools that execute DAG code: deploy dry-run, Python
import), `viewer` otherwise. Using AI at all requires `ai:use`.

**Third-party guardrails** were evaluated:

* *LLM Guard* (scanners for prompt injection, secrets, PII, toxicity; local models) —
  closest to AutoAir's needs; adapter `llm_guard` (`autoair[ai-guardrails]` installs it).
* *NeMo Guardrails* (Colang dialogue rails) — suited to conversational policy, heavy for
  a builder; adapter `nemo:<config dir>` runs a rails config's input/output checks.
* *Guardrails AI* (validator hub) — adapter `guardrails_ai` runs the configured `Guard`.
* Provider-side moderation / Llama Guard — usable through the `module:factory` hook.

All plug in behind the async `Guardrail` protocol (`check(text, direction) ->
GuardrailResult`, raise `GuardrailViolation` to block) via `ai.guardrails.external`; a
missing library logs a warning instead of breaking start-up. The built-in guardrails are
dependency-free and always on, so they also apply to a deployment without extras.

## 5. Retrieval

* **Corpus**: catalog operators (summary, parameters, docstring), the knowledge base
  (§9), saved DAG specs and templates; research adds installed provider docstrings,
  provider metadata, example DAGs and fetched pages.
* **Chunking** (`chunking.py`): Markdown by heading path with fences kept intact, Python by
  syntax tree (module docstring, top-level functions, classes split per method when
  large), plain text paragraph windows with overlap (`ai.retrieval.chunk_size` /
  `chunk_overlap`).
* **Hybrid search**: BM25 (identifier-aware tokens, title boost) + dense embeddings
  (`ai.embedding.*`), fused with **Reciprocal Rank Fusion** (`rrf_k`). Several query
  variants (LLM rewrites) are fused the same way.
* **Query rewriting/expansion**: deterministic Airflow/cloud vocabulary expansion
  (for example `k8s → kubernetes`, `bq → bigquery`, `s3 → aws`; the full table is `SYNONYMS` in
  `autoair/ai/query.py`) at reduced BM25 weight, and optional
  LLM rewrites for research (`llm_query_rewrite`).
* **Reranking** (`rerank.py`, `ai.retrieval.reranker`): `cross_encoder`
  (sentence-transformers, `autoair[ai-rerank]`), `llm` (the configured model scores the
  candidates), or hosted APIs `cohere` / `voyage` / `jina` over httpx2. Failures keep the
  fused order.
* **Metadata filters**: `kind` and any metadata key (exact or any-of), for example
  research restricted to one provider package.
* **Incremental indexing**: sources are re-read after `index_ttl_seconds`; the index is
  rebuilt only when a source's fingerprint changed, and vectors are kept per text hash
  (memory + SQLite cache), so only new or changed chunks are embedded.
* **Caching**: TTL/LRU query-result cache keyed by corpus fingerprint, query-vector cache,
  on-disk embedding cache.
* **Evaluation**: `tests/unit/data/ai_retrieval_eval.json` (20 labelled queries over
  operator docs + the knowledge base) and `tests/unit/test_ai_retrieval_eval.py` assert
  recall@k and MRR for BM25, hybrid and reranked retrieval (BM25: recall@5 1.0, MRR 0.96),
  so ranking changes are measured.

## 6. Research: air-gapped and online

Research is exposed as agent tools (`search_docs`, `read_source`,
`list_installed_packages`, `get_package_info`, `fetch_doc`), as MCP tools
(`search_installed_docs`, `read_source`, `list_installed_packages`, `get_package_info`,
`fetch_doc`) and as `GET /api/ai/research/search`.

**Air-gapped (default, `ai.research.online = false`)** — answers match the *installed*
versions, which is what generated code will run against:

* installed distributions and versions (`importlib.metadata`), Airflow provider metadata
  from each provider's `get_provider_info` entry point (operators, hooks, sensors,
  triggers, connection types);
* docstrings and signatures of provider modules (parsed with `ast`, nothing imported) and
  full example DAGs (`example_dags/`) of distributions matching
  `ai.research.package_patterns`, bounded by `max_index_modules`;
* AutoAir docs and the knowledge base;
* `read_source`: a module, `module.Class`, `Class.method` or a file — confined to
  site-packages, AutoAir's custom operator folders and `extra_source_dirs` (paths are
  resolved, symlink escapes and other suffixes refused).

**Online (`ai.research.online = true`)**: `fetch_doc` and PyPI data in `get_package_info`.
Only `allowed_domains` (default airflow.apache.org, pypi.org, raw.githubusercontent.com;
sub-domains included), every redirect hop re-checked, private/loopback/link-local/metadata
addresses refused (SSRF), per-domain token-bucket rate limit, body size limit, timeouts,
optional proxy, TTL cache on disk; HTML is reduced to readable text and fetched pages
become searchable. Fetched text is untrusted data like every tool output.

The prompts tell the model to search installed docs first, confirm parameters with
`read_source`, and use online docs only for what the installed packages don't explain.

## 7. Writers

`POST /api/ai/write[/stream]` and the MCP tool `write_code`: `task_body`,
`inline_function`, `bash`, `sql`, `operator_config`, `custom_operator`, `dag_spec`.

* Prompts are built from the **installed environment** (`environment_profile`: Airflow and
  Python version, installed providers, importable top-level modules, available linters;
  `GET /api/ai/writers`), the team conventions, retrieved reference docs (untrusted) and —
  for operator configs — candidate operators of the installed catalog with their parameter
  schemas. Prompts can be overridden per site (`ai.prompts.override_dir`).
* **Validate-and-repair**: syntax (`ast`), ruff (pyflakes, syntax, likely bugs, `exec`/`eval`),
  ShellCheck (when installed; `autoair[shell]`), import availability (imports must resolve
  in this environment), SQL checks (sqlglot when installed, else structural checks; no
  `UPDATE`/`DELETE` without `WHERE`), custom-operator structure (subclasses `BaseOperator`,
  `execute(self, context)`, Task SDK import), catalog schema + AutoAir validation for
  operator configs, the leak rules for every code kind. Problems are fed back for up to
  `ai.writers.max_repairs` rounds; the result reports `ok`, remaining issues and the checks
  that ran.

## 8. LLM connection modes

`ai.llm_mode`:

* `central` (default) — one server-side model (`ai.provider`/`ai.model`/keys or an Airflow
  connection); users may pick among `ai.models`.
* `per_user` — every user configures their own model in *AI → My AI model*; AI answers 428
  until they do.
* `both` — the user's model when set, else the central one.

User settings (`GET/PUT/DELETE /api/ai/me/model`, `POST /api/ai/me/model/test`) are stored
per user id (file name = SHA-256 of the id) under `ai.user_llm.store_url`. API keys are
encrypted with Fernet (`cryptography`, a core dependency) using
`ai.user_llm.encryption_key` (set it for multi-replica deployments) or a 0600 key file in
the state folder; they are never returned (only `api_key_set` and the last four
characters) and never logged. Admin restrictions: `allowed_providers`, `allowed_models`
(fnmatch), `allowed_base_urls` (URL prefixes; empty = provider defaults only, endpoints
that resolve to private addresses are refused unless `allow_private_networks`, re-checked
at use time against DNS rebinding) and `allowed_conn_ids` (users may reference an Airflow
connection only when the admin allow-lists it, because the server reads it on their
behalf).

## 9. Knowledge base

Built-in Markdown (`autoair/ai/knowledge/*.md`, plus AutoAir's `docs/` from a source
checkout) can be **disabled** (`ai.knowledge.disabled` or the UI), **replaced** (a file of
the same name in `ai.knowledge.extra_dirs`, or an override uploaded by an admin) and
**reset** (delete the override and UI toggles — back to the configured default). Custom
documents can be added. Overrides live under `ai.knowledge.store_url` (any fsspec URL,
default `<state>/ai-knowledge`). Admin API: `GET /api/ai/knowledge`,
`GET|PUT|DELETE /api/ai/knowledge/{id}`, `POST /api/ai/knowledge/{id}/enabled`; changes
invalidate the retrievers immediately and are recorded in the audit trail.

## 9a. Documentation knowledge base (`autoair.kb`)

A second, much larger knowledge base indexes the official Apache Airflow (core, Task SDK,
providers, Helm chart, airflowctl) and Astronomer documentation. It lives in
`src/autoair/kb/`, has its own settings section (`kb.*`, not `ai.*`) and feature flag
(`features.kb`) because it works without `autoair[ai]`: it never imports pydantic-ai, and
without an embedder search is BM25 only.

* **Pipeline** (`sources.py` -> `fetch.py` -> `parse.py` -> `chunk.py` -> `ingest.py`): sources
  (`official` with `kb.online`, `site` / `llms_txt` mirrors, `git`, `path` incl. object storage
  through `ObjectStoragePath` and a `conn_id`, `bundle`) yield units (one docs package at one
  version) with a fingerprint; unchanged units are skipped, pages are fetched conditionally,
  re-indexed only when their content hash changes and removed only after a complete listing.
  HTML (main content only), rst (with `exampleinclude` resolved in checkouts) and Markdown
  become heading-aware sections and chunks. Version selection follows the installed packages
  (`kb.versions=["installed"]`, `kb.providers=installed`).
* **Stores** (`stores/`): `local` SQLite with FTS5 and float32 vectors (default),
  `pgvector`, `elasticsearch` (REST over httpx2 through `outbound.clients.kb`, no client
  package). The store returns lexical and dense rankings; `service.py` fuses them with RRF,
  applies the `ai.retrieval.reranker`, per-page diversity and `kb.search.max_results`.
* **Replicas**: a lease document `lock-<store>` in the `kb_state` collection serialises ingestion
  per index, `status-<store>` carries progress and errors for every replica, `scheduler.py`
  starts due refreshes (`kb.refresh_interval_hours`).
* **Consumers**: the research tool `search_docs` (scope `docs`, fused with the other scopes, hits
  become citations), the writers' reference material and the review / fix knowledge context
  (wrapped as untrusted data), MCP `kb_search` / `kb_sources` / `kb_status` (also standalone:
  `autoair kb mcp`), `/api/kb/*` (read `ai:use`, manage `ai:admin` or `admin:config`), the admin
  panel and `autoair kb`.
* **Bundles** (`bundle.py`): `autoair kb build` exports parsed pages with a SHA-256-checked
  manifest, optionally Ed25519-signed with the marketplace's scheme; `autoair kb import` (or a
  `bundle` source) loads it air-gapped.

User documentation: [Documentation knowledge base](https://ctolon.github.io/autoair/docs/ai/knowledge-base).

## 10. MCP: in the app, in the AI service, or standalone

The same FastMCP server (including the research and `write_code` tools) can be served:

1. mounted in the main app at `/mcp` (default; same auth as the API);
2. by the AI service at `/mcp` (`ai.service.serve_mcp`), optionally proxied by the main
   server (`ai.service.proxy_mcp`) so clients keep one URL;
3. standalone: `autoair mcp-serve --http` (bearer token verified against Airflow, like
   `autoair serve`) or `autoair mcp-serve` / `autoair mcp` over stdio
   (`AUTOAIR_AIRFLOW_TOKEN`).

## 11. Deployment

Extras: `ai` plus a provider extra (`ai-anthropic`, `ai-openai`, `ai-google`, `ai-ollama`,
`ai-bedrock`, `ai-groq`, `ai-mistral`, `ai-cohere`, `ai-all`; §2a), `ai-rerank`
(sentence-transformers, pulls PyTorch), `ai-guardrails` (LLM Guard); `mcp` (also the
external MCP servers of the assistants), `shell` (ShellCheck) and `format` (ruff) help the
writers. Everything is imported lazily — the plugin and server start without any extra.

**In-process (plugin or standalone)** — add the extras you want to the Airflow image:
```dockerfile
RUN pip install "autoair[git,mcp,format,shell,ai,ai-anthropic]"
```

**Separate AI service** — an image with the AI extras (the Airflow image stays lean):
```dockerfile
FROM apache/airflow:3.3.2   # the same providers as the Airflow image => same catalog
RUN pip install "autoair[standalone,mcp,format,shell,ai,ai-anthropic]"
CMD ["autoair", "ai-serve", "--host", "0.0.0.0", "--port", "8790"]
```
Kubernetes (next to the official Airflow chart):
```yaml
# AI service Deployment. Also give it the api-server's env: the same AUTOAIR_CATALOG_FILE and the same database or store URLs
containers:
  - name: autoair-ai
    image: registry.example.com/autoair-ai:0.1.0
    args: ["autoair", "ai-serve", "--host", "0.0.0.0", "--port", "8790"]
    env:
      - { name: AUTOAIR_AIRFLOW_URL, value: "http://airflow-api-server:8080" }
      - { name: AUTOAIR_AI__SERVICE__SHARED_SECRET, valueFrom: { secretKeyRef: { name: autoair-ai, key: shared-secret } } }
      - { name: AUTOAIR_AI__USER_LLM__ENCRYPTION_KEY, valueFrom: { secretKeyRef: { name: autoair-ai, key: fernet-key } } }
      - { name: AUTOAIR_AI__API_KEY, valueFrom: { secretKeyRef: { name: autoair-ai, key: api-key } } }
      - { name: AUTOAIR_AI__KNOWLEDGE__STORE_URL, value: "s3://acme-airflow/autoair/ai-knowledge" }
      - { name: AUTOAIR_AI__USER_LLM__STORE_URL, value: "s3://acme-airflow/autoair/ai-users" }
      - { name: AUTOAIR_AI__AUDIT__URL, value: "s3://acme-airflow/autoair/ai-audit" }
    readinessProbe: { httpGet: { path: /ready, port: 8790 } }
    livenessProbe: { httpGet: { path: /health, port: 8790 } }
```
and in the Airflow chart values (api-server):
```yaml
env:
  - { name: AUTOAIR_AI__SERVICE_URL, value: "http://autoair-ai:8790" }
  - { name: AUTOAIR_AI__SERVICE__PROXY_MCP, value: "true" }
secret:
  - { envName: AUTOAIR_AI__SERVICE__SHARED_SECRET, secretName: autoair-ai, secretKey: shared-secret }
```
Streams stay open for the length of an agent run: give ingresses a read timeout above
`ai.agent.timeout_seconds` and do not buffer `text/event-stream`.

## 12. Configuration

All AI settings are in the `ai` section (`AUTOAIR_AI__<FIELD>`, nested sections with `__`),
every field has a description and default, secrets are `SecretStr`. Limits such as
retrieval sizes, timeouts, budgets, research limits, writer rounds and prompt overrides
are all settings. `features.ai = false` hides AI (only `GET /api/ai/status`
answers, with `enabled: false`); `features.mcp = false` unmounts `/mcp`.

## 13. Assistants, troubleshooting and the MCP client

* **Chat assistants** (`autoair.ai.assistants.chat`): the developer and admin assistants are
  two profiles of the same agent runtime (`AgentTask` carries the `history` and a custom
  `final_model`, so multi-turn conversations and structured final answers need no second
  agent implementation). Each has its own system prompt, tool allow-list and memory
  scope. Threads persist through the persistence layer (`ai_threads`); every answer stores
  the turn's pydantic-ai messages (tool calls and results included, serialised with
  `ModelMessagesTypeAdapter`; never returned by the API), which the next turn replays as
  message history. Long threads are compacted into a rolling summary. Tools are wrapped by a `SourceCollector` that numbers
  every source (`cite`) for `[n]` citations and records the tools that ran for the audit
  trail (`AiAuditRecord.sources` / `detail`). Proposals are validated server-side and only
  ever applied in the editor after an explicit approval; going live uses the normal deploy
  pipeline. See [ai-assistants.md](ai-assistants.md).
* **Troubleshooting** (`autoair.ai.assistants.troubleshoot`): a deterministic evidence pack
  (logs of every try with line numbers, states, spec + code, versions) followed by an
  agent with tools restricted to the effective scope (server settings ∧ the user's choice),
  a sandboxed read-only filesystem reader, and external MCP tools. The structured report
  (hypotheses with evidence and confidence, remediation, spec patches → validated proposed
  spec + code diff, config snippets, Airflow instructions) is stored (`ai_troubleshoot`);
  approve/reject/applied are separate, audited calls. See
  [ai-troubleshooting.md](ai-troubleshooting.md).
* **MCP client** (`autoair.ai.assistants.mcp_client`): external MCP tools become
  `AgentTool`s with the server's JSON schema (`AgentTool.schema`) for the duration of a run;
  the connection (fastmcp `Client`, `autoair[mcp]`) is entered and closed in the run's task
  (anyio scopes). pydantic-ai's MCP toolsets were considered: across the supported
  pydantic-ai range their API moved (`MCPServer*` classes → `MCPToolset` over fastmcp), and
  AutoAir's per-tool scoping, read-only filtering, citations and audit already work on
  `AgentTool`s, so the fastmcp client stays.
* **Prompts**: `ai.prompts.replace` / `append` / `variables` (`${name}`) and admin-managed
  project rules (`ai_rules`) appended to every authoring prompt.

## 14. Future work

* Human-in-the-loop approval of tool calls (pydantic-ai deferred tools).
* `outbound.*` for the Groq and Cohere providers once their SDKs accept an `httpx2` client.
* AI service: resolve the IAM principal itself for unsigned direct calls (unsigned bearer
  calls get the auth backend's flags, i.e. standalone semantics).
* Vector index persistence beyond the embedding cache (ANN index for very large corpora),
  and an evaluation set for research over provider docs.
* Token-level streaming for structured outputs (writers stream stage progress only).
* Online research: provider docs search via the docs sites' search indexes; signed fetch
  cache for multi-replica sharing.
