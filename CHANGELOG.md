# Changelog

All notable changes to AutoAir are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Until 1.0.0,
minor versions may contain breaking changes; they are listed under **Changed** or **Removed**.

## [Unreleased]

### Changed
- `autoair[all]` no longer includes `mcp`: FastMCP 4 needs Starlette 1.x, which Airflow 3.1's
  FastAPI pin excludes, so `all` now installs next to every supported Airflow. Use
  `autoair[all,mcp]` on Airflow 3.0, 3.2 and newer or standalone. The `sqlite`, `postgres` and
  `kb-pgvector` extras accept SQLAlchemy 1.4.49 or newer. New page: Installation →
  Compatibility (extras per Airflow version).

### Fixed
- The SQLite and PostgreSQL state backends and the pgvector knowledge-base store work on
  SQLAlchemy 1.4, the version Airflow 3.0 installs (previously an `ImportError` on
  `sqlalchemy.Connection`). On SQLAlchemy 1.4 PostgreSQL goes through psycopg2.
- Without `autoair[mcp]` the MCP feature is reported unavailable with the reason
  (`/api/meta` `mcp_unavailable_reason`, admin *Features* tab) instead of only being unmounted.
- Python 3.10: git history timestamps ending in `Z` and `$type: datetime` parameters sent by
  the browser (`toISOString`) no longer fail to parse.
- Python DAG import on Airflow 3.2: `CronTriggerTimetable("@daily", ...)` is recognised as a
  preset again (SDK timetables keep the preset unresolved).

## [0.1.0] - 2026-10-21

First public release.

### Added

#### Builder and canvas
- Visual DAG editor (React Flow) inside Airflow (**Browse → DAG Builder** and an **AutoAir**
  tab on every DAG page on Airflow ≥ 3.1; a full page at `/autoair/ui/` on 3.0) and as a
  standalone server (`autoair serve`).
- Integration logos on nodes and custom icon upload with an in-browser resizer; notes; task
  groups (group, ungroup, collapse); insert-on-edge; command palette; context menus;
  copy/paste with id and XCom remapping; alignment guides; undo/redo; ELK auto-layout in a web
  worker; run-state and version-diff overlays; keyboard shortcuts; collapsible panels.
- Full-screen code mode and an optional editable Code tab (`features.code_edit_inline`) that
  re-import edited Python into the visual spec; a read-only preview of the file generated for
  a chosen environment (`POST /api/codegen?environment=<env>`).
- Editor modes: `features.visual_editor` and `features.code_editor` give visual-only and
  code-only deployments; switching them at runtime keeps the open DAG and an unapplied code
  draft.
- Raw code DAGs (`DagSpec.raw_code`, permission `code:raw`): a hand-written DAG file stored,
  versioned, reviewed, proposed, deployed, promoted and audited like a visual DAG; validated
  by syntax, ruff, secret detection, policy (privileged and forbidden operators found by a
  static scan, dag_id rules, OPA with `context.document_type=raw`) and the DagBag import
  check, with problems located by line. **File → New raw code DAG**, **Convert to raw
  code…** and **Open in visual builder**; a **Raw code** badge and a type filter in the Open
  dialog.
- English and Turkish UI, light and dark themes, UI branding (title, logo, accent, banner).

#### Authoring
- Typed DAG spec (Pydantic v2) with migrations as the single source of truth; the TypeScript
  client is generated from the OpenAPI document.
- Every installed operator, sensor and transfer, from official and third-party providers,
  Airflow libraries, plugins, DAG folders and packages, through a static AST catalog that does
  not import the modules. The catalog can be precomputed with `autoair catalog build`.
- Operator packages describe their operators for the palette without AutoAir configuration:
  an `autoair.yaml` / `autoair.json` file in the package or an `autoair.operator_metadata`
  entry point (palette group and order, icons including sanitised SVGs, parameter widgets,
  examples, docs links). Settings `catalog.connection_param_patterns`, `catalog.groups`,
  `catalog.default_group_order`, `catalog.param_overrides` and `icons.rules_extra` take
  precedence over package metadata, which takes precedence over inference.
- Catalog diagnostics: `GET /api/catalog/diagnostics`, the admin panel's **Catalog** tab and
  `autoair catalog dump --diagnostics` (sources, operators per source, filtered operators and
  why, failed modules, applied package metadata, build time).
- Python references to callables and classes; functions written in place; embedded script
  files written to every worker at parse time.
- TaskFlow: `@task`, `@task.branch`, `short_circuit`, `virtualenv`, `external_python`,
  `sensor`, `bash`, `docker`, `kubernetes` and `@task.stub` (language SDKs).
- Dynamic task mapping (`expand`, `expand_kwargs`, `map_index_template`), task loops
  (Airflow 3.4), setup/teardown, trigger rules, callbacks and notifiers, retry policies
  (3.3), deadlines (3.1), human-in-the-loop operators with a form designer, OpenLineage
  toggles, disabled tasks.
