---
title: Optional extras
description: Every optional dependency group of the autoair package, what it enables, the settings that turn the feature on, and how to install it with pip or uv.
sidebar_label: Extras
---

The core `autoair` package installs only what every mode needs (FastAPI, Pydantic, httpx2, fsspec, Jinja, jedi, Typer, PyYAML, cryptography, Pillow and the Prometheus client). Everything else is an **extra**. A feature whose extra is missing is either switched off with a log message or reports a clear error that names the extra to install.

Not every extra can be installed next to every Airflow version: `mcp` does not resolve with Airflow 3.1. See [Airflow compatibility](/docs/installation/compatibility) for the matrix.

## Installing extras

Extras go in square brackets. Quote the requirement so the shell does not expand the brackets.

```bash
# pip
pip install 'autoair[standalone,format,git,mcp,collab,postgres]'

# uv, in a project
uv add 'autoair[standalone,format,git,mcp,collab,postgres]'

# uv, one-off tool run
uvx --from 'autoair[standalone,airflow]' autoair local

# a local wheel
pip install "dist/autoair-0.1.0-py3-none-any.whl[standalone,format]"
```

Inside an Airflow image, install AutoAir with the extras in the same `pip install` as your providers, and pin `apache-airflow` to the image's version so pip does not change it (the demo Dockerfile does `pip install "apache-airflow==${AIRFLOW_VERSION}" "<wheel>[standalone,format,git,ai,ai-openai,ai-anthropic,collab,collab-redis,postgres,otel,yaml]" apache-airflow-providers-standard apache-airflow-providers-git`).

## Which extras for which mode

