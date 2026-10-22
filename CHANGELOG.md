# Changelog

All notable changes to AutoAir are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Until 1.0.0,
minor versions may contain breaking changes; they are listed under **Changed** or **Removed**.

## [Unreleased]

## [0.2.1] - 2026-10-22

### Fixed
- Deploy target dialog: object storage targets can be chosen like git and local ones. Airflow's
  `S3DagBundle` and `GCSDagBundle` entries are discovered, and an object storage prefix can be
  entered by hand (fsspec URL plus optional non-secret storage options; credentials still come
  from the server environment or `AUTOAIR_STORAGE__STORAGE_OPTIONS`). Before, switching to
  object storage was possible only through the API. `storage.allowed_targets` still applies.
- Container image: `pip check` passes again. The Airflow base image ships `litellm` (an optional
  dependency of Vertex AI evaluation), which pins `openai<3`, while `autoair[ai-openai]` needs
  `openai>=3`. The image now removes `litellm`, which AutoAir does not use, and the build and
  the CI smoke test run `pip check`. Vertex AI evaluation with third-party judge models needs
  `litellm` and is not available in this image.
- `ai.llm_conn_id` finds connections stored in the Airflow metadata database again. Outside a
  task the Task SDK's `BaseHook.get_connection` does not read the metadata DB, so on Airflow
  3.1+ the plugin, `autoair serve` and `autoair ai-serve` reported "conn_id isn't defined" for
  connections created in the Airflow UI or CLI. All connection lookups (LLM, git, knowledge
  base, validator key) now share one helper that checks env vars, the configured secrets
  backend and the metadata DB first, then the Task SDK. The
  `AIRFLOW__SECRETS__BACKEND=airflow.secrets.metastore.MetastoreBackend` workaround is no
  longer needed.
- Python DAG import (import dialog, code mode, bulk import, MCP `import_python_dag`) keeps the
  `python_callable` of classic `PythonOperator`, `BranchPythonOperator`,
  `ShortCircuitOperator` and `PythonSensor` tasks (and other callable operator arguments).
  Before, the callable was dropped and the deploy failed with "python_callable missing". A
  function defined in the DAG file becomes an inline function (its source without decorators,
  with the file's imports it uses), a function imported from another module an import
  reference; a lambda, a `functools.partial` or a function that uses other names of the file
  is reported in the import warnings. Source-preserving deploys of such files compare these
  functions as code, so a canvas change is patched into the file instead of being refused.

## [0.2.0] - 2026-10-22

### Added
- Kustomize manifests for Kubernetes (`deploy/kustomize`): a hardened base for `autoair serve`
  (non-root, read-only root file system, no service account token, probes on `/api/health` and
  `/api/ready`, PodDisruptionBudget, topology spread), Components for PostgreSQL state with an
  `autoair db upgrade` init container, the git deploy target, the validator service (no egress),
  the AI service, a separate MCP server, a Gateway API HTTPRoute (the production default) or an
  Ingress, HorizontalPodAutoscaler,
  NetworkPolicies and Prometheus ServiceMonitors, and `dev`, `production` and `airflow-plugin`
  overlays. Secrets are referenced, never included; credentials stay Airflow connections. CI
  builds every overlay and validates it with kubeconform. New page: Deployment → Kubernetes
  with Kustomize.
- **AutoAir docs MCP**: AutoAir's own documentation (the website pages and the configuration
  reference) is packaged in the wheel at build time (`hatch_build.py`, `autoair.docs.build`) and
  served to coding agents by the MCP tools `search_autoair_docs`, `read_autoair_doc` and
  `list_autoair_docs`: offline, matched to the installed version, without Airflow, the AI extra
  or a knowledge-base store. `autoair docs-mcp` (stdio, or `--http`) serves only these tools,
  e.g. `uvx --from 'autoair[mcp]' autoair docs-mcp`; they are also on every AutoAir MCP endpoint
  (no AutoAir permission, feature flag `features.autoair_docs`). Setup for Claude Code, Cursor,
  OpenCode and Codex CLI: website page *AI → Docs MCP*.
- Knowledge base: built-in source kind and component `autoair` (AutoAir's packaged
  documentation, no network), so `kb_search` and the assistants cite AutoAir pages.
