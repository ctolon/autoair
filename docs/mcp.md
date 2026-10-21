# AI and the MCP server

Reference for contributors and operators. Task-oriented guide on the documentation site: [MCP](https://ctolon.github.io/autoair/docs/ai/mcp), [AI features](https://ctolon.github.io/autoair/docs/ai/introduction).

AutoAir exposes DAG *authoring* to AI agents over the Model Context Protocol and has its own
AI features in the editor. Other Airflow MCP servers cover operations; AutoAir's covers
building, validating, reviewing and deploying DAGs.

## MCP server (FastMCP)

Built on [FastMCP](https://gofastmcp.com) 4.x (`pip install 'autoair[mcp]'`), which runs on the
official `mcp` SDK 2.x. FastMCP 4 needs Starlette >= 1.0.1, which Airflow 3.1's FastAPI pin
excludes: next to Airflow 3.1 the extra cannot be installed, `features.mcp` is reported
unavailable (`autoair/mcp/availability.py`, `Settings.feature_unavailable`) and the server runs
in a separate environment (`autoair mcp-serve --http`, or the AI service with
`ai.service.proxy_mcp`). Every tool has typed Pydantic inputs and outputs (`outputSchema`),
an LLM-oriented description that says *when* to use it, MCP annotations
(`readOnlyHint` / `destructiveHint` / `idempotentHint` / `openWorldHint`) and size limits
(paging with `offset` → `next_offset`, log tails, truncated code).

| Group | Tools |
|---|---|
| Catalog | `search_knowledge` (ranked search over operators, guides, saved DAGs, templates), `list_operators`, `get_operator_schema`, `get_dagspec_schema`, `list_node_kinds`, `list_notifiers` |
| Specs | `validate_dag_spec`, `apply_spec_patch` (JSON-patch edits addressed by node id), `generate_code`, `list_dags` (filter `document_type`: `spec`/`raw`), `get_dag_spec`, `get_dag_code` (the DAG file and its `document_type`), `import_python_dag`, `save_dag_spec` (confirm), `save_raw_dag_code` (confirm; raw code DAGs, needs `code:raw`), `delete_dag_spec` (confirm) |
| Analysis | `review_dag_spec`, `scan_dag_leaks`, `optimize_dag_parameters` (from run history) |
| Airflow | `list_connections`, `list_variables`, `get_dag_status`, `list_dag_runs`, `get_task_log`, `trigger_dag` (confirm) |
| Research | `search_installed_docs` (offline search over the installed providers' docstrings, metadata and example DAGs, the knowledge base and fetched pages), `read_source` (installed module / class / method source, confined to site-packages and configured folders), `list_installed_packages`, `get_package_info` (+ PyPI when online), `fetch_doc` (allow-listed sites, online mode only) |
| Server-side LLM | `generate_dag_from_prompt`, `fix_dag_spec`, `write_code` (task bodies, inline functions, Bash, SQL, operator configs, custom operators, DAG specs — validated and repaired; need AutoAir's own LLM; the analysis tools take `use_ai: true` for an extra LLM pass) |
| Operator templates | `list_operator_templates`, `get_operator_template` (a version and the history), `save_operator_template` (confirm; creates a template or publishes the next version, needs the `etag` as `if_match` for an existing id; the task is validated first and errors refuse the save), `delete_operator_template` (confirm; deletes every version). `builder:use` to read, `template:write` to write, all need `features.templates` |
| Memory | `list_memories`, `remember`, `forget` |
| Knowledge base | `kb_search` (the indexed Airflow / provider / Astronomer documentation, hits with URL, version and source), `kb_sources`, `kb_status`; `ai:use` and `features.kb`, no LLM needed. `autoair kb mcp` serves only these three over stdio |

**Resources**: `autoair://schema/dagspec`, `autoair://operators` (index),
`autoair://operators/{fqn}/schema`, `autoair://dags`, `autoair://dags/{dag_id}/spec`,
`autoair://templates`, `autoair://templates/{id}`, `autoair://docs`, `autoair://docs/{name}`
(the enabled knowledge-base documents: the built-in `dagspec-authoring` and
`airflow3-best-practices`, files in `ai.knowledge.extra_dirs` and admin overrides — see [ai-architecture.md](ai-architecture.md#9-knowledge-base)),
`autoair://memory`.

**Prompts**: `build_dag_from_description`, `review_dag`, `fix_import_error`,
`optimize_parameters`, `scan_for_leaks`, `explain_dag`, `convert_existing_dag`.

### Safety
* `save_dag_spec`, `save_raw_dag_code`, `delete_dag_spec` and `trigger_dag` only act with `confirm=true`; without it
  they dry-run and describe what would happen. `AUTOAIR_AI__MCP__ALLOW_WRITES=false` refuses
  them entirely (read-only server).
* Over HTTP the endpoint sits behind the same auth as the API (Airflow's auth manager in plugin
  mode, the bearer verifier standalone); the caller's credentials are forwarded to Airflow, so
  Airflow RBAC applies to every Airflow tool. Connection/variable values and run `conf` are never
  returned.
* Git tokens: tools never take one as an argument (it would reach the model's context, agent
  transcripts and tool-call logs). With `AUTOAIR_STORAGE__GIT__MCP_REQUEST_TOKEN=true` (default
  `false`), `save_dag_spec`, `save_raw_dag_code` and `delete_dag_spec` over HTTP use the user's token from the
  `X-AutoAir-Git-Token` header of the MCP request, configured in the client's server entry next
  to `Authorization` (e.g. `"headers": {"X-AutoAir-Git-Token": "${GIT_TOKEN}"}`); the REST rules
  apply (`storage.git.request_token` off/optional/required, the forced connection wins, HTTPS or
  loopback only) and the token is masked in tool errors, results, logs and audit records
  (`via: mcp`, `credential: request-token`). With the flag off such a header is refused; with
  `request_token=required`, writes without it are refused (dry runs still answer). Over stdio
  there is no header. There is no MCP promote tool. See
  [configuration](configuration.md#git-tokens-typed-per-deploy-storagegitrequest_token).
* `import_python_dag` executes the file in an isolated subprocess without AutoAir's secrets and
  requires `AUTOAIR_ALLOW_PYTHON_CODE=true`. Unexpected errors are masked
  (`mask_error_details`).
* Raw code DAGs are arbitrary Python: `save_raw_dag_code`, and `save_dag_spec` with a spec that
  sets `raw_code`, need the `code:raw` permission and the `python_code` and `code_editor`
  features; `save_raw_dag_code` never replaces a visual (spec-backed) DAG.
* Research tools only read installed sources and configured folders; `fetch_doc` only
  exists when online research is enabled and is limited to allow-listed domains with SSRF,
  size and rate limits. Fetched text is treated as untrusted data.

### Memory
A small persistent store (`AUTOAIR_AI__MEMORY_URL`, any fsspec URL) of conventions and preferences
with three scopes: `global`, `team` (`AUTOAIR_TEAM`) and `user` (the JWT subject over HTTP,
`AUTOAIR_MCP_USER` or the OS user on stdio). Text that looks like a credential is rejected.
Memories are also injected (bounded) into the prompts of AutoAir's own AI features, so a
convention such as "warehouse loads use `snowflake_prod`" applies in the editor and in every
MCP client.

### Connecting
Claude Code / Desktop (stdio):
```bash
claude mcp add autoair -e AIRFLOW_HOME=$AIRFLOW_HOME -e AUTOAIR_AIRFLOW_TOKEN=<jwt> -- autoair mcp
```
Remote (inside Airflow, streamable HTTP, Airflow auth):
```bash
claude mcp add --transport http autoair https://airflow.example.com/autoair/mcp/ \
  --header "Authorization: Bearer <airflow jwt>"
```
### Where MCP runs
The same server can run in three places, with the same auth in each:

| Mode | How | Auth |
|---|---|---|
| In the AutoAir app (default) | `/mcp` next to `/api` (plugin: `…/autoair/mcp/`) | the API's auth (Airflow auth manager / bearer verifier) |
| In the separate AI service | `autoair ai-serve` serves `/mcp` (`AUTOAIR_AI__SERVICE__SERVE_MCP`); with `AUTOAIR_AI__SERVICE__PROXY_MCP=true` the main server proxies its `/mcp` there, so clients keep one URL | main server auth, then signed identity / bearer verification in the service |
| Standalone | `autoair mcp-serve --http --port 8766` (streamable HTTP at `/mcp/`), or stdio: `autoair mcp-serve` / `autoair mcp` / `autoair-mcp` | HTTP: the caller's Airflow bearer token verified against Airflow (`--no-auth` for local experiments); stdio: `AUTOAIR_AIRFLOW_TOKEN` |

`autoair mcp --http` / `autoair-mcp --http` serve HTTP without authentication, for local use only.

The in-app endpoint runs FastMCP's low-level server with a per-request, stateless session
manager: Airflow mounts plugin apps as sub-applications whose ASGI lifespans never run, so
FastMCP's lifespan-managed `http_app()` cannot be used there.

## AI features in the editor

`/api/ai/*` endpoints. None of them saves a DAG spec; the UI shows proposals as a diff before
they are applied.

| Endpoint | Feature |
|---|---|
| `POST /ai/generate` | DAG from a prompt, or edit the current DAG (`existing`); validated and repaired in a loop |
| `POST /ai/explain-dag`, `/ai/explain-code`, `/ai/explain` | Structured DAG walkthrough (steps link to nodes), code/command/SQL explanation, validation problems in plain language |
| `POST /ai/task-code` | Body of a `python_task` from a description (syntax-checked, scanned for leaks) |
| `POST /ai/fix` | Root cause + patches from validation problems, import errors or a failed task's log (fetched from Airflow by `run_id`/`task_id`) |
| `POST /ai/review` | Best practices, Airflow 3 idioms, idempotency, retries, deadlines (rules only: `POST /review/rules`, no AI needed) |
| `POST /ai/leaks` | Hard-coded secrets, credential exposure (logs, rendered templates, XCom, command lines), unbounded resources, missing timeouts (rules only: `POST /lint/secrets`, no AI needed) |
| `POST /ai/optimize` | Retries, timeouts, deferrable, concurrency from Airflow run history (p50/p95, flaky tasks) |
| `POST /ai/apply` | Apply selected findings' patches and validate |
| `POST /ai/describe` | `doc_md` |
| `POST /ai/ask`, `/ai/ask/stream` | Open question answered by the tool-using agent (a pydantic-ai `Agent`); the stream sends tokens, tool calls and the final result as Server-Sent Events |
| `POST /ai/explain-code/stream` | Token-streamed code explanation |
| `POST /ai/write`, `/ai/write/stream` | Specialised writers (task bodies, inline functions, Bash, SQL, operator configs, custom operators, DAG specs) built for the installed environment, with validate-and-repair; `GET /ai/writers` shows the environment profile |
| `GET /ai/search`, `/ai/research/search` | Hybrid retrieval; research over installed provider docs / examples |
| `GET/PUT/DELETE /ai/me/model`, `POST /ai/me/model/test` | The caller's own model (`ai.llm_mode` `per_user` / `both`); keys are encrypted, never returned |
| `GET /ai/knowledge`, `GET/PUT/DELETE /ai/knowledge/{doc_id}`, `POST /ai/knowledge/{doc_id}/enabled`, `GET /ai/audit`, `GET /ai/prompts` | Admin: knowledge base overrides, AI audit log, prompt overrides |
| `GET /ai/assistant/info`, `POST /ai/assistant/chat/stream`, `GET /ai/assistant/threads`, `GET/PATCH/DELETE /ai/assistant/threads/{thread_id}`, `POST /ai/assistant/threads/{thread_id}/proposals/{proposal_id}/decision` | The developer and admin chat assistants with persisted threads, citations, follow-ups and reviewable proposals ([ai-assistants.md](ai-assistants.md)) |
| `GET /ai/troubleshoot/options`, `POST /ai/troubleshoot/stream`, `GET /ai/troubleshoot/analyses`, `GET /ai/troubleshoot/analyses/{analysis_id}`, `POST /ai/troubleshoot/analyses/{analysis_id}/decision`, `POST /ai/troubleshoot/analyses/{analysis_id}/applied`, `POST /ai/troubleshoot/analyses/{analysis_id}/actions/{action_id}`, `GET /ai/mcp/servers/{name}/tools` | "Analyze with AI" on a DAG run / task instance: scoped log analysis, structured report, approve → deploy, Airflow actions per `ai.troubleshoot.airflow_actions` ([ai-troubleshooting.md](ai-troubleshooting.md)) |
| `POST /ai/upgrade` | Airflow upgrade assistant (ruff AIR3 + rules + AI) |
| `POST /ai/deploy-message` | Commit subject / PR description from the spec diff |
| `GET /ai/rules`, `GET /ai/rules/effective`, `PUT/DELETE /ai/rules/{scope}` | Project AI rules injected into authoring prompts |

Findings share one shape: `severity` (`critical`, `high`, `medium`, `low` or `info`), `category`, `title`, `message`,
`node_id` + `field` (the editor selects the node and focuses the field, same convention as
validation problems), `path` (JSON pointer), `evidence` (secrets masked), `source`
(`rule`/`ai`) and `patch` — RFC 6902 operations with node-id pointers
(`/nodes/@load/retries`). Reports include `proposed_spec` (all patches applied and validated),
`usage` (tokens, cache hits, cost when `AUTOAIR_AI__PRICING` is set) and the model used.

Review, leak scan and optimise always run deterministic rules first (also without an LLM) and
give the model those findings as "already reported", so it spends tokens only on what rules
cannot see. The rules-only review and leak scan do not depend on AI at all: `POST
/review/rules`, `POST /lint/secrets` and the MCP tools `review_dag_spec` / `scan_dag_leaks`
need `builder:use` only and work without the `autoair[ai]` extra or with `features.ai` off;
`use_ai: true` adds `ai:use` and `features.ai`. Secrets found by the rules are redacted before the spec leaves the server.

## External MCP servers (AutoAir as an MCP *client*)

AutoAir's agents can also **use** external MCP servers as tool providers (fastmcp `Client`
over streamable HTTP, SSE or stdio), for example a metrics, ticketing or runbook server during
an incident analysis. Servers come from `ai.mcp_client.servers` (any
transport) or the admin panel's **MCP servers** tab (`/api/admin/mcp-servers`, HTTP/SSE only;
header values encrypted at rest, never returned). Each server has a tool allow-list, secret
headers, a timeout and the agents allowed to attach it; users pick the servers and tools
per analysis, and the tools appear to the model as `mcp__<server>__<tool>`. Outputs are
truncated, scrubbed and treated as untrusted. Details:
[ai-troubleshooting.md](ai-troubleshooting.md#external-mcp-servers).

## Architecture and choices

See [ai-architecture.md](ai-architecture.md) for the design: in-process vs the separate
AI service (and the proxy), SSE streaming, the agent runtime on pydantic-ai, guardrails,
hybrid retrieval with RRF and reranking, air-gapped/online research, the writers, per-user
models and the knowledge base.

* **Models** are [pydantic-ai](https://ai.pydantic.dev) models (`pip install
  'autoair[ai,ai-anthropic]'`; `ai-openai`, `ai-google`, `ai-ollama`, `ai-bedrock`,
  `ai-groq`, `ai-mistral`, `ai-cohere`, `ai-all`), the same library and connection format
  (`pydanticai`: `extra.model` = `"provider:model"`) as Airflow's common.ai provider. The
  provider SDKs handle retries, streaming, tool calls and usage; AutoAir passes them its
  `httpx2` client so `outbound.*` (CA bundle, client certificate, proxies) applies.
* **Without the `ai` extra** `features.ai` is off, so the tools that need it
  (`TOOL_FEATURES` in `src/autoair/mcp/iam.py`: optimise, generate, fix, `write_code`, the
  research and memory tools) are unavailable; `review_dag_spec` and `scan_dag_leaks` run their
  rules only, and the catalog, validation, spec, Airflow and knowledge-base tools stay.
* **Structured outputs** are Pydantic models used as pydantic-ai output types; validation
  errors go back to the model (output retries).
* **Features whose context is known up front** (review, fix, explain, writers) use single
  structured calls or a validate-and-repair loop, which is cheaper and faster than an agent;
  open questions go to the agent.
