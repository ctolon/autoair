---
title: Feature flags
description: Every features.* switch, what it turns off in the API, the CLI and the UI, and how it interacts with the allow_python_code policy switch.
---

Feature flags switch whole features off for a deployment. They live in the `features` section: set `AUTOAIR_FEATURES__<NAME>=false` in the environment, or `features: {<name>: false}` in a [configuration file](/docs/admin/configuration#configuration-files).

When a flag is off:

* **API**: the feature's routes answer `404` with `the <name> feature is disabled (AUTOAIR_FEATURES__<NAME>)`. The routes stay in the OpenAPI document, so generated clients do not depend on the configuration.
* **CLI**: commands that belong to the feature print the same message and exit with code `2`.
* **UI**: `GET /api/meta` reports the effective flags in `features`, and the UI hides (or disables) everything that depends on a disabled flag.

Flags restrict what [IAM](/docs/admin/iam) allows; they never grant anything. A user still needs the matching permission when a flag is on.

## Flags

All flags default to `true` except `code_edit_inline`, `strict_source_preserve` and `ide_language_server`.

| Flag | Default | API and CLI effect when off | UI effect when off |
|---|---|---|---|
| `ai` | `true` | AI routes are replaced by a stub: only `GET /api/ai/status` answers, with `enabled: false`. AI assistants, troubleshooting and the admin panel's **Rebuild index** action are unavailable. | The **AI** menu shows only that AI is off; "Generate with AI…" on the empty canvas, **Analyze with AI** on runs and the AI deploy-message helper disappear. |
| `mcp` | `true` | `/mcp` answers `404` (Model Context Protocol endpoint for agents; not mounted at all when the flag is locked off). | No UI of its own; `/api/meta` reports `mcp_enabled: false`. |
| `collab` | `true` | `/collab/<dag_id>` WebSocket and `POST /api/collab/<dag_id>/ticket` answer `404`. | No live co-editing: the presence and collaboration control is hidden. |
| `marketplace` | `true` | `/api/marketplace/*` answers `404`; `autoair marketplace keygen` and `build` exit with `2`. | The marketplace section of the templates dialog is hidden. |
| `templates` | `true` | `/api/templates/*` answers `404`. | **File → New from template…**, **Save as template…**, **Save selection as fragment…** and template inserts on the canvas are hidden. |
| `imports` | `true` | `/api/interchange/*/import` answers `404`; `autoair import-python` and `import-yaml` exit with `2`. | **File → Import** (From Python file…, From Airflow…, From YAML…, From AutoAir spec (JSON)…) is hidden, as is the import offer for unmanaged DAGs. |
| `test_runs` | `true` | `/api/runner/*` (task and DAG test runs) answers `404`. | **Run → Test selected task…** and **Test whole DAG…** are hidden. |
| `runs` | `true` | Trigger, backfill, clear, pause/unpause and asset events (`POST /api/airflow/dags/{dag_id}/trigger`, `POST /api/airflow/dags/{dag_id}/backfill`, `POST /api/airflow/dags/{dag_id}/runs/{run_id}/clear`, `PUT /api/airflow/dags/{dag_id}/paused`, `PUT /api/airflow/dags/{dag_id}/scheduling-state`, `POST /api/airflow/assets/{asset_id}/events`, `POST /api/airflow/assets/{asset_id}/materialize`) answer `404`. | **Run → Trigger…** and **Backfill…** and the **Runs** tab of the bottom panel are hidden. |
| `deploy` | `true` | Saving and deleting specs (`PUT`/`DELETE /api/specs/<id>`) and environment promotion answer `404`. Reading specs and history still works. | The **Deploy** button and deploy dialog are hidden. |
| `custom_icons` | `true` | Uploading and deleting custom icons answers `404`; existing icons are still served. | The inspector's icon picker no longer offers uploads; built-in logos can still be chosen. |
| `python_code` | `true` | Python code in DAGs is refused by validation (`@task` bodies, inline functions, script files, Python references, raw code DAGs); saving a raw code DAG answers `404`. Same effect as `AUTOAIR_ALLOW_PYTHON_CODE=false`. `autoair import-python` exits with `2` (importing executes the file). | **From Python file…** is disabled; the code mode cannot apply edits (it re-imports the code). |
| `airflow` | `true` | `/api/airflow/*` and `POST /api/interchange/airflow/{dag_id}/import` (import from Airflow, which also needs `airflow:read` and `code:execute`) answer `404`: connection, pool and variable pickers, DAG status, runs, import from Airflow. AI troubleshooting answers `404`, `POST /api/ai/fix` cannot read a task log from Airflow (`404`; a pasted log still works), the developer assistant gets no Airflow tools (runs, task instances, logs), and the MCP tools that call Airflow (`list_connections`, `list_variables`, `get_dag_status`, `list_dag_runs`, `get_task_log`, `trigger_dag`) are disabled. A deploy to a local folder does not ask Airflow to reparse the file. Off in `autoair local`. | Pickers fall back to free text; **From Airflow…**, **Trigger…**, **Backfill…**, the **Runs** and **Airflow** tabs are hidden. |
| `kb` | `true` | The [documentation knowledge base](/docs/ai/knowledge-base#switching-it-off): `/api/kb/*` answers `404`, the MCP tools `kb_search`, `kb_sources` and `kb_status` are refused, `search_docs` adds no documentation hits, `autoair kb` exits with `2` and scheduled refreshes stop. | No UI of its own; the admin panel's **Knowledge base** tab gets `404` for its requests. |
| `iam` | `true` | AutoAir's own authorization (roles, role mapping, custom views, four-eyes approvals, direct-push roles) is switched off; see [Switching IAM off](/docs/admin/iam#switching-iam-off). | `GET /api/me` reports `iam_enabled: false`; role chips reflect the Airflow-derived baseline. |
| `visual_editor` | `true` | The visual (canvas) editor. At least one of `visual_editor` and `code_editor` must be on: switching both off is a configuration error at start-up, and the admin panel refuses it (`422`). | Off (code only): no canvas, palette, insert palette, auto-layout or canvas commands; visual DAGs open in the synced code mode and raw code DAGs in the raw code editor. **Open in visual builder** is hidden. |
| `code_editor` | `true` | Direct code editing: the synced code mode of visual DAGs (`POST /api/interchange/python/sync` answers `404` when off) and [raw code DAGs](/docs/user-guide/code-mode#raw-code-dags) (saving one answers `404` when off). Applying code needs `spec:write`, `code:execute` and `python_code`; saving raw code needs `spec:write`, `code:raw` and `python_code`. | Off (visual only): no **Visual \| Code** toggle and no code mode; **New raw code DAG** is hidden; an existing raw code DAG opens read-only with a notice. |
| `code_edit_inline` | `false` | Makes the bottom panel's **Code** tab editable, with the same import-on-apply as the code mode. | When on, the **Code** tab becomes an editor instead of a read-only preview. |
| `source_preserve` | `true` | [Source-preserving deploys](/docs/user-guide/editing-python-dags): off, the source policy Auto means Generate for every DAG, an explicit Preserve or Strict preserve is refused at deploy (`source_refused`), and files changed outside AutoAir stay protected (`preserve.ejected_files`). | The deploy preview shows generated files only. |
| `strict_source_preserve` | `false` | On: every Preserve deploy runs as Strict preserve (risky edits are refused instead of warned about). | The deploy preview shows the policy as Strict preserve. |
| `ide_language_server` | `false` | IDE-grade IntelliSense in code editors through a per-session language server (basedpyright or pylsp, bash-language-server), configured in the `lsp` section. Off: the built-in jedi completions. | When on, code editors connect to the language server over a WebSocket. |

The exact descriptions are in the [configuration reference](/docs/reference/configuration#features).

## Switching flags at runtime

Admins with `admin:config` can switch flags on and off without a restart, in the admin panel's [**Features** tab](/docs/admin/admin-panel#features). Each change asks for a reason.

The value a flag has is decided in this order, highest first:

1. **Locked**: a flag matching `features_locked` (fnmatch patterns such as `["ai", "deploy"]`; `["*"]` locks every flag), or set through the environment (`AUTOAIR_FEATURES__<NAME>`, the `.env` file or the secrets directory), keeps its configured value. The panel shows it as locked and `PUT /api/admin/features/<name>` answers `409`.
2. **Runtime override**: the value set in the admin panel.
3. **Configured value**: configuration files, then the default.

The policy switch `allow_python_code` still applies after that: an override cannot switch Python code on while `allow_python_code` is false (see [Python code and the policy switch](#python-code-and-the-policy-switch)). At least one of `visual_editor` and `code_editor` must stay on (`422`).

```yaml
# Admins may switch everything at runtime except AI and deploys.
features_locked: ["ai", "deploy"]
features_override_ttl: 5   # seconds before other replicas pick up a change
```

* **Replicas**: overrides are stored in the state backend (`database.backend`): `feature-overrides.json` in the state folder, or a row of the SQLite/PostgreSQL database. Every replica sharing the backend applies them within `features_override_ttl` seconds; the replica that made the change applies it at once.
* **Request-time gating**: every route checks the flag per request, including the `/collab` WebSocket, `/mcp` and the AI routes. They are mounted at start-up unless the flag is locked off, so switching a flag on at runtime works without a restart. MCP tools check the runtime value too.
* **UI**: `GET /api/meta` reports the new value at once; the UI re-reads it when the browser window regains focus and every minute. A change of `visual_editor` or `code_editor` keeps the open DAG: the builder switches to the editor that is left, shows a warning, and keeps an unapplied code draft.
* **Audit**: each change is recorded as `feature_override` with the user, the old and new effective value, the old and new override and the reason.

The API: `GET /api/admin/features` (needs `admin:panel`) lists each flag with its configured value, override, effective value, lock and the last change; `PUT /api/admin/features/<name>` with `{"value": true | false | null, "reason": "…"}` sets or (`null`) removes an override (needs `admin:config`).

## Python code and the policy switch

`python_code` is the only flag with a policy setting next to it: `allow_python_code` (`AUTOAIR_ALLOW_PYTHON_CODE`, default `true`). Python code is allowed only when both are true, so `AUTOAIR_ALLOW_PYTHON_CODE=false` also makes the effective `python_code` flag reported in `GET /api/meta` false, whatever the flag or a runtime override says.

`AUTOAIR_AI__ENABLED=false` is not a feature flag: it keeps AI's rules-only features (leak scan, review, optimise, search) and answers `503` for LLM features, while `features.ai=false` removes AI entirely.

## Examples

A locked-down production builder: no Python code, no imports, deploys only through reviewed templates.

```bash
AUTOAIR_FEATURES__PYTHON_CODE=false
AUTOAIR_FEATURES__IMPORTS=false
AUTOAIR_FEATURES__MARKETPLACE=false
AUTOAIR_FEATURES__TEST_RUNS=false
```

See [Code mode](/docs/user-guide/code-mode#editor-modes) for what each combination of `visual_editor` and `code_editor` shows.

```yaml
# /etc/autoair/autoair.yaml: code-first team, no canvas
features:
  visual_editor: false
  code_editor: true
  ide_language_server: true
```

`autoair local` applies its own preset: `features.runs`, `features.test_runs` and `features.airflow` are off because there is no Airflow server.

## Checking the effective flags

* `autoair config show | grep '^features\.'` prints every flag and its source.
* The **Features** summary at the top of the [configuration page](/docs/admin/configuration#the-configuration-page-in-the-ui), and the **Feature flags** card on the admin panel's **Overview** tab, show the effective values after `allow_python_code` and what is installed are applied.
* `GET /api/meta` returns `features` (effective flags) for scripts.