- Isolated import checks: `autoair validator-serve` runs the DagBag import check in a separate
  validator service, so the DAG code it imports (raw-code DAGs, Python task bodies) no longer
  executes in the Airflow API server or `autoair serve` (Python DAG import and test runs still
  do). Set `validator.url`
  (`AUTOAIR_VALIDATOR__URL`); requests are HMAC-signed with the password of the Airflow
  connection `validator.secret_conn_id` (no new secret setting). Workers get a fresh interpreter
  per check, memory/CPU/file-size/open-file limits, no new privileges, a network namespace where
  the kernel allows it and a Python audit hook (no subprocesses, sockets, DNS, writes outside
  their scratch folder or reads of other processes' `/proc`). Retries, a health probe and
  `validator.fail_mode` (`open` by default: an `import_service` warning and no import check
  while the validator is down; `closed` blocks deploys instead; `local`). `/health`, `/ready`, `/metrics`
  (`autoair_validator_*`) on the validator, `autoair_import_checks_total` on the main server.
  New Helm chart `deploy/helm/autoair-validator` (Deployment, Service, PodDisruptionBudget,
  NetworkPolicy denying all egress, read-only non-root pods) and Compose overlay
  `deploy/compose/docker-compose.validator.yml`. New page: Deployment → Validator service.
- Astronomer Blueprint steps: templates of `airflow-blueprint` (installed packages with the
  `airflow_blueprint.blueprints` entry point, `blueprints.template_dirs`, `blueprints.modules`)
  appear in the palette's *Blueprints (Astronomer)* section and become `kind: blueprint` nodes
  with a config form generated from the template's Pydantic model, version selection, a task
  preview (inspector and collapsible node) and validation against the config schema. Python and
  loader output call `Template().render(Config(...))`; the YAML output mode reports them as
  unsupported, the dag-factory export skips them. The Python importer folds `render()` calls
  back into blueprint steps. New extra `autoair[blueprint]`, feature switch
  `features.blueprints`, settings section `blueprints`, routes `/api/blueprints/*` and the MCP
  tool `list_blueprints`. Guide: User guide → Blueprint steps.
- Airflow 3.4: `plugin.dag_tab_tags` (`AUTOAIR_PLUGIN__DAG_TAB_TAGS`) shows the **AutoAir**
  DAG tab only on DAGs with one of the listed tags, through the plugin's `applies_to`
  scoping. Empty (default) keeps the tab on every DAG page; ignored before Airflow 3.4.
- Validation warns about task groups that depend on each other in a cycle
  (`task_group_cycle`), which Airflow 3.4 deprecates and Airflow 3.5 is planned to reject.
- Editing existing Python DAGs keeps the hand-written file: each DAG has a source policy
  (`auto`, `generate`, `preserve`, `strict-preserve`; DAG settings and the deploy dialog).
  Imported and code-mode DAGs default to preserve: a deploy applies only the change to the
  existing file with a LibCST patch (keyword arguments, TaskFlow bodies, added and removed
  tasks and dependencies, renames), verifies it with a round-trip import and the DagBag
  check, and never regenerates the file on its own. The deploy dialog previews the DAG file
  as a unified diff (`POST /api/specs/{dag_id}/source-preview`, MCP `preview_dag_source`);
  refused changes explain why and offer raw code or a deliberate switch to `generate`.
  A stopped deploy says why by the file's state (the hand-written file AutoAir keeps, a file
  changed outside AutoAir, or one AutoAir did not create); "Keep as raw code DAG" and
  "Overwrite the file" follow the user's `code:raw` and `deploy:overwrite_ejected`; a source
  policy picked in the deploy dialog changes the DAG only when it deploys (Cancel keeps it).
  Code mode and the Code tab show the hand-written file. New settings `preserve.*`, feature flags
  `source_preserve` and `strict_source_preserve`. `libcst` is a new core dependency.
- Generated DAG files carry a fingerprint line (`# autoair:fingerprint sha256=…`), so a file
  edited outside AutoAir is recognised on folder, object-storage and git targets even without
  AutoAir's state. Such a deploy stops and offers to apply the change to the edited file
  (merged with the outside edits); `preserve.ejected_files` (`block`, `warn`, `overwrite`)
  sets the behaviour. New permission `deploy:overwrite_ejected` (Admin; never from a
  wildcard) for a confirmed overwrite.
