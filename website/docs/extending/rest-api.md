---
title: REST API
description: Authenticate against AutoAir's REST API, find the base path for plugin and standalone mode, get the OpenAPI document and automate validation, deploys and runs with curl.
sidebar_label: REST API
---

Everything the AutoAir UI does goes through a JSON REST API: reading the operator catalog, validating and generating code, saving and deploying specs, history and diffs, reviews, test runs, Airflow pickers and run operations, templates, the marketplace, AI and administration. You can call the same API from CI pipelines, scripts or other tools.

The full, generated reference of every operation and model is the [API reference](/api) page.

## Base paths

| Mode | API base | Example |
|---|---|---|
| Plugin (inside Airflow) | `https://<airflow-host>/autoair/api` | `https://airflow.example.com/autoair/api/catalog/stats` |
| Standalone (`autoair serve`) | `https://<autoair-host>/api` | `https://autoair.example.com/api/catalog/stats` |
| Local (`autoair local`) | `http://127.0.0.1:8765/api` | |

The paths below are relative to the API base. The examples use:

```bash
API=https://airflow.example.com/autoair/api     # or https://autoair.example.com/api
```

## Authentication

AutoAir has no user database: **Airflow authenticates every request**. The credential is always an Airflow access token (a JWT) or Airflow's session cookie.

