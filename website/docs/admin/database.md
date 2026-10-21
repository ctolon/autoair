---
title: Database (state store)
description: Where AutoAir keeps its own state, the file, SQLite and PostgreSQL backends, connection URLs and TLS, schema migrations with autoair db, and what is stored where.
---

AutoAir keeps two kinds of data:

* **DAGs** (the spec `<dag_id>.autoair.json`, the generated DAG files, version history) go to the **deploy target**: a local folder, object storage or a git repository. That is what Airflow reads, and it is never moved into a database. See [Deploy](/docs/user-guide/deploy).
* **AutoAir's own state** (templates, the audit trail, reviews and deploy proposals, AI memory and audit, AI knowledge-base overrides, per-user AI model settings, assistant threads, MCP servers defined in the admin panel, AI rules and troubleshooting data, the research cache, custom icons, the deploy target chosen in the UI, the users seen, collaboration snapshots) goes to the **state store** selected by `database.backend`.

Multi-replica behaviour is in [Scaling](/docs/admin/scaling); backups are in [Backup and restore](/docs/admin/backup-restore).

## Backends

| Backend | Install | Use it for |
|---|---|---|
| `file` (default) | nothing | One replica, or several replicas on one shared volume or object store. JSON documents and JSON-lines logs on fsspec URLs under the state folder. |
| `sqlite` | `pip install 'autoair[sqlite]'` | One host, several worker processes. One SQLite file, WAL journal, `BEGIN IMMEDIATE` write transactions. |
| `postgresql` | `pip install 'autoair[postgres]'` | Several replicas on several hosts. psycopg 3, transactions, row versions, unique constraints, advisory locks, `LISTEN/NOTIFY`. |

Choose by topology:

* a single `autoair serve` process, or the plugin in a single Airflow API server: `file`;
* `autoair serve --workers N` on one machine: `sqlite`, or `file` with sticky sessions for co-editing;
* several pods or hosts: `postgresql`.

## File backend

The state folder is `<dags_folder>/<subfolder>/.autoair/`, shown as `<state>` throughout these docs (for example `/opt/airflow/dags/autoair/.autoair/`). Individual stores can be moved to any fsspec URL (another folder, or object storage such as `s3://` or `gs://`):

| Setting | Default | Holds |
|---|---|---|
| `AUTOAIR_TEMPLATES_URL` | `<state>/templates` | Saved templates (dag, fragment, operator) |
| `AUTOAIR_AUDIT_URL` | `<state>/audit` | Audit trail, `<day>/<host>-<pid>.jsonl` |
| `AUTOAIR_ICONS_URL` | `<state>/icons` | Custom icon PNGs and `assignments.json` |
| `AUTOAIR_IAM__PROPOSALS_URL` | `<state>/proposals` | Deploy proposals and reviews |
| `AUTOAIR_AI__AUDIT__URL` | `<state>/ai-audit` | AI audit log |
| `AUTOAIR_AI__MEMORY_URL` | `<state>/ai-memory` | AI conventions memory |
| `AUTOAIR_AI__KNOWLEDGE__STORE_URL` | `<state>/ai-knowledge` | AI knowledge-base overrides and toggles |
| `AUTOAIR_AI__USER_LLM__STORE_URL` | `<state>/ai-users` | Per-user AI model settings (keys encrypted) |
| `AUTOAIR_AI__ASSISTANTS__STORE_URL` | `<state>/ai-threads` | Assistant threads |
| `AUTOAIR_AI__MCP_CLIENT__STORE_URL` | `<state>/ai-mcp-servers` | MCP servers defined in the admin panel (headers encrypted) |

