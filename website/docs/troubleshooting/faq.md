---
title: Troubleshooting FAQ
description: Causes and fixes for common AutoAir installation, startup, sign-in, validation, deploy and multi-replica problems, keyed by the messages you see.
sidebar_label: FAQ
---

Each entry starts with the symptom or the exact message, then the cause and the fix. For AI-specific problems see [AI troubleshooting](/docs/ai/troubleshooting).

When reporting a problem, include the AutoAir version (`autoair --version`, the admin panel or `pip show autoair`), the Airflow version, the run mode, and the output of `autoair config show --changed` (it masks secrets).

## Installation

### "needs an HTTP server, which comes with the 'standalone' extra" when running `autoair local` or `autoair serve`

The HTTP server (uvicorn) is in the `standalone` extra, not in the core package; the command exits with code 1 and names the extras to install (older versions failed with `ModuleNotFoundError: No module named 'uvicorn'`). Install `autoair[standalone]`, or with uv run `uvx --from 'autoair[standalone,airflow]' autoair local`. The plugin does not need it: Airflow's API server serves AutoAir. See [Extras](/docs/installation/extras).

### The palette is empty or has no operators I expect

The operator catalog is built from the packages installed **in AutoAir's own Python environment**:

- local mode without Airflow or providers installed has no operators: add the `airflow` extra and providers (`--with apache-airflow-providers-amazon`);
- a standalone server needs the same providers as the Airflow cluster;
- in plugin mode, check that the provider is in the Airflow image used by the API server;
- `AUTOAIR_OPERATOR_ALLOW`, `AUTOAIR_OPERATOR_DENY` and `AUTOAIR_FORBIDDEN_OPERATORS` may hide operators;
- custom operators need `AUTOAIR_CUSTOM_OPERATOR_PATHS`, `AUTOAIR_EXTRA_OPERATOR_PACKAGES` or `AUTOAIR_EXTRA_OPERATOR_DISTRIBUTIONS` unless they live in Airflow's plugins or DAG folder (`discover_airflow_folders`).

See [Catalog sources](/docs/extending/catalog-sources).

### The log says `catalog file ... is missing or stale; ignoring it`, and startup is slow

