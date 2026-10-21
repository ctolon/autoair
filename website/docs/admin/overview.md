---
title: Administration overview
sidebar_label: Overview
description: A map of the administration guide and a production checklist for running AutoAir as an Airflow plugin or a standalone server.
---

Administering AutoAir means configuring it, deciding who may do what, knowing where its state lives, and running it reliably. The admin pages assume AutoAir is installed (see [Installation](/docs/installation/overview)).

## How AutoAir is administered

* **Configuration** comes from environment variables (`AUTOAIR_*`), a `.env` file and configuration files (YAML, JSON, INI, Python). Nothing in the UI writes settings, except the deploy target dialog.
* **Identity** belongs to Airflow. AutoAir has no user database: Airflow authenticates every user, and AutoAir maps Airflow roles or permissions to its own roles.
* **DAGs** are stored in the deploy target Airflow reads (a folder, object storage or git). **AutoAir's own state** (templates, audit, approvals, AI data) is in files or a SQL database.
* **Operations** use standard tools: health and readiness endpoints, Prometheus metrics, JSON logs, OpenTelemetry, and an admin panel in the UI.

## Admin pages

| Page | Read it to |
|---|---|
| [Configuration](/docs/admin/configuration) | Understand configuration sources and precedence, write configuration files, handle secrets, inspect the effective configuration |
| [Feature flags](/docs/admin/feature-flags) | Switch features off (AI, MCP, co-editing, marketplace, templates, imports, test runs, runs, deploy, custom icons, Python code, Airflow calls, IAM, the visual editor, the code editor, inline code editing, the IDE language server) and see what disappears from the UI |
| [IAM](/docs/admin/iam) | Map Airflow roles to AutoAir roles, restrict deploys and direct pushes, require four-eyes approvals, register permissions in FAB |
| [SSO and sessions](/docs/admin/sso) | Sign users in with their Airflow session or password, configure cookies, CSRF and the sign-in rate limit |
| [TLS and HTTP headers](/docs/admin/tls-and-headers) | Serve HTTPS or mutual TLS, run behind a proxy, tune security headers, CSP and CORS |
| [Database](/docs/admin/database) | Choose the state backend (file, SQLite, PostgreSQL), configure it, run `autoair db` migrations |
| [Scaling](/docs/admin/scaling) | Run several workers or replicas safely: shared state, co-editing transport, shared keys |
| [Admin panel](/docs/admin/admin-panel) | Use the in-app panel: replicas, audit browser, users, AI usage, approvals, database, caches |
| [Policy](/docs/admin/policy) | Enforce organisation rules on DAGs: forbidden operators, naming, owners, retries, secrets, Python code |
| [Audit](/docs/admin/audit) | Know what is recorded, where, how to export it, and how long it is kept |
| [Observability](/docs/admin/observability) | Set up logging, probes, metrics, tracing and alerts |
| [Backup and restore](/docs/admin/backup-restore) | Back up DAGs, state and keys, restore and verify |

Reference material: [configuration reference](/docs/reference/configuration) (every option), [CLI reference](/docs/reference/cli), [REST API](/api). Deployment recipes: [Deployment overview](/docs/deployment/overview).

## Production checklist

Work through this list before users depend on AutoAir. Each item links to the page with the details.

### Identity and access

