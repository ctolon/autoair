---
title: PostgreSQL for AutoAir state
description: Store AutoAir's own state in PostgreSQL, separate from Airflow's metadata, with migrations, TLS, connection pooling and multi-replica behaviour.
sidebar_label: PostgreSQL
---

AutoAir keeps its own state (saved templates, the audit trail, reviews and deploy proposals, the deploy target chosen in the UI, custom icons, AI memory, the AI audit log, AI knowledge overrides, per-user AI model settings, collaboration room snapshots, users seen and replica heartbeats) in the backend selected by `database.backend`. PostgreSQL is the backend for more than one host: it makes every replica equivalent, so no sticky sessions are needed.

PostgreSQL does **not** hold the DAGs. Specs and generated files always live in the [deploy target](/docs/deployment/git-bundles), because that is what Airflow reads.

| Backend | Extra | Use for |
|---|---|---|
| `file` (default) | none | One replica, or replicas on one shared volume with sticky sessions |
| `sqlite` | `autoair[sqlite]` | Several worker processes on one host |
| `postgresql` | `autoair[postgres]` (SQLAlchemy 2 with psycopg 3; on Airflow 3.0 SQLAlchemy 1.4 with psycopg2) | Several replicas on several hosts |

What each state item becomes in SQL and why it is safe is listed in [State and scaling](/docs/admin/scaling).

## Separate from Airflow's metadata

Keep AutoAir's tables out of Airflow's metadata schema. AutoAir runs inside Airflow's process in plugin mode, and its own migrator must not interfere with Airflow's Alembic environment or `alembic_version` table. Two layouts work:

**A separate database (recommended).** The demo stack does exactly this (`deploy/compose/initdb/001-autoair.sql`):

```sql
CREATE DATABASE autoair;
CREATE ROLE autoair LOGIN PASSWORD '...';
ALTER DATABASE autoair OWNER TO autoair;
```

**A separate schema in a shared database.** Set `database.schema_name`; the first migration runs `CREATE SCHEMA IF NOT EXISTS` for it, so the role needs `CREATE` on the database (or create the schema beforehand and grant it):

```sql
CREATE SCHEMA autoair AUTHORIZATION autoair;
```

```bash
AUTOAIR_DATABASE__SCHEMA_NAME=autoair
AUTOAIR_DATABASE__TABLE_PREFIX=autoair_   # default; every table carries it
```

The role needs to create tables in its schema (for migrations), read and write them, take advisory locks and use `LISTEN` / `NOTIFY` (all available to ordinary roles).

## Configuration

```bash
pip install 'autoair[postgres,collab]'

AUTOAIR_DATABASE__BACKEND=postgresql
AUTOAIR_DATABASE__URL=postgresql+psycopg://autoair@db.example.com:5432/autoair
AUTOAIR_DATABASE__PASSWORD=...                    # from a secret; merged into the URL
AUTOAIR_DATABASE__SSL_MODE=verify-full
AUTOAIR_DATABASE__SSL_ROOT_CERT=/etc/ssl/certs/corp-root.pem
AUTOAIR_DATABASE__CONNECT_ARGS='{"connect_timeout": 10, "application_name": "autoair"}'
AUTOAIR_DATABASE__MIGRATE_ON_START=false          # an init job runs `autoair db upgrade`
```

