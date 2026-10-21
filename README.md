# AutoAir: a no-code DAG builder for Apache Airflow 3

AutoAir lets you design Airflow DAGs on a visual canvas and produces **readable, type-checked
Python** (or a JSON spec with a runtime loader, an Airflow 3.4 native YAML Dag, or
dag-factory YAML). It validates with Airflow's own `DagBag`, deploys to a **local folder, object
storage or Git (direct push or pull requests)**, and lets you trigger, debug and version DAGs
from the Airflow UI. AI agents can use the same operations through AutoAir's **MCP server**.

* **Airflow 3.0 or later, below 4.** CI covers 3.0.6, 3.1.8, 3.2.2 and 3.3.2, plus the 3.4.0b1
  pre-release, on Python 3.10–3.14. Generated imports follow the target Airflow version (for
  example, timetables come from `airflow.timetables.*` on 3.0/3.1 and from `airflow.sdk` on 3.2+).
* Runs **inside Airflow** as a plugin (Browse → *DAG Builder* and an *AutoAir* tab on every DAG
  page on 3.1+; a full page at `/autoair/ui/` on 3.0) **or standalone** (`autoair serve`).

## Install

```bash
pip install "autoair[airflow]"      # into the Airflow environment: the plugin loads automatically
pip install "autoair[standalone]"   # standalone server: autoair serve --airflow-url http://airflow:8080
uvx --from 'autoair[standalone,airflow]' autoair local --open   # try it on a laptop, no Airflow server
```

AI is optional and runs on [pydantic-ai](https://ai.pydantic.dev) (like Airflow's common.ai
provider): add `ai` plus a provider extra, e.g. `pip install "autoair[ai,ai-anthropic]"`
(`ai-openai`, `ai-google`, `ai-ollama`, `ai-bedrock`, `ai-groq`, `ai-mistral`, `ai-cohere`,
`ai-all`). Other optional extras: `git`, `mcp`, `collab`, `collab-redis`, `sqlite`, `postgres`,
`otel`, `format`, `shell`, `lsp`, `lsp-pyright`, `ai-rerank`, `ai-guardrails`, and `all` for the
common set. The container image
`ghcr.io/ctolon/autoair` is the official Airflow image with AutoAir installed. See
[deployment](docs/deployment.md).

## Features

**Canvas.** React Flow editor with integration logos on nodes (S3, Jira, dbt, BigQuery and more,
plus custom icon upload), notes, task groups, insert-on-edge, a command palette (⌘/Ctrl+K),
context menus, copy/paste with id and XCom remapping, alignment guides, undo/redo, ELK
auto-layout, run-state and version-diff overlays, and collapsible panels (⌘/Ctrl+B, I, J).
English and Turkish UI, light and dark themes.

**Authoring.** Every installed operator, sensor and transfer, from official and third-party
providers, plugins, DAG folders and packages, through a static AST catalog that does not import
the modules. Also:
- TaskFlow tasks (`@task`, `@task.branch`, `short_circuit`, `virtualenv`, `sensor`, `bash`,
  `docker`, `kubernetes`, `@task.stub`), dynamic mapping (`expand`, `expand_kwargs`,
  `map_index_template`), task loops (3.4), setup/teardown and trigger rules.
- Callbacks and notifiers, retry policies (3.3), deadlines (3.1), human-in-the-loop operators
  with a form designer, OpenLineage toggles and disabled tasks.
- DAG params with a live trigger-form preview, and environment overlays (dev/staging/prod).
- Parameters that take a callable accept an import path or a function written in place. Script
  parameters (Bash, Spark, SQL and others) accept an embedded script file that is written to
  every worker at parse time.
- Hard-coded passwords and tokens are flagged with a hint to use Connections or Variables.

**Scheduling.** DAG time zone (DST aware), cron, presets, intervals, multi-cron, event dates,
asset expressions, event-driven assets (AssetWatcher with SQS, Kafka, Pub/Sub, Redis, Service Bus
or Kinesis), assets-or-time, assets-and-time (3.4) and partitioned schedules (3.2), with a "next
runs" preview computed by Airflow's timetable engine.

**Editors.** Jinja-aware CodeMirror fields with completion for context, params, variables,
connections, macros and `ti.xcom_pull`; drag-to-map upstream XComs; connection pickers filtered
by type, with inline connection creation and testing; Python and shell editors with jedi
completion, hover and signature help for the installed Airflow and providers, live ruff lint and
ShellCheck diagnostics. Operator docstrings render like an IDE hover. Every field shows its Python
type, whether it is required, and its description; clicking a problem jumps to the field.

**Lifecycle.**
- Live validation and code preview (Python, Airflow YAML, dag-factory, spec JSON), backed by a
  warm DagBag import-check pool.
- Test a single task or the whole DAG without the metadata database.
- Deploy with ETag optimistic locking and a three-way semantic merge dialog, with protection for
  hand-edited (ejected) files.
- Version history with a visual graph diff.
- Git pull-request mode (GitHub, GitLab, Bitbucket, Azure DevOps, Gitea, or any remote through
  the git CLI) with environment promotion.
- A deploy-target dialog that discovers where Airflow reads DAGs (DAG folder, GitDagBundles, a
  git-sync checkout).