| Mode | Accepted credentials |
|---|---|
| Plugin | `Authorization: Bearer <airflow token>`, or Airflow's `_token` session cookie. Airflow's auth manager validates it on every route |
| Standalone | `Authorization: Bearer <airflow token>`, verified against Airflow (`GET /api/v2/dags?limit=1`) and cached for `auth_cache_ttl` seconds (60, never past the token's expiry); or Airflow's session cookie when the browser sends it |
| Local | No authentication on `127.0.0.1`, or the token set with `autoair local --token` / `AUTOAIR_LOCAL__TOKEN` |

Get a token from Airflow (the auth manager must support username/password tokens, as SimpleAuthManager, FAB and Keycloak do):

```bash
TOKEN=$(curl -s -X POST https://airflow.example.com/auth/token \
  -H 'Content-Type: application/json' \
  -d '{"username": "ci-deployer", "password": "..."}' | jq -r .access_token)
```

Standalone servers also offer `POST /api/auth/login` (when `iam.login_enabled=true`), which proxies the same exchange once and returns `{"access_token": ..., "user": ..., "expires_at": ...}`. Failed attempts are rate limited per IP and user (429 with `Retry-After`).

Then:

```bash
curl -s -H "Authorization: Bearer $TOKEN" "$API/me" | jq
```

`GET /me` returns the caller's AutoAir roles and permissions; it is the quickest way to check what a service account may do.

**Authorization.** Every route requires an AutoAir permission (the [endpoint groups](#endpoint-groups) table names it) derived from the caller's Airflow roles, and routes about one DAG additionally require Airflow's own access to that DAG. Calls that AutoAir forwards to Airflow use the caller's token, so Airflow's RBAC applies to them too. See [IAM](/docs/admin/iam) for the permission of each route.

**CSRF.** In standalone mode, state-changing requests (`POST`, `PUT`, `PATCH`, `DELETE`) authenticated by the session **cookie** must carry the `X-AutoAir-CSRF` header and a same-origin `Origin`. Requests with a bearer token are exempt. Automation should always use bearer tokens.

**Unauthenticated routes:** `/health`, `/ready`, `/metrics`, `/auth/config`, `/auth/session` and the public icon images (`/icons/<id>`).

## OpenAPI document

The OpenAPI document is generated from the code:

```bash
uv run autoair export-openapi --output openapi.json    # from a checkout
autoair export-openapi --output openapi.json           # from an installation
```

The default output path is `frontend/openapi.json`, which the frontend's TypeScript client is generated from (`cd frontend && npm run codegen`). The documentation site renders the same document on the [API reference](/api) page and serves it as `/openapi.json`. Use it to generate clients in other languages.

A running server also publishes the document of exactly the version it runs, together with FastAPI's interactive Swagger UI, under the API base path: `/api/openapi.json` and `/api/docs` in standalone and local mode, `/autoair/api/openapi.json` and `/autoair/api/docs` in the plugin. Prefer these over the site's copy when the server and the docs may differ in version.

## Conventions

- **JSON in, JSON out.** Request and response bodies are JSON; models forbid unknown fields, so typos are rejected with 422.
- **Errors** use FastAPI's shape: `{"detail": "..."}` (or a list of validation errors for 422).
- **Feature flags.** Routes of a disabled feature (`features.*`) answer 404.
- **Rate limits.** Expensive groups (`validate`, `import`, `ai`, `test_runs`, `deploy`, `catalog_refresh`) are limited per user (`limits.rate_limits`); over the limit the API answers 429 with `Retry-After`.
- **Body size.** Requests above `server.max_body_bytes` (16 MiB) get 413.
- **Optimistic locking.** `GET /specs/{dag_id}` returns the spec's `etag` (also as the `ETag` header). Send it back as `base_etag` (or `If-Match`) when deploying; if someone else deployed in between, the deploy is not applied and returns `status: "conflict"` with the current version.

| Status | Meaning |
|---|---|
| 200 | Success. Deploys also return 200 for `status: "invalid"`, `"conflict"` and `"ejected"`; check `deployed` |
| 202 | Deploy stored as a proposal awaiting approval (`status: "pending_approval"`, `iam.require_approval`) |
| 400 | Bad request (unknown environment, path and body `dag_id` differ, storage refused the write) |
| 401 / 403 | Not signed in / missing permission or DAG access |
| 404 | Unknown object, or the feature is disabled |
| 409 | Storage conflict that cannot be expressed as a deploy result |
| 422 | Request does not match the schema |
| 429 | Rate limited |
| 502 / 503 | Airflow or an upstream service unreachable; AI not configured |

## Endpoint groups

| Group | Main routes | Permission |
|---|---|---|
| Meta | `GET /meta`, `GET /me` | signed in |
| Operations | `GET /health`, `GET /ready`, `GET /metrics` | none |
| Catalog | `GET /catalog/operators`, `GET /catalog/operators/{fqn}`, `GET /catalog/stats`, `GET /catalog/node-kinds`, `GET /catalog/notifiers`, `GET /catalog/decorators`, `GET /catalog/connection-types`, `POST /catalog/refresh` | `builder:use`; refresh `catalog:refresh` |
| Tools | `POST /codegen`, `POST /validate` (`?import_check=true`, `?environment=`), `POST /export/dag-factory`, `GET /schema/spec` | `builder:use`; import check `code:execute` |
| Rules-only checks | `POST /review/rules` (best-practice review), `POST /lint/secrets` (leak and secret scan); deterministic, available without the AI extra or `features.ai` | `builder:use` |
| Specs | `GET /specs`, `GET /specs/{dag_id}`, `PUT /specs/{dag_id}` (deploy), `POST /specs/{dag_id}/deploy-preview`, `DELETE /specs/{dag_id}`, `GET /specs/{dag_id}/audit`, `GET /specs/{dag_id}/reviews`, `POST /specs/{dag_id}/reviews`, `GET /specs/{dag_id}/proposals`, `GET /specs/{dag_id}/proposals/{etag}`, `POST /specs/{dag_id}/proposals/{etag}/deploy` | `spec:read`, `spec:write`, `deploy`, `spec:delete`, `review`, `audit:read` |
| Versions | `GET /vcs/status`, `GET /vcs/{dag_id}/history`, `GET /vcs/{dag_id}/versions/{version}`, `GET /vcs/{dag_id}/diff`, `POST /vcs/diff`, `POST /vcs/merge`, `POST /vcs/{dag_id}/promote` | `spec:read`; promote `promote` |
| Interchange | `POST /interchange/python/import`, `POST /interchange/yaml/import`, `POST /interchange/yaml/export`, `POST /interchange/spec/import`, `POST /interchange/airflow/{dag_id}/import` | `builder:use`; Python import `code:execute`; import from Airflow also `airflow:read` and `features.airflow` |
| Test runs | `POST /runner/test-task`, `POST /runner/test-dag` | `run:test` |
| Airflow | `GET /airflow/connections`, `/pools`, `/variables`, `/assets`, `/bundles`, `/import-errors`, `GET /airflow/dags/{dag_id}/status`, `/runs`, `/versions`, task logs and XCom, `POST /airflow/dags/{dag_id}/trigger`, `/runs/{run_id}/clear`, `/backfill`, `PUT /airflow/dags/{dag_id}/paused`, `POST /airflow/next-runs` | `airflow:read`; `run:trigger`, `run:manage`, `connection:write` |
| Templates | `GET /templates`, `GET /templates/{id}`, `POST /templates`, `DELETE /templates/{id}` | `builder:use`; writes `template:write` |
| Marketplace | `GET /marketplace`, `POST /marketplace/install` | `builder:use`; install `template:write` |
| Icons | `GET /icons`, `GET /icons/assignments`, `POST /icons`, `PUT /icons/assignments`, `DELETE /icons/{id}` | `builder:use`; writes `icon:write` |
| Deploy target | `GET /deploy-target`, `GET /deploy-target/git-credentials`, `POST /deploy-target/test`, `PUT /deploy-target`, `DELETE /deploy-target` | read `builder:use`; changes `admin:deploy_target` |
| Collaboration | `POST /collab/{dag_id}/ticket` | `spec:write` + DAG edit |
| Editor services | `POST /lsp/python/complete`, `/hover`, `/signatures`, `POST /lsp/shell/lint`, `POST /lsp/validate`, `GET /lsp/servers`, `POST /lsp/session` | `builder:use` |
| AI | `GET /ai/status`; `POST /ai/generate`, `/ai/task-code`, `/ai/explain`, `/ai/explain-dag`, `/ai/explain-code`, `/ai/explain-code/stream`, `/ai/describe`, `/ai/review`, `/ai/leaks`, `/ai/optimize`, `/ai/fix`, `/ai/apply`, `/ai/ask`, `/ai/ask/stream`, `/ai/upgrade`, `/ai/deploy-message`; `GET /ai/search`, `/ai/research/search`; `GET /ai/writers`, `POST /ai/write`, `/ai/write/stream`; `GET/PUT/DELETE /ai/me/model`, `POST /ai/me/model/test`; `GET /ai/assistant/info`, `POST /ai/assistant/chat/stream`, `GET /ai/assistant/threads`, `GET/PATCH/DELETE /ai/assistant/threads/{thread_id}`, `POST /ai/assistant/threads/{thread_id}/proposals/{proposal_id}/decision`; `GET /ai/troubleshoot/options`, `POST /ai/troubleshoot/stream`, `GET /ai/troubleshoot/analyses`, `GET /ai/troubleshoot/analyses/{analysis_id}`, `POST /ai/troubleshoot/analyses/{analysis_id}/decision`, `POST /ai/troubleshoot/analyses/{analysis_id}/applied`, `POST /ai/troubleshoot/analyses/{analysis_id}/actions/{action_id}`, `GET /ai/mcp/servers/{name}/tools`; administration: `GET /ai/knowledge`, `GET/PUT/DELETE /ai/knowledge/{doc_id}`, `POST /ai/knowledge/{doc_id}/enabled`, `GET /ai/audit`, `GET /ai/prompts`, `GET /ai/rules`, `GET /ai/rules/effective`, `PUT/DELETE /ai/rules/{scope}`. Streaming routes end in `/stream` and answer `text/event-stream` | `ai:use`; administration `ai:admin` |
| Configuration | `GET /config` | `admin:config` (and `expose_config`) |
| Admin panel | `GET /admin/overview`, `/audit`, `/users`, `/templates`, `/ai-usage`, `/proposals`, `/database`, `/caches`, `/mcp-servers` and their actions | admin permissions |
| Sign-in (standalone) | `POST /auth/login`, `POST /auth/logout`, `GET /auth/config`, `GET /auth/session`, `POST /auth/refresh`, `GET /auth/sso/login` | public / token |

## Examples

### Validate a spec in CI

```bash
curl -s -X POST "$API/validate" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  --data @dags/etl_daily.autoair.json | jq '{ok, problems: [.problems[] | select(.severity == "error")]}'
```

The body is a [DagSpec](/docs/extending/spec-format). The report is `{"ok": bool, "problems": [{"severity", "code", "message", "node_id", "field", "line", "column"}], "import_check": ...}`. Add `?import_check=true` to also import the generated file with a DagBag (needs `code:execute`).

### Generate the DAG file

```bash
curl -s -X POST "$API/codegen" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  --data @dags/etl_daily.autoair.json | jq -r .code > etl_daily.py
```

### Deploy

```bash
ETAG=$(curl -s -H "Authorization: Bearer $TOKEN" "$API/specs/etl_daily" | jq -r .etag)

jq --arg etag "$ETAG" '{spec: ., base_etag: $etag, environment: "prod",
                        message: "chore(dags): raise retries for etl_daily"}' \
   dags/etl_daily.autoair.json |
curl -s -X PUT "$API/specs/etl_daily" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' --data @- |
jq '{deployed, status, message, pr_url, version}'
```

`DeployRequest` fields: `spec` (required), `base_etag`, `environment`, `message`, `pr_description`, `git_conn_id` (per-user git credentials), `force` (overwrite hand-edited files), `direct_push` (git targets; needs `deploy:direct_push` to push in PR mode). For a DAG that does not exist yet, omit `base_etag`. The response's `status` is `deployed`, `invalid`, `conflict`, `ejected` or `pending_approval`; a git target in PR mode returns the pull request in `pr_url`.

Preview the commit message and pull-request text first with `POST /specs/{dag_id}/deploy-preview` (same body).

### Trigger a run and follow it

```bash
curl -s -X POST "$API/airflow/dags/etl_daily/trigger" \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"conf": {"full_refresh": true}, "logical_date": "now", "note": "triggered from CI"}'

curl -s -H "Authorization: Bearer $TOKEN" "$API/airflow/dags/etl_daily/runs" | jq '.[0] | {dag_run_id, state}'
```

`TriggerRequest`: `conf`, `unpause` (default `true`), `logical_date` (`"now"`, an ISO date-time, or `null` for a run without a logical date), `partition_key`, `note`.

### List stored specs and their history

```bash
curl -s -H "Authorization: Bearer $TOKEN" "$API/specs" | jq -r '.[].dag_id'
curl -s -H "Authorization: Bearer $TOKEN" "$API/vcs/etl_daily/history?limit=5" | jq
```

### Health checks

```bash
curl -s "$API/health"    # {"status": "ok", "version": "0.2.0"}
curl -s "$API/ready"     # {"ready": true, "checks": {"catalog": "ok (...)", "storage": "ok (git)"}}
```

## Clients

- **TypeScript.** The frontend generates its client from the OpenAPI document (`frontend/src/api/gen`) with `npm run codegen`.
- **Python.** Use `httpx` or `requests` with a bearer token; for typed models, the Pydantic models in `autoair.spec` (`DagSpec`) and `autoair.validation` are importable from the installed package.
- **AI agents.** Prefer the MCP server, which wraps the same operations with confirmations; see [MCP integration](/docs/extending/mcp-integration).

## Related pages

- [API reference](/api)
- [Spec format](/docs/extending/spec-format)
- [IAM](/docs/admin/iam)
- [CLI reference](/docs/reference/cli)