- DAG params with a live trigger-form preview; environment overlays (dev/staging/prod).
- Scheduling: DAG time zone (DST aware), cron, presets, intervals, multi-cron, event dates,
  asset expressions, event-driven assets (AssetWatcher with SQS, Kafka, Pub/Sub, Redis,
  Service Bus, Kinesis), assets-or-time, assets-and-time (3.4) and partitioned schedules
  (3.2), with a live "next runs" preview from Airflow's timetable engine.
- Jinja-aware editors for templated fields with completion and inline preview;
  drag-to-map upstream XComs; connection pickers filtered by type with inline "New
  connection" (create and test in Airflow); IDE-style operator docs; Markdown fields with
  snippets and preview.
- Python and shell editors: jedi completion, hover and signature help that know the installed
  Airflow and providers, live ruff lint, ShellCheck diagnostics. Optional language servers
  (basedpyright, pylsp, bash-language-server) per session.
- Output as readable Python, a JSON spec with a runtime loader, an Airflow 3.4 native YAML
  DAG, or dag-factory YAML. Generated imports follow the target Airflow version.
- Import from hand-written Python DAGs (mapped tasks, `partial()` arguments and cron presets
  kept), from Airflow, and from YAML.

#### Validation and policy
- Live validation: graph, catalog, Jinja, policy and Airflow version gates, plus a warm
  DagBag import-check pool.
- Organisation policy: flat switches (required owner or description, minimum retries,
  required tags, forbidden operators, max tasks, dag_id prefix, operator allow/deny lists,
  privileged operators, Python code), configurable id and secret-detection patterns,
  declarative `policy.rules`, and an optional Open Policy Agent decision.
- Hard-coded secret detection in parameters and inline code, with a hint to use Connections
  or Variables.
- Rules-only best-practice review and leak/secret scan without AI (`POST /api/review/rules`,
  `POST /api/lint/secrets`, `builder:use`), offered in the **Run** menu when AI is off.

#### Lifecycle and deploy
- Deploy targets: a local folder (LocalDagBundle), object storage through fsspec
  (S3/GCS DagBundles) and Git: the git CLI with any remote, or the GitHub, GitLab, Bitbucket,
  Azure DevOps and Gitea APIs.
- Git pull-request mode (branch and PR per DAG) or direct push, environment promotion
  (validated for the target environment like a deploy), PR labels, reviewers and drafts, and
  GitHub App authentication.
- Deploy target dialog: discovers where Airflow reads DAGs (DAG folder,
  GitDagBundles, a git-sync checkout) and switches without a restart. Credentials always come
  from Airflow connections: shared, per user, or forced by an administrator.
- Git tokens typed per deploy: the deploy, promote, delete, conflict-retry and bulk-import
  dialogs accept the user's own token, sent only in the `X-AutoAir-Git-Token` header of that
  request and never stored, logged or audited; HTTPS or loopback only. Settings
  `storage.git.request_token` (`off` / `optional` / `required`),
  `request_token_overrides_forced`, `request_token_allow_insecure` and `mcp_request_token`.
- Deploy governance: a mandatory context block and `X-AutoAir-*` git trailers on every
  commit and PR, plus commit, PR and branch templates and a live preview.
- ETag optimistic locking with a three-way semantic merge dialog; protection of ejected
  (hand-edited) files; version history with a visual graph diff.
- Test a task or a whole DAG without the metadata database; trigger, backfill (dry run),
  clear and pause/unpause; a run overlay with logs and XComs.
- Template gallery, reusable fragments and operator templates; signed (Ed25519) template
  marketplaces (`autoair marketplace keygen|build|verify`).
- Operator template editor, available in every editor mode from the palette, the template
  gallery, the command palette and the admin panel: catalog operator picker, parameter form,
  a Default / Locked / Required switch per field, live validation, a code preview, and a
  version history with a diff (`GET /api/templates/{id}/versions[/{version}]`,
  `templates_history_limit`). Saves and deletes take `If-Match` / `If-None-Match: *` and
  answer 409 on a conflict; another team's template needs `admin:config`.

#### Collaboration, identity and access
- Real-time co-editing (Yjs): presence, remote cursors and selections, per-user undo, and
  WebSocket access through short-lived tickets that require DAG edit permission.
- IAM on top of Airflow's auth manager: built-in and custom roles; `airflow_permissions`,
  `role_mapping` and `custom_view` strategies (FAB, Keycloak, SimpleAuthManager); four-eyes
  approvals; direct-push roles; `autoair iam roles|sync-fab`; an audit trail with CSV export.
- Standalone sign-in through the Airflow UI session (SSO), with Airflow username and
  password, or with an API token; every token is verified by Airflow, and sign-out is
  propagated.
- Runtime feature flags: the admin panel's **Features** tab (`GET/PUT /api/admin/features`)
  switches `features.*` without a restart. Overrides are shared by replicas through the state
  backend (`features_override_ttl`) and audited with a reason; `features_locked` and
  `AUTOAIR_FEATURES__<NAME>` lock a flag.
