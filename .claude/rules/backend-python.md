---
paths:
  - "src/autoair/**/*.py"
  - "tests/**/*.py"
  - "scripts/**/*.py"
---

# Backend Python

## Module map (`src/autoair`)

| Package / module | Responsibility | Entry points |
|---|---|---|
| `spec/` | `DagSpec` Pydantic models, the single source of truth; schema migrations; raw code DAGs (`DagSpec.raw_code`) | `models.py` (`DagSpec`, `SPEC_VERSION`), `migrations.py` (`MIGRATIONS`, `migrate`) |
| `codegen/` | spec -> Python AST -> source (ruff-formatted); version-aware imports; YAML and dag-factory export | `render(spec, ..., airflow_version=)`, `builder.py`, `features.py` (`required_features`), `yaml_dag.py`, `dagfactory.py` |
| `loader/` | `output_mode="loader"`: a stub DAG file builds the DAG from `.autoair.json` at parse time | `load_dag`, `render_stub` |
| `catalog/` | operator/notifier discovery without importing modules (static AST), subprocess fallback, disk cache, precomputed catalog file | `service.py` (`OperatorCatalog`, `build_catalog_file`), `static.py`, `discovery.py`, `metadata.py` |
| `blueprints/` | Astronomer Blueprint (`airflow-blueprint`) templates as `kind: blueprint` nodes: static discovery (entry points, template folders, modules), config schemas and default previews in a child process, task previews in the DagBag pool | `catalog.py` (`BlueprintCatalog`, attached as `OperatorCatalog.blueprints`), `worker.py`, `preview.py`, `config.py` (`BlueprintSettings`) |
| `validation/` | graph, catalog, Jinja, policy, version gates, OPA, warm DagBag pool, optional isolated validator service; raw code checks | `validate_spec`, `Problem`, `ValidationReport`; `raw.py` (`validate_raw`), `opa.py`, `pool.py` (`DagBagPool`), `service.py` (`autoair validator-serve`), `remote.py`, `sandbox.py` |
| `preserve/` | source-preserving edits of hand-written DAG files with LibCST: static task mapping, minimal patches, round-trip check, deploy plans, ejected/hand-written file detection, fingerprints | `service.py` (`plan_source`), `mapper.py`, `patcher.py`, `fingerprint.py`, `config.py`; `api/source_plans.py` |
| `docs/` | AutoAir's own documentation packaged in the wheel (`pages.json`, built by `hatch_build.py`) and the docs MCP tools / `autoair docs-mcp` | `build.py`, `index.py`, `mcp.py` |
| `policy_rules.py` | organisation policy rules and `OpaOptions` | |
| `importer/` | Python DAG -> spec (subprocess), bulk import, code editor <-> canvas sync | `python_dag.py`, `bulk.py`, `code_sync.py` |
| `gateway/` | async Airflow REST v2 client forwarding the caller's credentials | `AirflowGateway`; `api/deps.py::get_gateway` + `auth.forwarded_credentials` |
| `storage/` | deploy targets and git providers; per-request git tokens | `base.py` (`DeployTarget`), `local.py`, `object.py`, `git/target.py`, `git/provider.py`, `config.py` (`build_target`, `_git_provider`), `request_token.py` |
| `governance.py` | deploy context, commit/PR templates, identities | `DeployContext`, `commit_message`, `pr_body` |
| `persistence/` | state backends `file` / `sqlite` / `postgresql`; own migrator (no Alembic); replica heartbeats | `document_store(...)`, `documents.py`, `sql.py`, `schema.py`, `migrations.py` (`autoair db upgrade`) |
| `collab.py`, `collab_sync.py` | Yjs co-editing rooms and tickets; room sync across replicas (`memory`, `database`, `redis`) | `create_collab_app`, `build_sync` |
| `iam/` | permissions on top of Airflow's auth manager; role strategies; four-eyes proposals; SSO cookie; standalone login | `model.py` (permissions, `BUILTIN_ROLES`), `deps.py` (`require`), `resolver.py`, `proposals.py`, `sso.py`, `login.py` |
| `api/` | FastAPI app and routers | `app.py` (`create_api`, `create_app`, `build_services`), `routers/*`, `deps.py`, `features.py` (`requires_feature`, imported as `feature` in `app.py`) |
| `mcp/` | MCP server and its IAM middleware | `server.py` (`create_mcp_server`), `iam.py`, `http.py` |
| `ai/` | AI features on pydantic-ai (`autoair[ai]`): generation, edit, explain, assistants, troubleshooting, RAG, guardrails, SSRF-safe networking, AI service | `models.py`, `agent.py`, `llm.py`, `service.py`, `guardrails.py`, `toolbox.py`, `net.py`, `assistants/`, `config.py` (`AiSettings`) |
| `kb/` | documentation knowledge base: sources, parse/chunk, lease-protected ingestion, stores `local` / `pgvector` / `elasticsearch`, hybrid search; works without `autoair[ai]` | `config.py` (`KbSettings`), `service.py` (`kb_for`, `kb_context`), `ingest.py`, `stores/`, `bundle.py`, `mcp.py`, `cli.py` |
| `lsp/` | Jedi completion/hover, language-server proxy (basedpyright/pylsp), code validation, ShellCheck | `proxy.py`, `validate.py`, `shell.py` |
| `net/tls.py` | outbound TLS, CA bundle, client certificates, proxies per client (names: `OutboundClient` in `sections.py`) | `async_client`, `httpx_kwargs`, `transport_kwargs` |
| `settings.py`, `sections.py`, `config_sources.py` | `Settings` (`AUTOAIR_` prefix, `__` nesting), generic sections, config files and precedence | `Settings`, `FeatureFlags`, `sections.active()` |
| `feature_overrides.py` | runtime feature switches set by admins (stored document, CAS) | `FeatureOverrideStore`; `api/routers/admin_features.py` |
| `secret_mask.py` | masks registered secrets in logs, audit and errors | `masking`, `mask`, `install_log_masking` |
| `_compat.py` | Airflow version detection and symbol locations | `airflow_version`, `supports_react_apps`, `sdk_symbol_module` |
| `registry/`, `scaffold/` | custom node kinds (`autoair.node_kinds` entry point) and the `autoair new-node` template | `NodeKindHandler`, `new_node` |
| `runner/` | task and DAG test runs in a child process, no metadata DB | `run_task_test`, `run_dag_test` |
| `audit.py`, `marketplace.py`, `plugin.py`, `server.py`, `cli.py` | audit trail; signed (Ed25519) template marketplaces; Airflow plugin; standalone app factory; CLI | |