- Tool-call approval for AI agents (pydantic-ai deferred tools): when the developer or admin
  assistant or a troubleshooting analysis calls a tool that changes something (`remember`,
  external MCP tools not marked `readOnlyHint`), the run pauses with an approval card showing
  the tool and its masked arguments; the user who started the run approves or denies each
  call (optionally with a reason) and the run resumes in the same answer or analysis.
  Approvals are stored with the state (multi-replica safe, decided at most once, expiring
  after `ai.approvals.ttl_seconds`) and audited. New settings `ai.approvals.*`, new endpoints
  `GET /api/ai/approvals`, `GET /api/ai/approvals/{id}` and
  `POST /api/ai/approvals/{id}/stream`. New page: AI → Tool-call approval.
- Large DAGs: above 150 nodes the canvas can zoom out to an overview of the whole graph
  (**Fit view**), and below 40 % zoom task cards turn into light, colour-tinted boxes with
  their name (problems, run states, diff and selection stay visible). Benchmarks for large
  DAGs: `scripts/gen_large_dag.py` (synthetic specs), `scripts/bench_large_dag.py` (server
  timings) and `frontend/e2e/large-graph.spec.ts` (load, layout, frame rate, memory). New
  page: User guide → Large DAGs (measured limits and tuning).
- `preserve.allow_force_overwrite` (`AUTOAIR_PRESERVE__ALLOW_FORCE_OVERWRITE`, default
  `false`): the "Overwrite (force)" choice is offered and accepted only when it is `true`, for
  the hand-written file AutoAir keeps for a DAG, a DAG file changed outside AutoAir and `force`
  deploys of the loader and YAML output modes and raw code. The caller still needs
  `deploy:overwrite_ejected` and an explicit confirmation; `/api/meta` reports
  `allow_force_overwrite`, and the source-plan and conflict dialogs hide the overwrite button
  otherwise. Source plans refuse with the new code `overwrite_disabled`.
- `server.harden_process` (`AUTOAIR_SERVER__HARDEN_PROCESS`, default `true`) controls whether
  `autoair serve`, `autoair local` (and their uvicorn workers), `autoair mcp-serve --http` and
  `autoair ai-serve` make their process non-dumpable. Turn it off only to debug with py-spy,
  gdb or core dumps; the server then logs a warning at start-up. Never applied in the Airflow
  plugin.