- Admin panel: overview, configuration, audit browser, replicas, templates, users, AI usage
  and rules, MCP servers, proposals, catalog, knowledge base, database and caches; audit
  export and scan sizes are configurable (`admin.export_limit`, `admin.export_default`,
  `admin.scan_limit`, `admin.usage_scan_limit`).

#### AI and MCP
- AI on [pydantic-ai](https://ai.pydantic.dev) as an optional extra (`autoair[ai]` plus one
  provider extra: `ai-anthropic`, `ai-openai`, `ai-google`, `ai-ollama`, `ai-bedrock`,
  `ai-groq`, `ai-mistral`, `ai-cohere`, or `ai-all`). Providers `anthropic`, `openai`,
  `azure`, `gemini`, `google-cloud`, `ollama`, `bedrock`, `groq`, `mistral`, `cohere` and
  OpenAI-compatible servers. Without the extra, AI is reported off with the reason
  (`GET /api/meta` → `ai_unavailable_reason`, `GET /api/ai/status` → `reason`).
- AI assistance: generate a DAG from a prompt, edit with AI (reviewed as a diff), explain and
  fix problems, review, write docs, troubleshoot runs. Central or per-user models (per-user
  keys are encrypted); `ai.llm_conn_id` and per-user connections read Airflow's common.ai
  `pydanticai` connection format.
- Agents on pydantic-ai with typed tools, structured output, streaming and usage limits;
  assistant threads replay earlier tool calls and results; retrieval with optional
  reranking, guardrails, research (air-gapped or online) and an AI audit log.
- AI troubleshooting can run the Airflow actions it proposes (clear or retry a task instance,
  clear the run, mark a task or the run success or failed): `ai.troubleshoot.airflow_actions`
  = `read_only` (default), `hitl` (each action runs after the user approves it) or `apply`.
  Actions are vetted (`allowed_actions`, `max_actions`, `max_cleared_task_instances` checked
  by a dry run, upstream/downstream limits), run with the user's Airflow token and
  `run:manage`, are claimed once across replicas, and are audited.
- Documentation knowledge base (`kb` settings, `features.kb`): the Apache Airflow core, Task
  SDK, provider, Helm chart and airflowctl documentation and Astronomer's guides at the
  installed versions, read from the official sites, HTTP mirrors, git repositories, folders,
  object storage or signed portable bundles (`autoair kb build|import`) for air-gapped
  installs. Incremental, SSRF-checked, size- and time-limited ingestion by one replica at a
  time; hybrid BM25 and vector search (BM25 only without `autoair[ai]`) in a local SQLite,
  pgvector (`kb-pgvector`) or Elasticsearch (`kb-elasticsearch`) store. Used by the agents,
  writers, review and fix; routes `/api/kb/*`, MCP tools `kb_search`, `kb_sources`,
  `kb_status`, and `autoair kb sources|status|refresh|search|build|import|keygen|mcp`.
- A separate AI service (`autoair ai-serve`) that the main server proxies to, with
  HMAC-signed caller identity.
- MCP server with authoring tools for Claude Code, Claude Desktop and other agents (stdio or
  streamable HTTP inside Airflow), including raw code DAGs and operator templates; the review
  and leak-scan tools work without AI (`builder:use`). An MCP client attaches external servers
  to the agents and chat assistants (`ai.mcp_client.servers[].agents`).

#### Operations
- Deployment modes: Airflow plugin, standalone server (workers, TLS, mutual TLS, proxy
  headers, security headers, HSTS), and `autoair local` for a laptop without an Airflow
  server.
- State in files (fsspec), SQLite or PostgreSQL (`autoair db upgrade|current`). With
  PostgreSQL, replicas are stateless; collaboration fans out through the database
  (`LISTEN/NOTIFY`) or Redis.
- Configuration from environment variables, YAML/JSON/INI/Python files and `airflow.cfg`;
  `autoair config show`; a generated configuration reference; admin-only config exposure.
- Outbound TLS settings (CA bundle, client certificates, proxies, per-client overrides) for
  Airflow, git, marketplace, AI, MCP and knowledge-base calls, on the `httpx2` client.
- `/api/health`, `/api/ready`, Prometheus `/api/metrics`, JSON logs and optional
  OpenTelemetry tracing.
- A container image on the official Airflow image with a precomputed catalog, a non-root
  user and a health check (`ghcr.io/ctolon/autoair`); Helm values for the official chart;
  a Docker Compose demo with profiles for a load balancer, Redis, the AI service, Ollama,
  Gitea, TLS and tracing.
- Supports Apache Airflow ≥ 3.0, < 4 (CI: 3.0.6, 3.1.8, 3.2.2, 3.3.2, plus the 3.4
  pre-release) on Python 3.10–3.14.

[Unreleased]: https://github.com/ctolon/autoair/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/ctolon/autoair/releases/tag/v0.1.0
