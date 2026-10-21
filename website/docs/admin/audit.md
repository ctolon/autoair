---
title: Audit
description: What AutoAir records in its audit trail (deploys, reviews, permission denials, administrative actions) and its AI audit log, the audit sinks (file, database, log, OpenTelemetry), how to browse and export records, and retention.
---

AutoAir keeps two append-only logs:

* the **audit trail**: who did what to which DAG and to AutoAir itself (deploys, deletes, promotions, proposals, reviews, permission denials, template and administrative changes);
* the **AI audit log**: every AI request, with user, model, tokens, cost, tools and outcome.

Both are written by every replica and stored in the [state store](/docs/admin/database). Git deploys additionally carry the deploy context in their commits and pull requests (see [Deploy governance](/docs/admin/configuration#deploy-governance)), so the git history is a second, independent audit of what went live.

## Audit trail

### Record format

Every record has:

| Field | Description |
|---|---|
| `timestamp` | UTC, ISO 8601 |
| `action` | What happened (see below) |
| `user` | The Airflow user name, from the authenticated token (`null` for anonymous callers) |
| `dag_id` | The DAG, when the action concerns one |
| `details` | Action-specific fields |

```json
{"timestamp": "2026-10-19T08:41:12.512+00:00", "action": "permission_denied", "user": "bob",
 "dag_id": "etl_daily",
 "details": {"permission": "deploy", "method": "PUT", "path": "/api/specs/etl_daily", "roles": ["Author"]}}
```

### Actions

| Action | Recorded when | Notable details |
|---|---|---|
| `deploy` | A spec is written live, a pull request is opened or updated, or a direct push happens | The deploy context (below), `write` (`deploy`, `pull_request` or `direct_push`), `direct_push`, target, credentials used (`shared`/`per_user` and the connection id), PR URL or commit, `approved_by`, `proposed_by` |
| `propose` | An unapproved live deploy is stored as a deploy proposal (`iam.require_approval`) | Deploy context, proposal `etag`, `base_etag`, message |
| `review` | A reviewer approves, requests changes or comments | `etag`, `verdict`, `comment`, whether it was a proposal, `proposed_by`, reviewer name and e-mail |
| `delete` | A spec and its DAG files are deleted | Deploy context, target, credentials used |
| `promote` | A DAG is promoted between environments | Deploy context, `from_env`, `to_env`, `pr_url`, `branch`, `version` |
| `permission_denied` | A request is refused with `403` by IAM | `permission`, `method`, `path`, `roles` (switch off with `iam.audit_denials=false`) |
| `template_save`, `template_delete` | A template is saved or deleted (builder or admin panel) | `template`, `kind` |
| `db_upgrade` | Migrations are applied from the admin panel | `applied` versions |
| `cache_clear` | A cache is rebuilt or cleared from the admin panel | `cache` |
| `feature_override` | A feature flag is switched (or its override removed) at runtime from the admin panel | `feature`, `old`, `new` (effective values), `old_override`, `new_override`, `reason` |
| `ai_knowledge_put`, `ai_knowledge_toggle`, `ai_knowledge_reset` | AI knowledge-base documents are overridden, toggled or reset | `doc`, `enabled` |
| `ai_rules_put`, `ai_rules_delete` | AI project rules are saved or deleted | `scope` |
| `ai_mcp_server_put`, `ai_mcp_server_delete` | MCP servers are added, changed or deleted in the admin panel | `server`, `url`, `tools` (never header values) |
| `assistant_proposal_<decision>`, `troubleshoot_<decision>`, `troubleshoot_applied` | A user accepts, rejects or applies a change proposed by an AI assistant or a troubleshooting analysis | model, tools, the DAG and run ids involved |

**Deploy context.** Deploys, deletes, promotions and proposals carry the same context as the commit message: `user_name`, `user_email`, `autoair_version`, `environment`, `etag_before`, `etag_after`, `validation` (`ok`, error, warning and info counts, import-check result), `approved_by`, `reviewed_by`, `proposed_by`, `link` (needs `server.public_url`), `target` and `git_mode`.

### Where it is stored: sinks

`audit.sinks` (`AUTOAIR_AUDIT__SINKS`) lists where each record goes. Unset (the default), records go to the state store (`db` with a SQL backend, otherwise `file`) **and** to `log`. See [Audit sinks](#audit-sinks) for the options.

| Store | Location |
|---|---|
| `file` | JSON lines under `AUTOAIR_AUDIT_URL` (default `<state>/audit`), one file per day and process: `<day>/<host>-<pid>.jsonl`. Replicas never write the same file, so a shared volume or object store (`s3://...`) is safe |
| `db` | The `<prefix>audit` table of the SQL state store, indexed by DAG, user and action with time |

Writing the audit record never breaks the request: if a store fails, the error is logged (`failed to write audit record`) and the action still completes. Monitor for that log line.

### Browsing and exporting

* **Per DAG**: the builder's audit tab shows the DAG's records (`GET /api/specs/{dag_id}/audit`, needs `audit:read` and Airflow read access to the DAG).
* **Whole trail**: the admin panel's **Audit** tab filters by user, action, DAG and date range and exports CSV (`GET /api/admin/audit`, `GET /api/admin/audit.csv`, needs `admin:panel`). The CSV export neutralises cells that look like spreadsheet formulas. See [Admin panel](/docs/admin/admin-panel#audit).
* **Directly**: with the file backend the JSON lines can be read with any tool (`jq`, a log shipper); with a SQL backend query the `audit` table.

```bash
# Who deployed etl_daily in October (file backend)
cat /opt/airflow/dags/autoair/.autoair/audit/2026-10-*/*.jsonl \
  | jq -c 'select(.action=="deploy" and .dag_id=="etl_daily") | {timestamp, user, write: .details.write}'

# Recent denials via the API
curl -s -H "Authorization: Bearer $TOKEN" \
  "https://autoair.acme.io/api/admin/audit?action=permission_denied&limit=50"
```

### Retention

The audit trail has **no automatic retention**: records are kept until you remove them. Prune according to your compliance policy:

* file backend: delete whole day folders older than the retention period (`<state>/audit/<YYYY-MM-DD>/`);
* SQL backends: delete rows by `timestamp` (an ISO 8601 string, so lexical comparison works), for example `DELETE FROM autoair_audit WHERE timestamp < '2025-10-01'`.

Export before pruning if records must be archived. The admin panel's **Users** tab reads the most recent records to show users' last actions; it is unaffected by older records being removed.

## AI audit log

With `ai.audit.enabled=true` (default), every AI request is recorded in the background (it never slows the request):

| Field | Description |
|---|---|
| `timestamp`, `user`, `role` | Who asked, and their AI role (viewer, editor, admin) |
| `feature` | The AI feature: `generate`, `explain`, `describe`, `explain_dag`, `explain_code`, `task_code`, `review`, `leaks`, `optimize`, `fix`, `ask`, `agent`, `model_test`, `deploy_message`, `upgrade`, `troubleshoot`; `write:<kind>` for the code writers (`task_body`, `inline_function`, `bash`, `sql`, `operator_config`, `custom_operator`, `dag_spec`); `assistant:developer` and `assistant:admin`; decisions as `assistant:proposal_approve`, `assistant:proposal_reject`, `assistant:proposal_applied`, `troubleshoot:approve`, `troubleshoot:reject` and `troubleshoot:applied` |
| `provider`, `model`, `llm_source` | Which model answered; `llm_source` is `central`, `user`, `user-connection` or `service` |
| `usage` | Input and output tokens, and `cost_usd` when `ai.pricing` covers the model |
| `tools`, `sources`, `guardrails` | Tools the agent called, sources it used, guardrails that fired |
| `duration_ms`, `outcome`, `error` | Result |
| `prompt` | Only with `ai.audit.include_prompts=true`, redacted. Prompts may still contain sensitive data: enable deliberately |
| `detail` | Feature specifics (thread or analysis id, DAG and run ids, proposal decisions; never secrets) |

Storage: `<state>/ai-audit/<day>/<host>-<pid>.jsonl` (`ai.audit.url`) with the file backend, or the `<prefix>ai_audit` table (with token and cost columns) with a SQL backend.

Reading: `GET /api/ai/audit?limit=&user=` (needs `ai:admin`), and the admin panel's **AI usage** tab, which aggregates requests, tokens, cost and errors per user and model.

### Retention

The AI audit log is pruned automatically, once per day and process, by whole days, oldest first:

| Setting | Default | Effect |
|---|---|---|
| `ai.audit.retention_days` | `90` | Delete days older than this (`0` keeps them forever) |
| `ai.audit.max_total_mb` | `1024` | Delete the oldest days while the log is larger than this (`0` = no size limit) |

With PostgreSQL, only the replica holding an advisory lock prunes; with files, every process may prune (deletes are idempotent).

## Audit sinks

Airflow 3's REST API cannot write event-log entries, so AutoAir keeps its own trail and can emit it to several sinks at once:

| Sink | Writes | Notes |
|---|---|---|
| `file` | JSON lines at `audit_url` | Queryable by the UI when there is no `db` sink |
| `db` | The `<prefix>audit` table | Needs a SQL `database.backend`; otherwise ignored with the warning `audit sink 'db' ignored: no database configured (database.backend)` |
| `log` | One `INFO` line per record on the `autoair.audit` logger: the message is `audit {json}`, and with `AUTOAIR_LOGGING__FORMAT=json` the record is also attached as a structured `audit` field | For log pipelines and SIEMs; see [Observability](/docs/admin/observability#logging) |
| `otel` | One OpenTelemetry span per record, named `audit <action>`, with the attributes `autoair.audit.action`, `autoair.audit.timestamp`, `autoair.audit.user`, `autoair.audit.dag_id` and `autoair.audit.details` (JSON) | Needs `autoair[otel]` and a tracer provider (`AUTOAIR_OTEL_ENABLED=true` and the `OTEL_*` exporter variables, or Airflow's provider in plugin mode). Without the package, a warning is logged once and nothing is emitted |

```yaml
audit:
  sinks: [db, log, otel]
```

```bash
AUTOAIR_AUDIT__SINKS='["file","log"]'
```

Every record is sent to all configured sinks, in this order: the `log` line, the `otel` span, then the repositories (`db` and `file`). A failing repository is logged as `failed to write audit record`, a failing span export as `failed to emit the audit record to OpenTelemetry`; in both cases the other sinks still receive the record and the request completes.

The audit views of the UI and API (the builder's audit tab, the admin panel's **Audit** tab, CSV export, the admin panel's **Users** tab) read the `db` sink when it is configured, otherwise `file`. With both `db` and `file`, records are written to both and the views read `db`. With neither (for example `["log"]` only), records are emitted but those views are empty. Duplicate entries in the list are ignored. An empty list (`[]`) disables the audit trail entirely; the AI audit log is configured separately (`ai.audit.*`).

## Related settings

| Setting | Default | Description |
|---|---|---|
| `audit.sinks` | state store + `log` | `file`, `db`, `log`, `otel` |
| `audit_url` | `<state>/audit` | Location of the `file` sink (fsspec URL) |
| `iam.audit_denials` | `true` | Record `permission_denied` |
| `ai.audit.enabled`, `ai.audit.url`, `ai.audit.include_prompts` | `true`, `<state>/ai-audit`, `false` | AI audit log |
| `ai.audit.retention_days`, `ai.audit.max_total_mb` | `90`, `1024` | AI audit retention |

See also [IAM](/docs/admin/iam), [Backup and restore](/docs/admin/backup-restore) and [Security overview](/docs/security/overview).