`AUTOAIR_CATALOG_FILE` points at a catalog built by another AutoAir version or for other installed providers. AutoAir ignores it and builds the catalog at startup. Rebuild the file in the image (`autoair catalog build --output <the AUTOAIR_CATALOG_FILE path>`) after every AutoAir or provider upgrade. See [Upgrading](/docs/installation/upgrading#rebuild-the-operator-catalog).

### pip upgraded or downgraded Airflow when I installed AutoAir

Pin Airflow in the same command: `pip install "autoair[<extras>]" "apache-airflow==$(airflow version)"`. Never install the `airflow` extra into an Airflow image.

### `/ui/` shows "Frontend bundle not built" (HTTP 503)

The wheel or checkout has no built UI. Run `npm ci && npm run build` in `frontend/` (Node.js 22), or `make build` for a wheel. Released wheels contain the UI.

### YAML configuration file fails with "YAML configuration files need PyYAML"

Install `autoair[yaml]`. JSON, INI and Python configuration files need nothing extra.

## Startup and configuration

### The CLI exits with code 2 and prints a settings error

A setting has an invalid value (wrong type, unknown enum value, malformed JSON in a list or map variable). Lists and maps in environment variables are JSON: `AUTOAIR_ENVIRONMENTS='["dev","prod"]'`. The message names the setting. See [Configuration](/docs/admin/configuration).

### `refusing to serve 0.0.0.0 without --token: local mode has no other authentication`

`autoair local` has no users; binding a non-loopback address requires `--token` or `AUTOAIR_LOCAL__TOKEN`. Keep `--host 127.0.0.1`, or set a token.

### `the <name> feature is disabled (AUTOAIR_FEATURES__<NAME>)`

A CLI command needs a feature that is switched off, for example imports. Enable it with the named variable, or ask an administrator. See [Feature flags](/docs/admin/feature-flags).

### MCP endpoint missing; log says `MCP endpoint disabled: ... (install autoair[mcp])`

Install the `mcp` extra and keep `AUTOAIR_FEATURES__MCP=true`. See [MCP](/docs/ai/mcp).

### `AUTOAIR_OTEL_ENABLED is set but opentelemetry is not installed`

Install `autoair[otel]`. Spans are exported only when `OTEL_EXPORTER_OTLP_ENDPOINT` (or `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`) is set. See [Observability](/docs/admin/observability).

## Plugin mode

### "DAG Builder" does not appear in the Airflow UI

1. Airflow 3.0 has no plugin menu entries: open `/autoair/ui/` instead.
2. `airflow plugins` must list `autoair` in the API server's environment. If it does not, the package is not installed there (check the image the API server pod actually runs).
3. Search the API server log for `AutoAir: invalid configuration` or `AutoAir plugin failed to initialise`. The plugin then registers nothing but Airflow keeps running; the log line contains the reason.
4. `AUTOAIR_PLUGIN__NAV_CATEGORY` may have moved the entry to another menu.

### The **AutoAir** tab is missing on DAG pages

It needs Airflow 3.1 or newer and `AUTOAIR_PLUGIN__DAG_TAB=true` (default). On Airflow 3.4 and newer, `AUTOAIR_PLUGIN__DAG_TAB_TAGS` shows it only on DAGs carrying one of the listed tags. If the tab is missing everywhere with tags set, run `airflow plugins --verbose` on the API server: Airflow logs there why it changed or dropped a plugin view.

### `/autoair/api/...` returns 401 or 403

401: the Airflow session expired; sign in to Airflow again ("Your Airflow session has ended. Sign in to Airflow again to continue."). 403: the user lacks the Airflow or AutoAir permission for that action; the message names it, for example `<action> requires DAG edit permission`. See [IAM](/docs/admin/iam).

### FAB: users have no AutoAir permissions with the `custom_view` strategy

The permissions must exist in FAB's database. Run `autoair iam sync-fab` (with `--create-roles` to also create `AutoAir <Role>` roles) where Airflow runs, then assign the roles. `--create-roles` refuses to run with an empty `iam.role_prefix`.

### Keycloak: every request is denied, including Airflow's own API

Set the Keycloak client's authorization decision strategy to **Affirmative**. With the default **Unanimous**, overlapping role permissions deny everything.

## Standalone sign-in

### `Airflow bearer token required` (401)

The request had no token. In the UI, sign in; for API clients send `Authorization: Bearer <token>`.

### `Airflow rejected the token` or `Airflow token expired` (401)

The token is invalid for this Airflow, expired (`[api_auth] jwt_expiration_time`), or was issued by another Airflow (different `jwt_secret`). Get a new one from `POST <airflow>/auth/token`. Verified tokens are cached for `auth_cache_ttl` seconds (60).

### `cannot reach Airflow: ...` or `Airflow auth check failed (<status>)` (502)

AutoAir could not call `GET <airflow_url>/api/v2/dags?limit=1`. Check `AUTOAIR_AIRFLOW_URL` (it is the address AutoAir uses, not necessarily the browser's), DNS, firewalls, and TLS: a private CA needs `AUTOAIR_OUTBOUND__CA_BUNDLE`; a proxy needs `AUTOAIR_OUTBOUND__HTTPS_PROXY` / `NO_PROXY`. The timeout is `auth_timeout` (10 s).

### "Sign in with Airflow" does nothing or keeps waiting

The browser must be able to send Airflow's `_token` cookie to AutoAir: same host (any port), the same proxy, or a sibling sub-domain whose proxy widens the cookie domain. Otherwise use the password form or paste a token. Allow pop-ups ("The browser blocked the sign-in window"). Set `AUTOAIR_SSO__AIRFLOW_PUBLIC_URL` when browsers reach Airflow under another URL than `airflow_url`. See [SSO](/docs/admin/sso).

### `invalid username or password`, or `too many failed sign-in attempts; try again later` (429)

The password form forwards the credentials to Airflow's `/auth/token`. After `iam.login_max_failures` (5) failures within `iam.login_window_seconds` (300) per IP and per username, further attempts are refused until the window passes. Behind a proxy, set `server.forwarded_allow_ips` so the real client IP is used. `sign-in with a password is not enabled` means `AUTOAIR_IAM__LOGIN_ENABLED=false`.

### State-changing requests fail with a CSRF error when signed in through the cookie

Cookie sessions require the `X-AutoAir-CSRF` header and a same-origin (or `cors_origins`) `Origin`. The UI sends both; custom clients should use a bearer token instead. See [SSO](/docs/admin/sso).

### "Cannot reach the AutoAir API"

The UI cannot reach `/api` of the server that served it. Common causes: a reverse proxy that does not forward `/api` (or WebSockets for `/collab`), a wrong `server.public_url`, or a strict `server.content_security_policy`. See [Reverse proxy](/docs/deployment/standalone-proxy) and [TLS and headers](/docs/admin/tls-and-headers).

## Validation

### Validate in Airflow fails with an import error that Airflow itself would not have

The import check runs in AutoAir's environment. Install the same providers and Airflow version there as in the cluster (standalone), or rely on the plugin, which runs inside Airflow. `AUTOAIR_TARGET_AIRFLOW_VERSION` aligns version gates with the cluster when the versions differ.

### The import check is reported as skipped on deploy

Callers without the `code:execute` permission do not execute the DAG file on the server (`iam.import_check_requires_code_execute`). An Author or Admin deploy runs it. In local mode the check runs only when Airflow is installed.

### A required field is flagged although I set it in Jinja

Required parameters must have a value in the spec; a Jinja template counts as a value. If the field is a non-templated parameter, Airflow passes the text literally ("This parameter is not a template field").

### "Unknown timezone"

DAG timezones must be IANA names such as `Europe/Istanbul` or `UTC`.

## Deploy

### The Deploy button is disabled

"Add at least one task to deploy": the DAG has no tasks. "Fix N problems to deploy": open the Problems tab (clicking the button does). Otherwise the user lacks the deploy permission, or `AUTOAIR_FEATURES__DEPLOY=false`.

### "Someone else changed this DAG"

Another user deployed this DAG after you opened it (ETag mismatch). Use the merge dialog: **Apply merged & redeploy**, or **Load merged (don't deploy)** to review first. In Git mode a moved branch reports `branch '<name>' moved; reload and retry`.

### `<file> was modified outside AutoAir (ejected)`

The generated file in the deploy target differs from what AutoAir last wrote: someone edited it by hand. Copy the manual change into the spec (or import the file), or force the deploy to overwrite it. See [Deploy](/docs/user-guide/deploy).

### `<file> exists and was not created by AutoAir; refusing to overwrite it`

A hand-written DAG with the same file name exists. Choose another DAG id, or import that DAG into AutoAir and remove the original.

### The DAG was deployed but does not appear in Airflow

- The deploy target is not where Airflow reads DAGs. In standalone mode `--dags-folder` must be the dag-processor's folder (shared volume), or use Git/object storage. Open **Versions & Git → Deploy target…**: the storage chip turns orange when Airflow reads from Git but AutoAir writes locally.
- Git pull-request mode: the DAG appears only after the pull request is merged and the bundle refreshes (`refresh_interval`).
- Airflow has an import error for the file: check Airflow's DAG import errors.
- The dag-processor has not parsed it yet (`[dag_processor] refresh_interval`).
- New DAGs are paused by default; unpause them in Airflow or with the toolbar chip.

### Git deploy errors

| Message | Cause |
| --- | --- |
| `git push failed: ...`, `git <command> failed: ...` | Credentials or network for `provider=local`; credentials come from `AUTOAIR_STORAGE__GIT__TOKEN`, `AUTOAIR_STORAGE__GIT__SSH_KEY_FILE` or the Airflow connection `storage.git.conn_id` |
| `branch '<name>' does not exist` | `AUTOAIR_STORAGE__GIT__BASE_BRANCH` or an environment branch in `AUTOAIR_STORAGE__GIT__ENVIRONMENTS` is wrong |
| `use_airflow_bundle is set but no GitDagBundle is configured` | `AUTOAIR_STORAGE__GIT__USE_AIRFLOW_BUNDLE=true` but Airflow's `dag_bundle_config_list` has no `GitDagBundle` (or AutoAir cannot read Airflow's configuration in standalone mode); set the repository explicitly |
| `pushing directly needs the AutoAir permission 'deploy:direct_push'` | Only `iam.direct_push_roles` (default Admin) may skip the pull request |
| `no branch configured for environment '<env>'` | Add the environment to `AUTOAIR_STORAGE__GIT__ENVIRONMENTS` |
| `provider=gitea needs storage.git.api_url (https://<host>/api/v1) for an SSH remote` | `AUTOAIR_STORAGE__GIT__PROVIDER=gitea` with an `ssh://` or `git@host:owner/repo` remote: set `AUTOAIR_STORAGE__GIT__API_URL` to the Gitea or Forgejo API base |
| Authentication fails after a token was rotated in Airflow | Nothing to do in most cases: credentials read from an Airflow connection are cached for `storage.git.credentials_ttl` seconds (default 60), re-read at once after an authentication failure, and the operation is retried. If the error persists, the new token itself lacks access |

See [Git bundles](/docs/deployment/git-bundles).

### `the git deploy target is unavailable: ...` and `/api/ready` returns 503

AutoAir could not build the deploy target at startup (typically the Airflow connection in `storage.git.conn_id` does not exist yet, or AutoAir cannot read the GitDagBundle configuration) and started degraded. The log shows `deploy target (storage.mode=...) unavailable, starting degraded: <reason>`. Fix the cause (create the connection, correct the repository settings); AutoAir retries on the next deploy and on reads at most every 10 seconds, so no restart is needed. See [Observability](/docs/admin/observability#degraded-startup).

### `storage mode 'object' needs object_url`

Set `AUTOAIR_STORAGE__OBJECT_URL` (for example `s3://bucket/dags/autoair`) and install the fsspec driver (`s3fs`, `gcsfs`). See [Object storage](/docs/deployment/object-storage).

### `credentials are not stored here: ...` when saving the deploy target in the UI

The deploy target dialog never stores tokens or keys. Put credentials in an Airflow connection and select it in the dialog.

### The deploy returns 202 and the DAG is not live

Four-eyes approvals are on (`iam.require_approval`): the deploy was stored as a proposal. Another user with the Reviewer role must approve that version ("this proposal needs an approval by another user than its author"). See [Reviews and approvals](/docs/user-guide/reviews-and-approvals).

## Runs, logs and pickers

### Run, Trigger, Test and the connection pickers are missing

Local mode switches them off (`features.runs`, `features.test_runs`, `features.airflow`). In other modes check those feature flags and the user's Operator/Deployer permissions.

### "Deploy the DAG first: this needs it in Airflow."

Runs, logs and the Airflow tab work on DAGs Airflow knows. Deploy, wait for the dag-processor, then retry.

### Pickers show "not found in Airflow's metastore" for connections that exist

Your Airflow resolves connections from a secrets backend the REST API cannot list. Set `AUTOAIR_AIRFLOW_SECRETS_BACKEND=true`.

## Collaboration and replicas

### Collaboration does not connect

- The `collab` extra is missing, or `AUTOAIR_FEATURES__COLLAB=false` (`collaboration is disabled`, 404).
- The proxy does not forward WebSocket upgrades on `/collab/` (`/autoair/collab/` in plugin mode).
- The WebSocket handshake is refused (403) without a valid ticket: viewers cannot co-edit; tickets need DAG edit permission and expire after `collab_ticket_ttl` (600 s).
- With several standalone workers or replicas, every process needs the same `AUTOAIR_COLLAB_SECRET`, and rooms must be shared (`AUTOAIR_COLLAB__SYNC=database` with a SQL backend, or `redis` with `AUTOAIR_COLLAB__REDIS_URL`), or sticky sessions on `/collab/<dag_id>`.

### Replicas disagree (templates, deploy target, approvals)

With the file backend each replica has its own state folder. Use PostgreSQL (`AUTOAIR_DATABASE__BACKEND=postgresql`) or shared fsspec URLs. See [Scaling](/docs/admin/scaling).

### `database.backend is 'file': nothing to migrate`

`autoair db upgrade` only applies to `sqlite` and `postgresql`. Make sure the command sees the server's `database` settings (`AUTOAIR_DATABASE__BACKEND`, `AUTOAIR_DATABASE__URL`, `AUTOAIR_DATABASE__PASSWORD`).

### `cannot load the postgresql driver (...): pip install 'autoair[postgres]'`

Install the `postgres` extra. The URL scheme should be `postgresql+psycopg://`.

## Still stuck

- Raise logging: `AUTOAIR_LOGGING__LEVEL=DEBUG`, or per logger `AUTOAIR_LOGGING__LEVELS='{"autoair.storage": "DEBUG"}'`.
- Check `/api/ready` (`/autoair/api/ready` in plugin mode): it reports whether the catalog is loaded and the deploy target (and Airflow, with `server.ready_check_airflow`) is reachable, including a target that could not be built at startup.
- The audit trail records denials (`iam.audit_denials`) and deploys: [Audit](/docs/admin/audit).
- Report security issues privately: [Reporting](/docs/security/reporting).