- [ ] Choose IAM strategies that reflect your Airflow setup. In standalone mode, use `role_mapping` **alone**; `airflow_permissions` there gives every user every role. ([IAM](/docs/admin/iam#mapping-strategies))
- [ ] In plugin mode with FAB, run `autoair iam sync-fab --create-roles` after every AutoAir upgrade and assign `AutoAir <Role>` roles. ([IAM](/docs/admin/iam#fab-registering-permissions-with-autoair-iam-sync-fab))
- [ ] Decide who may push past pull requests: `iam.direct_push_roles` (default `["Admin"]`) or `[]` for nobody.
- [ ] Enable `iam.require_approval=true` if deploys to local or object targets need a second pair of eyes; git PR mode already reviews in the git host.
- [ ] Never run `autoair serve --no-auth` outside development.
- [ ] Keep `iam.audit_denials=true`.

### Network and TLS

- [ ] Serve HTTPS, either directly (`--ssl-certfile`/`--ssl-keyfile`) or at a proxy. ([TLS and HTTP headers](/docs/admin/tls-and-headers))
- [ ] Behind a proxy, set `server.forwarded_allow_ips` to the proxy's addresses so client IPs and the HTTPS scheme are right.
- [ ] Set `server.public_url` (links in commits and pull requests, SSO return URL).
- [ ] Keep `server.security_headers=true`; set `server.frame_ancestors` for portals that embed AutoAir; enable HSTS only when every client uses HTTPS.
- [ ] Leave `cors_origins` empty unless another origin calls the API; never combine `*` with credentials.
- [ ] Trust a corporate CA for outgoing calls with `outbound.ca_bundle`; never set `outbound.verify=false` in production.
- [ ] Restrict `/api/metrics` to the monitoring network.

### Sign-in

- [ ] Prefer same-host deployment so the Airflow session cookie signs users in automatically. ([SSO and sessions](/docs/admin/sso))
- [ ] Keep `sso.require_csrf_header=true`.
- [ ] Set `sso.airflow_public_url` when Airflow's browser URL differs from `airflow_url`.
- [ ] With several replicas on the file backend, put a shared rate limiter in front of `/api/auth/login`, or use a SQL backend (`database.shared_rate_limit`).

### State, storage and scaling

- [ ] Choose the state backend: `file` for one process, `sqlite` for several workers on one host, `postgresql` for several replicas. ([Database](/docs/admin/database))
- [ ] With PostgreSQL: `ssl_mode=verify-full`, password in `database.password`, `migrate_on_start=false` and `autoair db upgrade` in an init job.
- [ ] Use a git or object-storage deploy target when replicas run on several hosts; restrict target kinds with `storage.allowed_targets`.
- [ ] Prefer Airflow connections or a GitHub App over a raw `storage.git.token`; consider `storage.git.credentials_mode=per_user`.
- [ ] Set `AUTOAIR_COLLAB_SECRET` and `AUTOAIR_AI__USER_LLM__ENCRYPTION_KEY` from a secret store. ([Scaling](/docs/admin/scaling#shared-keys))
- [ ] Build images with a precomputed catalog (`autoair catalog build`, `AUTOAIR_CATALOG_FILE`) and the same providers on every replica.

### Policy and features

- [ ] Decide on Python code (`allow_python_code`, `features.python_code`) and privileged operators (`allow_privileged_operators`). ([Policy](/docs/admin/policy))
- [ ] Set naming rules (`policy.dag_id_pattern`), required owner, tags and minimum retries as your organisation requires.
- [ ] Switch off features you do not support (`features.*`). ([Feature flags](/docs/admin/feature-flags))
- [ ] If AI is enabled: choose providers and models, keep `ai.guardrails.redact_secrets=true`, decide on `ai.research.online`, set `ai.pricing` for cost tracking. ([AI providers](/docs/ai/providers), [Guardrails](/docs/ai/guardrails))

### Configuration hygiene

- [ ] Keep secrets in environment variables or secret mounts, not in configuration files readable by DAG code. ([Configuration](/docs/admin/configuration#secrets))
- [ ] Load Python configuration files only from administrator-controlled paths.
- [ ] Set `expose_config=non-sensitive-only` (or `true`) only if administrators need the configuration page.
- [ ] Record `autoair config show --changed` with each release.

### Operations

- [ ] Liveness on `/api/health`, readiness on `/api/ready`, and a generous startup probe. ([Observability](/docs/admin/observability#health-and-readiness))
- [ ] `AUTOAIR_LOGGING__FORMAT=json` and log shipping, including the `autoair.audit` logger.
- [ ] Prometheus scraping and alerts on readiness, 5xx rates and `autoair_deploys_total{result="error"}`.
- [ ] Optional: OpenTelemetry tracing (`autoair[otel]`, `AUTOAIR_OTEL_ENABLED=true`).
- [ ] An audit retention and export procedure. ([Audit](/docs/admin/audit#retention))
- [ ] Backups of the deploy target, the state store and the keys, with a tested restore. ([Backup and restore](/docs/admin/backup-restore))
- [ ] An upgrade procedure: new image, `autoair db upgrade`, `autoair iam sync-fab` (FAB), rolling restart. ([Upgrading](/docs/installation/upgrading))

### Example: standalone production settings

```bash
autoair serve --workers 1 \
  --ssl-certfile /etc/autoair/tls.pem --ssl-keyfile /etc/autoair/tls.key   # or TLS at the proxy
AUTOAIR_AIRFLOW_URL=http://airflow-api-server:8080
AUTOAIR_SERVER__PUBLIC_URL=https://autoair.acme.io
AUTOAIR_SERVER__FORWARDED_ALLOW_IPS='["10.0.0.10"]'
AUTOAIR_SERVER__HSTS_SECONDS=31536000
AUTOAIR_OUTBOUND__CA_BUNDLE=/etc/ssl/certs/acme-root.pem
AUTOAIR_LOGGING__FORMAT=json
AUTOAIR_IAM__STRATEGIES='["role_mapping"]'
AUTOAIR_DATABASE__BACKEND=postgresql
AUTOAIR_DATABASE__URL=postgresql://autoair@db.acme.io:5432/autoair
AUTOAIR_DATABASE__PASSWORD=...
AUTOAIR_DATABASE__SSL_MODE=verify-full
AUTOAIR_DATABASE__MIGRATE_ON_START=false
AUTOAIR_COLLAB_SECRET=...
AUTOAIR_AI__USER_LLM__ENCRYPTION_KEY=...
AUTOAIR_STORAGE__MODE=git
AUTOAIR_STORAGE__ALLOWED_TARGETS='["git"]'
AUTOAIR_CATALOG_FILE=/opt/autoair/catalog.json
```