A `postgresql://` URL is treated as `postgresql+psycopg://` (psycopg 3); on SQLAlchemy 1.4 (Airflow 3.0) it, and `postgresql+psycopg://`, use psycopg2 ([details](/docs/installation/compatibility#postgresql-on-airflow-30)). Credentials in the URL are masked when the configuration is exposed; keeping the password in `database.password` keeps it out of the URL entirely.

| Setting | Default | Purpose |
|---|---|---|
| `database.backend` | `file` | `postgresql` selects this backend |
| `database.url` | none | SQLAlchemy URL |
| `database.password` | none | Secret, merged into the URL |
| `database.ssl_mode` | driver default | `disable`, `allow`, `prefer`, `require`, `verify-ca`, `verify-full` |
| `database.ssl_root_cert` | none | CA bundle verifying the server |
| `database.schema_name` | search path | PostgreSQL schema for AutoAir's tables |
| `database.table_prefix` | `autoair_` | Prefix of every table |
| `database.migrate_on_start` | `true` | Migrate when the server starts |
| `database.connect_args` | `{}` | Extra psycopg `connect()` arguments |
| `database.echo` | `false` | Log every SQL statement (debugging only) |
| `database.heartbeat_seconds` | `30` | How often a replica records it is alive (admin panel) |
| `database.sync_seconds` | `5` | How often a replica picks up shared changes such as the deploy target chosen in the UI |
| `database.shared_rate_limit` | `true` | Count failed standalone sign-ins in the database (one limit for all replicas) |

## Migrations

AutoAir ships its own small, numbered migrator instead of Alembic. Applied versions are rows of `<prefix>schema_version`; each migration runs in one transaction under an advisory lock, so several replicas or jobs starting at once are safe.

```bash
autoair db upgrade              # create or migrate to the latest version
autoair db upgrade --revision 3 # migrate to a specific version
autoair db current              # applied version, latest version, pending migrations
autoair db current --json
```

Two operating models:

| Model | Settings | Behaviour |
|---|---|---|
| Migrate on start | `migrate_on_start=true` (default) | Each replica upgrades the schema when it starts. Simple; fine for one replica |
| Migration step | `migrate_on_start=false` + `autoair db upgrade` in a Kubernetes Job, init container or Helm pre-upgrade hook | A replica refuses to start on an older schema instead of migrating it. Recommended for several replicas |

Run the migration with the same image and the same `database` settings as the servers (`AUTOAIR_DATABASE__BACKEND`, `AUTOAIR_DATABASE__URL`, `AUTOAIR_DATABASE__PASSWORD` and any other [`database`](/docs/reference/configuration#database) option you set). See [Plugin with the Airflow Helm chart](/docs/deployment/helm-plugin#make-the-api-servers-stateless) for a Job manifest.

## Connection pooling

Each AutoAir **process** has its own SQLAlchemy pool. Size the database for:

```text
processes x (pool_size + max_overflow) + processes   (one LISTEN connection each)
```

where *processes* counts every API-server worker that loads the plugin (Airflow's `[api] workers` per pod times the pods), every `autoair serve` worker (`server.workers`) and the AI service. With the defaults (`pool_size=5`, `max_overflow=10`) four processes can open up to 64 connections.

| Setting | Default | Guidance |
|---|---|---|
| `database.pool_size` | `5` | Connections kept open per process |
| `database.max_overflow` | `10` | Extra connections under load |
| `database.pool_timeout` | `30` | Seconds to wait for a free connection before failing the request |
| `database.pool_recycle` | `1800` | Replace connections older than this; keep it below the server's or proxy's idle timeout (`-1`: never) |
| `database.pool_pre_ping` | `true` | Test a pooled connection before use, so restarts and failovers are survived |

**PgBouncer.** Collaboration fan-out (`collab.sync=database`) keeps one `LISTEN` connection per process, which needs a session-level connection; PgBouncer in transaction pooling mode does not deliver notifications. Either connect AutoAir directly to PostgreSQL, use PgBouncer in session mode, or set `AUTOAIR_COLLAB__REDIS_URL` so collaboration uses Redis instead of `LISTEN/NOTIFY`. Without notifications, replicas still converge by polling, `20x` slower than `collab.poll_seconds`.

AutoAir's own repositories and migrations use transaction-level advisory locks (`pg_advisory_xact_lock`, `pg_try_advisory_xact_lock`), which work with transaction pooling.

## Behaviour with several replicas

- Collaboration rooms are shared (`collab.sync=auto` resolves to `database`); updates are relayed through `<prefix>collab_updates`, pruned after `collab.retention_seconds`.
- Tickets, the per-user AI key encryption key (created once when `ai.user_llm.encryption_key` is unset) and the sign-in rate limit are shared.
- Approvals are idempotent (unique constraints), AI audit retention runs on one replica at a time (advisory lock), and the admin panel lists every live replica.
- A deploy target chosen in the UI reaches every replica within `database.sync_seconds`.

## Moving from the file backend

Switching `database.backend` starts with empty AutoAir state; nothing is imported automatically. Before switching:

1. Export saved templates (they are plain JSON under `<state>/templates/`, or `AUTOAIR_TEMPLATES_URL`) and re-import them through the API (`POST /api/templates`).
2. Re-select the deploy target in the UI if it was chosen there.
3. Keep the old audit files if you must retain them; they are not migrated.

DAG specs and history are unaffected: they live in the deploy target.

## Backups

Back up the AutoAir database together with the deploy repository and the encryption keys (`ai.user_llm.encryption_key`, `collab_secret`). Without the Fernet key, stored per-user AI keys cannot be decrypted. See [Backup and restore](/docs/admin/backup-restore).

## Related pages

- [Database settings](/docs/admin/database)
- [State and scaling](/docs/admin/scaling)
- [Kubernetes reference architecture](/docs/deployment/kubernetes)
- [`database` settings](/docs/reference/configuration#database)