- Trigger, backfill (dry run), clear, and pause or unpause from the builder; a run overlay with
  logs and XComs.
- Templates, reusable fragments, and import from hand-written Python DAGs, Airflow and YAML.
- An audit trail.

**Collaboration and access.** Live co-editing (Yjs) with presence, remote cursors and per-user
undo; WebSocket access through short-lived tickets that require DAG edit permission. IAM on top
of Airflow's auth manager with four-eyes approvals ([IAM](docs/iam.md)). Signed (Ed25519) template
marketplaces (`autoair marketplace keygen|build|verify`).

**AI and MCP.** Generate a DAG from a prompt, edit with AI (reviewed as a diff), explain and fix
problems, write docs and troubleshoot runs, using Anthropic, OpenAI or OpenAI-compatible servers,
Gemini or Ollama, with a central or per-user model ([AI architecture](docs/ai-architecture.md)).
The MCP server exposes authoring tools to Claude Code, Claude Desktop and other agents over stdio
or streamable HTTP ([MCP](docs/mcp.md)).

**Operations.** `/api/health`, `/api/ready` and Prometheus `/api/metrics` (under `/autoair` in the
plugin), optional OpenTelemetry tracing, organisation policy rules ([policy](docs/policy.md)), state
in files, SQLite or PostgreSQL, a container image with a precomputed catalog, and Helm values for
the official Airflow chart.

## Architecture

```
React 19 + @xyflow/react ── typed client (hey-api, generated from OpenAPI) ──► FastAPI (AutoAir API, MCP)
  zustand + zundo · RJSF · CodeMirror · i18next                                 │
                                                                               ├─ spec/        Pydantic v2 DagSpec + migrations (single source of truth)
                                                                               ├─ catalog/     static AST provider index (+ subprocess fallback, disk cache)
                                                                               ├─ codegen/     spec → AST → ruff · YAML (AIP-85) · dag-factory · version-aware imports
                                                                               ├─ loader/      the same AST compiled at parse time (+ environment overlays)
                                                                               ├─ validation/  graph · catalog · Jinja · policy · version gates · warm DagBag pool
                                                                               ├─ storage/     local · object (fsspec) · git (CLI, GitHub, GitLab, Bitbucket, Azure DevOps, Gitea)
                                                                               ├─ runner/      task and DAG test runs without the metadata DB, in a sandbox process
                                                                               ├─ importer/    Python DAG → spec, bulk import, code sync
                                                                               ├─ iam/         roles and permissions on top of Airflow's auth manager
                                                                               ├─ ai/ · mcp/   AI assistants and the MCP server
                                                                               ├─ collab · lsp Yjs rooms (ticket auth) · language services
                                                                               └─ gateway/     Airflow REST API v2 (caller's token forwarded → Airflow RBAC)
```

## Development

```bash
uv sync && (cd frontend && npm ci && npm run build)
./scripts/dev-airflow.sh   # Airflow 3 + plugin at http://localhost:8080 → Browse → DAG Builder
# Git mode: AUTOAIR_DEV_GIT_REPO=file:///path/to/bare.git ./scripts/dev-airflow.sh  (adds a GitDagBundle)
uv run autoair serve --dags-folder /path/to/dags --airflow-url http://localhost:8080   # standalone
uv run autoair mcp         # MCP server (stdio)
```

```bash
make check     # ruff, mypy --strict, hard-coded audit, config reference, tsc, eslint, prettier
make test      # pytest (unit + real-Airflow integration) and vitest
make matrix    # nox: Airflow 3.0 / 3.1 / 3.2 / 3.3, the 3.4 pre-release, Python 3.10
make e2e       # Playwright inside the Airflow UI
make crawl     # UI crawler: opens every menu, dialog, tab and node inspector; any error fails
make codegen   # regenerate the TypeScript client after API changes
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the full workflow. `autoair --help` lists the CLI
commands: `serve`, `local`, `generate`, `validate`, `mcp`, `mcp-serve`, `ai-serve`,
`import-python`, `import-yaml`, `export-yaml`, `export-dag-factory`, `export-schema`,
`export-openapi`, `new-node`, `catalog`, `marketplace`, `iam`, `config`, `kb` and `db`.

## Documentation

User documentation (installation, user guide, administration, AI, extending):
https://ctolon.github.io/autoair/ (source in [`website/`](website/README.md); `make docs-serve`).

In-repo reference: [configuration](docs/configuration.md) ·
[configuration reference](docs/configuration-reference.md) · [deployment](docs/deployment.md) ·
[IAM](docs/iam.md) · [SSO with Airflow](docs/sso.md) · [policy](docs/policy.md) ·
[AI architecture](docs/ai-architecture.md) · [AI assistants](docs/ai-assistants.md) ·
[AI troubleshooting](docs/ai-troubleshooting.md) · [state and scaling](docs/architecture/state-and-scaling.md) ·
[MCP](docs/mcp.md) · [extending](docs/extending.md) · [roadmap](docs/ROADMAP.md) ·
[changelog](CHANGELOG.md)

## License

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE). Security issues: see
[SECURITY.md](SECURITY.md).