### Changed
- `spec_version` is now 3. Version 3 adds only optional parts (`source` and the `blueprint`
  node kind); version 1 and 2 documents keep loading and are written as version 3 the next
  time they are saved or deployed. **Upgrade note:** AutoAir 0.1.0 refuses version 3 specs
  with "spec_version 3 is newer than this AutoAir supports (2); upgrade AutoAir", also in the
  loader stubs inside Airflow. Upgrade every AutoAir process that reads the same specs
  together (plugin, `autoair serve` replicas, MCP and AI services, the `autoair` package in
  Airflow's dag-processor and workers); rolling back to 0.1.0 needs the specs from before the
  upgrade.
- Overwriting a DAG file AutoAir would otherwise not replace now needs
  `preserve.allow_force_overwrite=true` (default `false`). `preserve.ejected_files=overwrite`
  takes effect only with it (otherwise it behaves like `block`, also for administrators), and
  regenerating the kept hand-written file (switch to generate) needs it as well as
  `deploy:overwrite_ejected`. Deleting a kept hand-written DAG still needs only the
  permission. Files without AutoAir's marker are never overwritten by `force`. A deploy that
  would replace the kept hand-written file without a source plan (another output mode, a
  conversion to raw code) returns HTTP 200 with `deployed: false`, `status: "ejected"` and
  `conflict.ejected_file`, `conflict.hand_written` and `conflict.force_allowed` instead of
  HTTP 409; ejected results without a source plan also carry `conflict.force_allowed`.
  **Upgrade note:** deployments that relied on `preserve.ejected_files=overwrite` or on
  regenerating kept hand-written files must set `AUTOAIR_PRESERVE__ALLOW_FORCE_OVERWRITE=true`;
  API clients that handled the 409 must read `status: "ejected"` instead.
- MCP `save_raw_dag_code`: a raw code DAG that replaces its own code no longer needs
  `deploy:overwrite_ejected`, like the REST deploy (`code:raw` is still required over HTTP).
  The file is written only while it is unchanged since AutoAir wrote it; MCP has no `force`,
  so a file changed outside AutoAir is refused. Replacing a visual DAG's kept file is still
  refused.
- **Upgrade note:** the validator service (`autoair validator-serve`) and the main server
  must run the same AutoAir version; there is no mixed-version compatibility window. Upgrade
  and roll back both together.
- Knowledge base defaults: `kb.sources` is now `official` plus `autoair`, and `kb.components`
  includes `autoair`. With `kb.refresh_on_start` the server now builds the (small) index of
  AutoAir's own documentation at start-up even when air-gapped. A `kb.sources` list you set
  replaces the default; add `{"id": "autoair", "kind": "autoair"}` to keep AutoAir's pages.
- The warm DagBag pool exchanges JSON with its workers instead of pickles, so DAG code that
  writes to the worker's pipe can no longer execute code in the AutoAir process when the
  parent reads the reply. Workers run in their own process group, and stopping one also stops
  the processes it started.
- CI tests the Airflow 3.4.0b2 pre-release (was 3.4.0b1). No final 3.4 exists yet. Airflow
  3.4 needs Python 3.11 or newer; AutoAir keeps supporting Python 3.10 with Airflow 3.0-3.3.
- A local deploy whose reparse request Airflow refuses (`403`, for example a file without a
  registered DAG on Airflow 3.4 without the `REPARSE_ALL` permission, or `404`) says so in the
  deploy message instead of "could not contact Airflow"; the deploy itself succeeds. A
  `409` (a reparse of the file is still queued) is reported as "reparse already requested".
- The deploy option `force` no longer overwrites a Python DAG file that was changed outside
  AutoAir: answer the stopped deploy with `source_resolution` instead. For loader stubs it
  needs `deploy:overwrite_ejected`.
- **Deployer** cannot deploy a DAG that keeps its hand-written Python file: the round
  trip needs `code:execute` and writing the imported file needs `code:raw`. The refusal
  names the missing permission. New built-in role **CodeDeployer** (Deployer +
  `code:execute` + `code:raw`) for those deploys; or deploy with the `generate` policy.
  Regenerating a hand-written file AutoAir keeps needs `deploy:overwrite_ejected` and
  `preserve.allow_force_overwrite=true`; deleting it needs the permission.
- Imported DAGs (Python file, Airflow, repository import, MCP) and code applied in the code
  mode keep their Python file (`source.origin: python`); deploying it as written needs
  `code:execute` for the round trip and `code:raw` when the file comes from the spec. The
  repository import dialog can generate the files instead. Promoting such a DAG between
  environments is refused (`409`).
- Calls of `remember` and of external MCP tools without `readOnlyHint: true` now wait for
  the user's approval by default (previously they ran directly). Exempt tools with
  `ai.approvals.default.exempt` or `ai.approvals.agents.<agent>.exempt`. Troubleshooting
  analyses can have the new status `awaiting_approval`.
- Validation lints all Python task bodies with one ruff run and shell scripts with one
  ShellCheck run per 50 scripts, and remembers results of unchanged bodies (a failed run is
  retried, not remembered; `.shellcheckrc` files are no longer read): validating a 1,000-task DAG takes
  about 0.3 s the first time and 0.1 s afterwards (was 2-5 s, one process per task).
  `editor.python_lint_timeout` and `editor.shellcheck_timeout` now apply per batched run.
- Editor performance on large DAGs: dragging no longer serialises the whole document for undo
  history, live checks and collaboration on every frame; node badges, run-state overlays and
  the inspector no longer re-render or rescan every node per change; live checks wait longer
  after the last change on DAGs with more than 200 nodes (up to 2 s); an auto layout that
  finishes after the graph changed is discarded instead of applied.
- Testing: visual regression baselines inside the Airflow UI (`npm run test:visual:plugin`):
  the eight standalone screens plus the connection form, clear and backfill dialogs and asset
  actions, light and dark, against a private Airflow with the plugin. The browser runs in the
  Playwright Docker image so the baselines match on every Linux machine and in CI (the
  non-blocking *Frontend · visual regression* job runs both suites).
- `autoair[all]` no longer includes `mcp`: FastMCP 4 needs Starlette 1.x, which Airflow 3.1's
  FastAPI pin excludes, so `all` now installs next to every supported Airflow. Use
  `autoair[all,mcp]` on Airflow 3.0, 3.2 and newer or standalone. The `sqlite`, `postgres` and
  `kb-pgvector` extras accept SQLAlchemy 1.4.49 or newer. New page: Installation →
  Compatibility (extras per Airflow version).

### Fixed
- Airflow 3.4: the latest log of a task waiting for a retry (`up_for_retry`) is the failed
  try again, not the upcoming try that has no log yet (run panel, AI troubleshooting and fix,
  MCP `get_task_log`). `GET /api/airflow/capabilities` reports `retry_try_is_next`.
- The SQLite and PostgreSQL state backends and the pgvector knowledge-base store work on
  SQLAlchemy 1.4, the version Airflow 3.0 installs (previously an `ImportError` on
  `sqlalchemy.Connection`). On SQLAlchemy 1.4 PostgreSQL goes through psycopg2.
- Without `autoair[mcp]` the MCP feature is reported unavailable with the reason
  (`/api/meta` `mcp_unavailable_reason`, admin *Features* tab) instead of only being unmounted.
- Python 3.10: git history timestamps ending in `Z` and `$type: datetime` parameters sent by
  the browser (`toISOString`) no longer fail to parse.
- Python DAG import on Airflow 3.2: `CronTriggerTimetable("@daily", ...)` is recognised as a
  preset again (SDK timetables keep the preset unresolved).
- Security: a hand-written DAG file adopted over MCP (`save_dag_spec` with source policy
  preserve) is recorded as kept, like a REST deploy, so regenerating or deleting it needs
  `deploy:overwrite_ejected`. The deploy targets (local folder, object storage, git) now refuse
  to replace or remove a kept file on every write path that skips the source plan: switching
  the DAG to the loader or YAML output mode, converting it to a raw code DAG, MCP saves and
  promotions. A deploy with `force` by a holder of `deploy:overwrite_ejected` still may, with
  `preserve.allow_force_overwrite=true` and only for a file with AutoAir's marker; a raw code
  DAG keeps editing its own code.
- Security: a DAG that writes part of a reply to the import-check worker's pipe and stops no
  longer hangs a DagBag pool thread (and, in the validator service, a capacity slot until a
  restart): the check's deadline covers reading the whole reply, after which the worker is
  replaced. Replies carry a per-check nonce, so code left running from an earlier check
  cannot answer for a later one.
- Security: the validator service signs its `/v1/check` answers with the HMAC key, bound to
  the request's nonce (`x-autoair-validator-response-signature`); the main server rejects
  unsigned or mismatched answers and treats the validator as unavailable
  (`validator.fail_mode` applies). Update the validator and the main server together.
- Security: `autoair serve`, `autoair local`, `autoair mcp-serve --http` and `autoair ai-serve`
  make their process non-dumpable at start-up (Linux), so DAG code in their child processes
  cannot read the server's environment through `/proc`. The Airflow plugin leaves Airflow's
  process unchanged; use `validator.url` there.
- Security: denial reasons of AI tool approvals are scrubbed of secrets and passed through the
  guardrails before they reach the model, the stored steps and the audit record.
- Security: for a hand-written file AutoAir did not write (content hidden), deploy results no
  longer return the merged `stored_spec`, and import and round-trip errors no longer quote the
  file.
- Security: the MCP `save_dag_spec` validates a preserved hand-written file with the same
  inputs as the REST deploy (operator templates, target Airflow version, user and action for
  OPA).
- Blueprint task previews report an unavailable validator service as a preview error instead
  of HTTP 500.
- The deploy audit record and commit context say when the import check was skipped
  (`validation.import_check_skipped`: `fail_open` when the validator was unavailable,
  `permission` without `code:execute`).
- The validator's Kustomize NetworkPolicy admits Airflow api-server pods only from the
  namespace `airflow` (a placeholder to adjust) instead of from every namespace.

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

[Unreleased]: https://github.com/ctolon/autoair/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/ctolon/autoair/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/ctolon/autoair/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/ctolon/autoair/releases/tag/v0.1.0