Other files in the state folder: `deploy-target.json` (the deploy target chosen in the UI), `collab.key` (co-editing ticket key, mode `0600`, when neither `collab_secret` nor Airflow's `jwt_secret` is available), `ai-user-llm.key` (Fernet key when `ai.user_llm.encryption_key` is unset), `users/` (users seen, for the admin panel), and folders of other AI stores (`ai-rules`, `ai-troubleshoot`, research cache).

The file backend writes atomically per file and uses file locks on local disks. Its limits with several replicas are listed in [Scaling](/docs/admin/scaling#persistent-state).

## SQLite

```bash
pip install 'autoair[sqlite]'
AUTOAIR_DATABASE__BACKEND=sqlite
# optional; default <state>/autoair.db
AUTOAIR_DATABASE__URL=sqlite:////var/lib/autoair/autoair.db
```

Four slashes mean an absolute path. The file is opened in WAL mode with a busy timeout, and writes take `BEGIN IMMEDIATE` locks, so several worker processes on one host are safe. Do not put the file on a network file system shared by several hosts; use PostgreSQL for that.

## PostgreSQL

```bash
pip install 'autoair[postgres]'
AUTOAIR_DATABASE__BACKEND=postgresql
AUTOAIR_DATABASE__URL=postgresql://autoair@db.acme.io:5432/autoair
AUTOAIR_DATABASE__PASSWORD=...                  # SecretStr, merged into the URL
AUTOAIR_DATABASE__SSL_MODE=verify-full          # disable|allow|prefer|require|verify-ca|verify-full
AUTOAIR_DATABASE__SSL_ROOT_CERT=/etc/ssl/certs/acme-root.pem
AUTOAIR_DATABASE__POOL_SIZE=5                   # + MAX_OVERFLOW, POOL_TIMEOUT, POOL_RECYCLE, POOL_PRE_PING
AUTOAIR_DATABASE__CONNECT_ARGS='{"connect_timeout": 10, "application_name": "autoair"}'
AUTOAIR_DATABASE__SCHEMA_NAME=autoair           # PostgreSQL schema (created by the migration)
AUTOAIR_DATABASE__TABLE_PREFIX=autoair_         # share a database with other applications
AUTOAIR_DATABASE__MIGRATE_ON_START=false        # when an init job runs `autoair db upgrade`
```

* **URL.** A SQLAlchemy URL. `postgresql://` and `postgres://` are switched to the psycopg 3 driver (`postgresql+psycopg://`) automatically. Credentials in the URL are masked wherever the configuration is shown, but keep the password in `database.password` so it never appears in a URL.
* **TLS.** `ssl_mode` and `ssl_root_cert` map to libpq's `sslmode` and `sslrootcert`. Use `verify-full` in production.
* **Pool.** `pool_size` (5) connections are kept open, plus up to `max_overflow` (10) under load; a request waits `pool_timeout` (30 s) for a free connection. `pool_recycle` (1800 s) replaces connections before a server or proxy idle timeout closes them, and `pool_pre_ping` tests a pooled connection before use (survives database restarts). Each replica and each worker process has its own pool, so size `max_connections` for `replicas x workers x (pool_size + max_overflow)`, plus one `LISTEN` connection per replica for co-editing.
* **Sharing a database.** Every table name starts with `table_prefix` (`autoair_`), and `schema_name` puts them in their own schema. AutoAir never touches other tables; it does not use or modify Airflow's `alembic_version`.
* **Debugging.** `database.echo=true` logs every SQL statement.

A separate PostgreSQL deployment guide is in [PostgreSQL](/docs/deployment/postgres).

## Tables

All names carry `table_prefix` (default `autoair_`).

| Table | Content |
|---|---|
| `schema_version` | Applied migrations: version, description, time |
| `documents` | Versioned JSON documents by collection and key (see below) |
| `audit` | The audit trail, indexed by DAG, user, action and time |
| `ai_audit` | AI calls with token and cost columns |
| `ai_memory` | AI conventions memory; unique `(scope, owner, text_hash)` |
| `reviews` | Spec reviews; unique `(dag_id, etag, user, verdict, comment_hash)` |
| `proposals` | Deploy proposals; primary key `(dag_id, etag)` |
| `login_failures` | Failed standalone sign-ins (`database.shared_rate_limit`) |
| `replicas` | Replica heartbeats (admin panel) |
| `collab_updates` | Co-editing messages relayed between replicas |

Collections in `documents` include `templates`, `deploy_target`, `icons`, `icon_assignments`, `users_seen`, `secrets` (keys generated once and shared by every replica), `collab_rooms`, `collab_holders`, `ai_user_llm`, `ai_knowledge_state`, `ai_rules`, `ai_mcp_servers`, `ai_threads`, `ai_troubleshoot` and `ai_identity_nonces`.

## Migrations: `autoair db`

AutoAir ships its own small, numbered migrator instead of Alembic. It needs nothing beyond SQLAlchemy, and it never interferes with Airflow's Alembic environment when AutoAir runs inside Airflow's process. Each migration runs in one transaction under an advisory lock and is recorded in `<prefix>schema_version`, so several replicas starting at once are safe.

```bash
autoair db current            # schema version, latest version, pending migrations
autoair db current --json     # {"url": "...", "current": 1, "head": 1, "pending": []}
autoair db upgrade            # create or migrate the tables to the latest version
autoair db upgrade --revision 1
```

Example output:

```text
$ autoair db current
postgresql+psycopg://autoair:***@db.acme.io:5432/autoair: version None (head 1)
pending: [1] - run `autoair db upgrade`

$ autoair db upgrade
applied: [1]; head: 1
```

| Situation | Exit code | Message |
|---|---|---|
| `database.backend` is `file` | `2` | `database.backend is 'file': nothing to migrate` |
| Bad settings, missing driver (`autoair[postgres]` not installed) | `2` | the error |
| `--revision` above the latest | `2` | `unknown schema version N (latest is M)` |
| Database unreachable or SQL error | `1` | `<masked URL>: <first line of the error>` |

The commands read the same configuration as the server (environment, `.env`, `--config`), so run them with the same settings, for example `autoair -c /etc/autoair/autoair.yaml db upgrade`.

### Migrating on start-up or from a job

* `database.migrate_on_start=true` (default): the server applies pending migrations when it starts. Fine for one replica.
* `database.migrate_on_start=false`: the server refuses to start when the recorded schema version differs from its own (an older schema is not migrated). Use this with several replicas, and run `autoair db upgrade` once from an init job (Kubernetes `initContainer`, Helm pre-upgrade hook, CI step) before the new version rolls out.

Administrators can also apply pending migrations from the admin panel (**Database → Apply migrations**, needs `admin:config`); the action is audited as `db_upgrade`.

## Background activity

With a SQL backend, each replica:

* records a heartbeat every `database.heartbeat_seconds` (30) in `replicas` (the admin panel's **Replicas** list; a replica is shown as stale after one interval without a heartbeat);
* re-reads shared changes every `database.sync_seconds` (5), such as a deploy target chosen in the UI on another replica, and applies them without a restart;
* counts failed sign-ins in the database when `database.shared_rate_limit=true` (default), so the limit holds across replicas.

## Inspecting the database

* The admin panel's **Database** tab shows the backend, the masked URL, whether it is reachable, the schema version and latest version, pending and applied migrations, the connection pool, and row counts per table. With the file backend it shows the state folder. See [Admin panel](/docs/admin/admin-panel).
* `GET /api/admin/database` returns the same data (`admin:panel`).
* `GET /api/ready` does not check the database; `autoair db current` is a good start-up probe for jobs.

## Changing backends

Moving from `file` to a SQL backend (or between databases) starts with **empty state**; there is no automatic copy. Before switching:

1. Export saved templates (they are plain JSON in `<state>/templates`) and re-import them through the API or the UI after the switch.
2. Keep or archive the file audit trail (`<state>/audit`) for compliance; it is not imported.
3. Re-create UI-defined items (the deploy target chosen in the UI, MCP servers, AI rules), and have users re-enter personal AI keys.
4. Set `collab_secret` and `ai.user_llm.encryption_key` explicitly if you want them to stay the same.

DAGs are not affected: they live in the deploy target.

## Settings

| Setting | Default | Description |
|---|---|---|
| `database.backend` | `file` | `file`, `sqlite`, `postgresql` |
| `database.url` | unset | SQLAlchemy URL (SQLite default `<state>/autoair.db`) |
| `database.password` | unset | Password merged into the URL (secret) |
| `database.ssl_mode`, `database.ssl_root_cert` | unset | PostgreSQL TLS |
| `database.pool_size`, `database.max_overflow`, `database.pool_timeout`, `database.pool_recycle`, `database.pool_pre_ping` | `5`, `10`, `30`, `1800`, `true` | Connection pool |
| `database.connect_args` | `{}` | Extra DBAPI `connect()` arguments |
| `database.schema_name` | unset | PostgreSQL schema |
| `database.table_prefix` | `autoair_` | Table name prefix |
| `database.migrate_on_start` | `true` | Migrate when the server starts |
| `database.echo` | `false` | Log SQL |
| `database.heartbeat_seconds`, `database.sync_seconds` | `30`, `5` | Replica heartbeat and shared-change polling |
| `database.shared_rate_limit` | `true` | Shared sign-in rate limit |

Full descriptions: [configuration reference](/docs/reference/configuration#database).