| Mode | Recommended extras |
| --- | --- |
| Local mode | `standalone,airflow` (add `format` for formatted output) |
| Airflow plugin | `format,git,mcp` plus `ai` and a provider extra such as `ai-anthropic` for AI (add `collab`, `postgres`, `otel`, `shell` as needed); never `airflow` (Airflow is already installed) and not `standalone` (Airflow's API server serves AutoAir). On Airflow 3.1 leave out `mcp` and [run the MCP server separately](/docs/installation/compatibility#mcp-next-to-airflow-31) |
| Standalone server | `standalone,format,git,mcp,collab` plus `postgres` for several replicas and `ai,ai-<provider>` for AI |
| Separate AI service | `standalone,mcp,ai` plus the `ai-*` provider extras you use (the main server then needs no `ai` extra) |

## Reference

### Runtime and server

| Extra | Installs | Enables | Turned on by |
| --- | --- | --- | --- |
| `airflow` | `apache-airflow>=3.0,<4`, `apache-airflow-providers-standard` | A standard operator palette (the operators and sensors of `apache-airflow-providers-standard`, such as `BashOperator`, `PythonOperator` and `EmptyOperator`) and the `DagBag` import check when AutoAir runs without an Airflow installation, mainly for `autoair local` | Installed packages are picked up automatically. In local mode `import_check_on_deploy` is turned on only when Airflow is importable. |
| `standalone` | `uvicorn[standard]` | The HTTP server behind `autoair serve`, `autoair local`, `autoair mcp-serve --http` and `autoair ai-serve`. Without it these commands exit with code 1 and a message naming the extra to install (`autoair local` also suggests `airflow` while Airflow is not installed). | Running one of those commands. Not needed in plugin mode. |
| `format` | `ruff` | `ruff format` of generated DAG files and live ruff lint of Python task code in the editor | Automatic when `ruff` is found on `PATH` or importable. Tune with `codegen.line_length`, `codegen.format_timeout`, `editor.python_lint_select`. Without ruff, generated code is emitted unformatted. |
| `yaml` | `pyyaml` | YAML configuration files (`AUTOAIR_CONFIG_FILE=/etc/autoair/autoair.yaml`) | PyYAML is also a core dependency; the extra is a stable install name. A missing PyYAML is reported as "YAML configuration files need PyYAML: pip install 'autoair[yaml]'". |
| `otel` | `opentelemetry-instrumentation-fastapi`, `opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-http` | OpenTelemetry tracing of the API | `AUTOAIR_OTEL_ENABLED=true`. Exporter settings use the standard `OTEL_*` variables (`OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_SERVICE_NAME`); inside Airflow an existing tracer provider is reused. `server.otel_excluded_urls` skips health, ready and metrics. |

### Deploy targets and state

| Extra | Installs | Enables | Turned on by |
| --- | --- | --- | --- |
| `git` | nothing | Stable install name for the Git deploy target | `AUTOAIR_STORAGE__MODE=git`, `AUTOAIR_STORAGE__GIT__PROVIDER`, `AUTOAIR_STORAGE__GIT__REPO` and credentials (`AUTOAIR_STORAGE__GIT__CONN_ID` or `AUTOAIR_STORAGE__GIT__TOKEN`); every option is listed under [`storage.git`](/docs/reference/configuration#storagegit). The Git target talks to GitHub, GitLab, Bitbucket and Azure DevOps through their REST APIs; `storage.git.provider=local` (any remote) and `gitea` (Gitea or Forgejo) run the `git` command-line tool, which must be on `PATH`. |
| `sqlite` | `sqlalchemy` (1.4.49 or newer; Airflow 3.0 installs 1.4) | AutoAir state (saved templates, the audit trail, reviews and deploy proposals, the deploy target chosen in the UI, custom icons, AI memory, the AI audit log, AI knowledge overrides, per-user AI model settings, collaboration room snapshots, users seen and replica heartbeats) in one SQLite file | `AUTOAIR_DATABASE__BACKEND=sqlite`, optionally `AUTOAIR_DATABASE__URL=sqlite:////var/lib/autoair/autoair.db` (default `<state>/autoair.db`). Suits several workers on one host. |
| `postgres` | `sqlalchemy`, `psycopg[binary]` | AutoAir state in PostgreSQL, shared by every replica | `AUTOAIR_DATABASE__BACKEND=postgresql`, `AUTOAIR_DATABASE__URL=postgresql+psycopg://autoair@db:5432/autoair`, `AUTOAIR_DATABASE__PASSWORD`. Missing driver: "cannot load the postgresql driver (...): pip install 'autoair[postgres]'". On Airflow 3.0 (SQLAlchemy 1.4) the driver is psycopg2: see [PostgreSQL on Airflow 3.0](/docs/installation/compatibility#postgresql-on-airflow-30). |

:::note
The `git` extra is empty: every Git provider uses either the host's REST API (over httpx2, a core dependency) or the `git` CLI, so AutoAir needs no Python Git library. The extra only keeps `autoair[git]` (and `autoair[all]`) installable under a stable name; a Git deploy target works without it as long as the `git` binary is installed where the `local` or `gitea` provider runs. (Airflow's `GitDagBundle` needs `apache-airflow-providers-git` on the Airflow side, which brings its own dependencies.)
:::

Object storage targets (`AUTOAIR_STORAGE__MODE=object`) use fsspec, which is a core dependency; install the fsspec implementation for your store yourself (`s3fs`, `gcsfs`, `adlfs`). See [Object storage](/docs/deployment/object-storage) and [Database](/docs/admin/database).

### Collaboration

| Extra | Installs | Enables | Turned on by |
| --- | --- | --- | --- |
| `collab` | `pycrdt`, `pycrdt-websocket` | Live co-editing (Yjs over WebSockets at `/collab/<dag_id>`) | `AUTOAIR_FEATURES__COLLAB=true` (default). Without the packages the `/collab` WebSocket endpoint is not mounted, so co-editing is unavailable. Several standalone replicas also need `AUTOAIR_COLLAB_SECRET` (or a shared database) and `collab.sync`. |
| `collab-redis` | `redis` | Fan-out of collaboration rooms through Redis pub/sub | `AUTOAIR_COLLAB__SYNC=redis` and `AUTOAIR_COLLAB__REDIS_URL=redis://redis:6379/0`. `collab.sync=auto` (default) picks Redis when `collab.redis_url` is set. |

### Agents and AI

| Extra | Installs | Enables | Turned on by |
| --- | --- | --- | --- |
| `mcp` | `fastmcp`, `mcp` | The MCP endpoint at `/mcp` (plugin: `/autoair/mcp/`), `autoair mcp` (stdio) and `autoair mcp-serve` | `AUTOAIR_FEATURES__MCP=true` (default). Without the packages the server logs "MCP endpoint disabled: ... (install autoair[mcp])", `/api/meta` reports `mcp_unavailable_reason` and the admin *Features* tab shows `mcp` as *Not installed*. Writes are controlled by `ai.mcp.allow_writes`. Cannot be installed next to Airflow 3.1 ([why](/docs/installation/compatibility)). |
| `ai` | `pydantic-ai-slim` | The AI runtime ([pydantic-ai](https://ai.pydantic.dev), as used by Airflow's common.ai provider; compatible with it in one environment). Without it AI is off: `/api/meta` reports `ai_unavailable_reason`, the admin panel shows the hint, only `GET /api/ai/status` answers, the AI MCP tools are hidden and the admin *Features* tab shows `ai` as *Not installed* and `autoair ai-serve` exits with the extra to install. | Install it with a provider extra, then configure `AUTOAIR_AI__PROVIDER`, `AUTOAIR_AI__MODEL`, `AUTOAIR_AI__API_KEY` (or `AUTOAIR_AI__LLM_CONN_ID`, a `pydanticai` connection). |
| `ai-anthropic` | `pydantic-ai-slim[anthropic]` (the `anthropic` SDK) | Claude | `AUTOAIR_AI__PROVIDER=anthropic` |
| `ai-openai` | `pydantic-ai-slim[openai]` (the `openai` SDK) | OpenAI, Azure OpenAI and OpenAI-compatible servers (vLLM, LM Studio, LiteLLM) | `AUTOAIR_AI__PROVIDER=openai` / `azure`; `AUTOAIR_AI__BASE_URL` for compatible servers |
| `ai-google` | `pydantic-ai-slim[google]` (`google-genai`) | Gemini and Vertex AI | `AUTOAIR_AI__PROVIDER=gemini` / `google-cloud` |
| `ai-ollama` | `pydantic-ai-slim[openai]` | Ollama (its OpenAI-compatible API) | `AUTOAIR_AI__PROVIDER=ollama`, `AUTOAIR_AI__BASE_URL=http://ollama:11434` |
| `ai-bedrock`, `ai-groq`, `ai-mistral`, `ai-cohere` | `pydantic-ai-slim[bedrock]` / `[groq]` / `[mistral]` / `[cohere]` | Amazon Bedrock, Groq, Mistral, Cohere | `AUTOAIR_AI__PROVIDER=bedrock` / `groq` / `mistral` / `cohere` and `AUTOAIR_AI__MODEL` |
| `ai-all` | every provider extra above | All providers | |
| `ai-rerank` | `sentence-transformers` (pulls PyTorch; large) | Local cross-encoder reranking for AI retrieval | `AUTOAIR_AI__RETRIEVAL__RERANKER=cross_encoder`, optional `AUTOAIR_AI__RETRIEVAL__RERANK_MODEL`. Without it the reranker is skipped with a warning. |
| `ai-guardrails` | `llm-guard` | The LLM Guard input scanners as an extra guardrail | `AUTOAIR_AI__GUARDRAILS__EXTERNAL='["llm_guard"]'`. NeMo Guardrails (`nemo:<config dir>`) and Guardrails AI (`guardrails_ai`) need `nemoguardrails` or `guardrails-ai` installed separately. |

See [AI providers](/docs/ai/providers), [Guardrails](/docs/ai/guardrails), [AI service](/docs/ai/ai-service) and [MCP](/docs/ai/mcp).

### Documentation knowledge base

The knowledge base works without these extras on its default `local` store (SQLite, no dependencies).

| Extra | Installs | Enables | Turned on by |
| --- | --- | --- | --- |
| `kb-pgvector` | `sqlalchemy` (1.4.49 or newer), `psycopg[binary]` (psycopg2 on SQLAlchemy 1.4) | The `pgvector` knowledge-base store in PostgreSQL | `AUTOAIR_KB__STORE=pgvector`, optionally `AUTOAIR_KB__PGVECTOR__URL` (default: AutoAir's own database when `database.backend=postgresql`) |
| `kb-elasticsearch` | nothing | Stable install name for the `elasticsearch` store, which uses the REST API over httpx2 | `AUTOAIR_KB__STORE=elasticsearch`, `AUTOAIR_KB__ELASTICSEARCH__URL` |

See [Knowledge base](/docs/ai/knowledge-base).

### Editors

| Extra | Installs | Enables | Turned on by |
| --- | --- | --- | --- |
| `shell` | `shellcheck-py` (the ShellCheck binary) | ShellCheck diagnostics for `bash_command`, `@task.bash` and `.sh` script files, in the editor and as validation warnings | Automatic when installed. `editor.shellcheck_in_validation`, `editor.shellcheck_exclude`, `editor.shellcheck_exclude_extra`, `editor.shellcheck_timeout`. |
| `lsp` | `python-lsp-server`, `python-lsp-ruff` | IDE-grade Python IntelliSense through pylsp | `AUTOAIR_FEATURES__IDE_LANGUAGE_SERVER=true` and `AUTOAIR_LSP__PYTHON_SERVER=pylsp` |
| `lsp-pyright` | `basedpyright` (bundles Node through `nodejs-wheel`) | IDE-grade Python IntelliSense through basedpyright | `AUTOAIR_FEATURES__IDE_LANGUAGE_SERVER=true` (`lsp.python_server` defaults to `basedpyright`) |

Without a language server the editors keep the built-in jedi completion, hover and signature help. Bash IntelliSense through a language server needs `npm i -g bash-language-server` (`lsp.bash_command`); without it shell fields keep the built-in completion and ShellCheck. Limits: `lsp.max_sessions` (16), `lsp.max_sessions_per_user` (4), `lsp.idle_timeout` (600 s), `lsp.start_timeout` (30 s), `lsp.memory_limit_mb` (2048), `lsp.cpu_limit_seconds` (3600) and `lsp.max_message_bytes` (8 MiB); see [`lsp`](/docs/reference/configuration#lsp) in the configuration reference.

### `all`

`autoair[all]` is a shortcut for `standalone,format,git,ai,ai-anthropic,ai-openai,otel,collab,shell,yaml`: everything that installs next to every supported Airflow. It does **not** include `mcp` (it cannot be installed with Airflow 3.1), `airflow`, `sqlite`, `postgres`, `collab-redis`, `kb-pgvector`, `kb-elasticsearch`, the other `ai-*` extras (`ai-google`, `ai-bedrock`, ..., `ai-rerank`, `ai-guardrails`), `lsp` or `lsp-pyright`; add those explicitly.

```bash
pip install 'autoair[all,mcp,postgres]'   # Airflow 3.0, 3.2 and newer, or standalone
pip install 'autoair[all,postgres]'       # Airflow 3.1
```

## Checking what is active

- `autoair config show --changed` prints the settings that differ from the defaults and where each came from.
- The admin panel shows the AutoAir version, the state backend and its schema version, and the operator catalog statistics. See [Admin panel](/docs/admin/admin-panel).
- Server logs name each optional feature that was switched off because its extra is missing.