## Style

- ruff (line length 100, rules in `pyproject.toml`) and `mypy --strict` over `src/`. mypy skips
  `airflow.*` (`follow_imports = "skip"`), so it will not catch a symbol missing on Airflow 3.0.
- Full type hints; `from __future__ import annotations`; Pydantic models for structured data
  crossing a boundary (API, MCP, settings, persisted documents).
- No bare `except:` or silent `except Exception: pass`; catch the narrowest exception and keep
  the cause (`raise ... from exc`).
- `async def` where the framework calls async (FastAPI routes, gateway, MCP), with no blocking
  I/O inside (use `anyio.to_thread` or an executor).
- Import optional extras (`pydantic_ai`, `sqlalchemy`, `pycrdt`, `fastmcp`) inside functions so
  the core install works without them.
- Errors: `HTTPException(status, detail)` with an actionable message; spec findings are
  `validation.Problem` (`severity`, `code`, `message`, `node_id`, `field`); never echo secrets.
- Naming: `snake_case` modules and functions, `PascalCase` models; settings named for what they
  control, with units in the description; permissions are `area:action` strings in
  `iam/model.py`.
- HTTP code uses `httpx2` (Pydantic's httpx fork) through `net/tls.py`; `httpx` is not a
  declared dependency.

## Tests

- pytest markers (`--strict-markers`, declared in `pyproject.toml`): `airflow` (auto-skipped
  without Airflow), `postgres` (`AUTOAIR_TEST_POSTGRES_URL`), `live`
  (`AUTOAIR_LIVE_AIRFLOW_URL`; real auth managers and GitLab, CI only).
- `tests/conftest.py` resets process-wide sections around every test (`sections.reset_all()`);
  tests that need a value pass `Settings(...)` explicitly instead of setting env vars.
- HTTP fakes use `httpx2.MockTransport`; explicit edge cases instead of property-based
  generators. Writing tests: subagent `test-writer`.
