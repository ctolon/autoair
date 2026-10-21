---
title: Scaling and replica safety
sidebar_label: Scaling
description: Running several AutoAir workers or replicas safely - what state is shared, what stays per process, how co-editing rooms are shared, and which keys must be the same everywhere.
---

AutoAir is stateless apart from its **state store** (`database.*`) and the **deploy target**. The tables below list everything AutoAir keeps between requests, where it lives, whether several replicas can share it, and what makes it safe. Read it before running more than one AutoAir process: several `autoair serve` workers or pods, several Airflow API-server replicas with the plugin, or a separate AI service.

## Two ways to scale out

**1. Shared database (recommended).** PostgreSQL makes every replica equivalent; no sticky sessions are needed.

```bash
pip install 'autoair[postgres,collab]'
AUTOAIR_DATABASE__BACKEND=postgresql
AUTOAIR_DATABASE__URL=postgresql://autoair@db:5432/autoair
AUTOAIR_DATABASE__PASSWORD=...                 # from a secret
AUTOAIR_DATABASE__SSL_MODE=verify-full
AUTOAIR_DATABASE__MIGRATE_ON_START=false       # migrations run once, from an init job:
autoair db upgrade                             # Kubernetes initContainer / Helm pre-upgrade hook
AUTOAIR_AI__USER_LLM__ENCRYPTION_KEY=...       # optional: otherwise created once in the database
```

With it:

* co-editing rooms are shared between replicas (`collab.sync=database`, PostgreSQL `LISTEN/NOTIFY`), or through Redis with `AUTOAIR_COLLAB__REDIS_URL`;
* co-editing tickets, the encryption key of per-user AI keys and the sign-in rate limit are shared;
* a deploy target chosen in the UI reaches every replica within `database.sync_seconds`;
* approvals are idempotent (unique constraints), AI audit retention runs on exactly one replica (advisory lock), and the admin panel lists every live replica.

**2. File backend on shared storage, with sticky sessions.** Keep `database.backend=file` on a volume every replica mounts (or point `templates_url`, `audit_url`, `icons_url`, `iam.proposals_url` and the `ai.*` URLs at object storage), and route `/collab/<dag_id>` stickily by path so all editors of a DAG reach one replica. Limits:

* the sign-in rate limit is per replica (N replicas allow N times the limit);
* a deploy target chosen in the UI reaches the other replicas only after a restart;
* reviews written at the same instant on two replicas can lose one (one JSON index per DAG);
* without `collab_secret` or Airflow's `jwt_secret`, the ticket key file must be on the shared volume.

`database.backend=sqlite` suits several workers on **one** host only.

In both cases the DAG files go to the deploy target: use **git or object storage** when replicas run on several hosts. A local-folder target is only safe on one host or on a shared volume (a file lock serialises writers on one host).

## Shared keys

Some keys must be identical on every replica and every worker. Set them explicitly in multi-replica deployments:

| Key | Used for | If unset |
|---|---|---|
| `AUTOAIR_COLLAB_SECRET` | HMAC of co-editing tickets | Derived from Airflow's `[api_auth] jwt_secret`; without it, created once in the database (SQL) or in `<state>/collab.key` (file, shared only if the folder is shared) |
| `AUTOAIR_AI__USER_LLM__ENCRYPTION_KEY` | Fernet encryption of per-user AI keys and of MCP server headers stored in the admin panel | Created once in the database (SQL) or in `<state>/ai-user-llm.key` (file: **per host**, another host cannot decrypt) |
| `AUTOAIR_AI__SERVICE__SHARED_SECRET` | Signed caller identity between the main server and a separate AI service | Unsigned calls are verified against Airflow instead |
| Airflow's `[api_auth] jwt_secret` | Airflow tokens that AutoAir forwards | Airflow's own requirement for several API servers |

Keep these in a secret store and back them up: losing the encryption key makes stored user AI keys unreadable (users must re-enter them). See [Backup and restore](/docs/admin/backup-restore).

## The state backends

| Backend | Install | Use it for |
|---|---|---|
| `file` (default) | nothing | One replica, or several replicas on one shared volume or object store. JSON documents and JSON-lines logs on fsspec URLs under `<dags_folder>/<subfolder>/.autoair/`. |
| `sqlite` | `autoair[sqlite]` | One host, several worker processes (one SQLite file, WAL journal, `BEGIN IMMEDIATE` write transactions). |
| `postgresql` | `autoair[postgres]` | Several replicas on several hosts. Transactions, row versions, unique constraints, advisory locks, `LISTEN/NOTIFY`. |

The code talks to small repository protocols; each has a file implementation (with the same on-disk layout as before) and a SQL implementation (SQLAlchemy 2 Core). API shapes are the same on every backend. Configuration and migrations: [Database](/docs/admin/database).

## Persistent state

"Multi-replica safe" is answered for the **file** backend on a shared volume and for the **SQL** backends.

| # | State | File backend location | File: multi-replica safe? | SQL backend | Notes |
|---|---|---|---|---|---|
| 1 | Saved templates (dag, fragment, operator) | `<state>/templates/<id>.json` (`templates_url`) | Yes for create/delete (atomic replace per file); concurrent edits of the *same* template: last writer wins | `documents`, collection `templates` | Operator-template version numbers are computed read-then-write: two saves of one template at the same instant can get the same version (rare; last writer wins). |
| 2 | Audit trail | `<state>/audit/<day>/<host>-<pid>.jsonl` (`audit_url`) | Yes (one file per process and day, never shared); reads scan files | `audit` table, indexed by time, DAG, user, action | The admin audit browser's filters and CSV export work on both; SQL pages with `LIMIT/OFFSET`. |
| 3 | AI memory (conventions) | `<state>/ai-memory/memory.json` (`ai.memory_url`) | `flock` on local disks; object stores: last writer wins | `ai_memory` table; unique `(scope, owner, text_hash)` makes adds idempotent; capacity checked under an advisory lock | |
| 4 | AI audit log and retention | `<state>/ai-audit/<day>/<host>-<pid>.jsonl` (`ai.audit.url`) | Writes yes; retention runs in every process (idempotent deletes) | `ai_audit` table with token and cost columns (admin AI-usage aggregates in SQL) | Retention takes `pg_try_advisory_xact_lock`: exactly one replica prunes. |
| 5 | AI knowledge-base overrides and UI toggles | `<state>/ai-knowledge/docs/<id>.md`, `state.json` (`ai.knowledge.store_url`) | Overrides yes; toggles locked | `documents` collections | Toggle changes are atomic updates (row lock or file lock). Each replica's retrieval index refreshes on `ai.retrieval.index_ttl_seconds`. |
| 6 | Per-user LLM settings and API keys | `<state>/ai-users/<sha256(user)>.json`; Fernet key `ai.user_llm.encryption_key` or `<state>/ai-user-llm.key` | Records yes; the generated key file is **per host** | `documents` collection `ai_user_llm`; without a configured key, the Fernet key is created once in the database (`secrets`) and shared | Keys stay encrypted at rest on both backends; set `ai.user_llm.encryption_key` explicitly in production. |
| 7 | Deploy target chosen in the UI | `<state>/deploy-target.json` | **No**: read only at start-up; other replicas keep the old target | `documents` collection `deploy_target`; a background thread re-reads it every `database.sync_seconds` and applies changes without restart | Secrets are never stored (`***` placeholders). |
| 8 | Custom icons and icon assignments | `<icons>/<name>.png`, `<icons>/assignments.json` (`icons_url`) | Images yes; assignments locked | PNG bytes in `documents` (`icons`), assignments in `icon_assignments`, versioned updates | Icons are small (at most 128 px PNG). |
| 9 | Spec reviews (approvals) | `<state>/proposals/<dag>/reviews.json` (`iam.proposals_url`) | **No**: read-modify-write of one JSON index per DAG under a process lock; concurrent reviews on two replicas can lose one | `reviews` table, unique `(dag_id, etag, user, verdict, comment_hash)` with `ON CONFLICT` upsert: a double-submitted approval is one row | The file backend stays correct for one replica. Reviews are also in the audit trail. |
| 10 | Deploy proposals | `<state>/proposals/<dag>/proposals/<etag>.json` | Yes (one file per version) | `proposals` table, primary key `(dag_id, etag)` (saving twice is idempotent) | TTL `iam.proposal_ttl_seconds` applies on both. |
| 11 | Co-editing room snapshots | none (rooms live only in memory) | not applicable | `documents` collections `collab_rooms` (merged Yjs updates), `collab_holders` (which replica has a room open) | See [Collaboration transport](#collaboration-transport). |
| 12 | Users seen (admin panel) | `<state>/users/<hash>.json` | Yes (one file per user) | `documents` collection `users_seen` | User, AutoAir roles, last seen. |
| 13 | Replica heartbeats | none | not applicable | `replicas` table | The admin panel's **Overview** lists live replicas. |
| 14 | DAG specs, generated files, versions | The deploy target: local folder (`.autoair/history/<dag>/`), object storage or git | Local folder: only on a shared volume (an `flock` serialises writers on one host); object storage: per-object atomic; git: the repository is the source of truth | unchanged | Deliberately *not* in the database: the deploy target is what Airflow reads. |
| 15 | Sign-in rate limiter | process memory | **No**: each replica counts its own failures | `login_failures` table, sliding window, per-key advisory lock (`database.shared_rate_limit`) | |
| 16 | Co-editing ticket key | `collab_secret`, else Airflow's `jwt_secret`, else a generated key | Yes when `<state>/collab.key` (mode `0600`) is on the shared folder | Key created once in `documents/secrets` | The key never depends on the process. |
| 17 | Other AI stores (assistant threads, AI rules, MCP servers defined in the admin panel, troubleshooting reports) | `<state>/ai-threads`, `ai-rules`, `ai-mcp-servers`, `ai-troubleshoot` | One document per item | `documents` collections `ai_threads`, `ai_rules`, `ai_mcp_servers`, `ai_troubleshoot` | MCP server headers are encrypted with the key of row 6. |

## Process-local caches

These stay per process. That is fine, but know the consequences:

| State | Where | What to know |
|---|---|---|
| Live co-editing rooms (Yjs documents, awareness) | the WebSocket server in each process | With `collab.sync=memory`, put several replicas behind **sticky sessions** (all editors of a DAG on one replica). With a SQL database or Redis, rooms are shared. |
| Bearer-token verification cache | per process, `auth_cache_ttl` (60 s) | Logout forgets the token on the replica that served it; another replica may trust it until its cache entry expires. Airflow 3.2 and later revokes the token itself. |
| `custom_view` permission answers | per process, `iam.custom_view_cache_seconds` (30 s) | Role changes in Airflow show up after the TTL on every replica. |
| API rate limits (`limits.*`) | per process | N processes allow up to N times the configured rate per user. |
| Per-user LLM clients | per process, keyed by the stored record's version | A change made on another replica is picked up on the next request. |
| Operator catalog | memory and disk cache `cache_dir` (per host) | Derived from installed packages: **every replica must have the same providers installed**. "Rebuild catalog" in the admin panel acts on the replica serving the request. |
| Marketplace indexes | memory, `marketplace_cache_ttl` | Re-fetched after the TTL; "Clear" acts per replica. |
| AI retrieval index, query and embedding caches | memory and `<cache_dir>/ai-embeddings.sqlite3` | Derived data; rebuilt after `ai.retrieval.index_ttl_seconds` ("Rebuild index" per replica). |
| Signed-identity replay cache of the AI service | per process | A separate AI service with several replicas detects a replayed identity only on the replica that saw it first; identities expire quickly, which bounds the window. |
| Git provider lookups, DagBag worker pool, module caches | per process | Derived or configuration data. |

## Concurrency rules on SQL

* **No blind read-modify-write.** Documents carry an integer `version`; an update runs in one transaction holding a per-document lock (`pg_advisory_xact_lock` on PostgreSQL, the `BEGIN IMMEDIATE` write lock on SQLite), so concurrent replicas queue instead of overwriting each other. Conditional writes raise a conflict when the version moved.
* **Idempotency through constraints.** The same review verdict, the same memory text and the same proposal are `ON CONFLICT` upserts, never duplicate rows.
* **Leader-only maintenance** (AI audit retention) uses `pg_try_advisory_xact_lock`; a replica that does not get the lock skips the run.
* Contract tests run every repository on files, SQLite and PostgreSQL, including many threads doing read-modify-write at once (no lost increments).

## Schema migrations

Migrations are numbered, applied in one transaction under an advisory lock and recorded in `<prefix>schema_version`; several replicas starting at once are safe. In production with several replicas, run `autoair db upgrade` from an init job and set `database.migrate_on_start=false`: a replica then refuses to start on an older schema instead of migrating it. Details: [Database](/docs/admin/database#migrations-autoair-db).

## Collaboration transport

Co-editing rooms are Yjs documents relayed over WebSockets. `collab.sync` decides how replicas share them:

| Mode | When | How |
|---|---|---|
| `memory` | `auto` with the file backend | One process owns a room. Several replicas need **sticky sessions** keyed by DAG (for example a hash of the `/collab/<dag_id>` path) so all editors of a DAG reach the same replica. |
| `database` | `auto` with `sqlite` or `postgresql` | Every update and awareness message from a replica's clients is appended to `<prefix>collab_updates` and applied by the other replicas. PostgreSQL wakes readers with `NOTIFY` (one `LISTEN` connection per replica) and polls only as a fallback; SQLite polls every `collab.poll_seconds` (0.25 s). Rows are pruned after `collab.retention_seconds` (300 s). |
| `redis` | `auto` when `collab.redis_url` is set | Redis pub/sub on `collab.redis_channel` (`autoair:collab`); needs `autoair[collab-redis]`. Snapshots still go to the SQL database when one is configured. |

`collab.sync=auto` (default) picks `redis` when `redis_url` is set, else `database` with a SQL backend, else `memory`. The admin panel's **Overview** shows the effective mode as **Co-editing sync**.

With a shared mode:

* a replica opening a room it does not hold asks the others for the room state and serves its first client only after that state arrived (or after `collab.join_timeout`, 1.5 s), so a newcomer never sees an empty room or re-seeds it from a stale spec. Replicas record which rooms they hold, so the wait is skipped when nobody else has the room open;
* changed rooms are saved every `collab.snapshot_seconds` (5 s) and when they close, **merged** with the stored snapshot (never overwritten), and restored when the room re-opens within `collab.snapshot_ttl_seconds` (300 s), for example across a rolling restart. Older snapshots are ignored: the stored spec stays the source of truth;
* tickets validate on every replica (shared key, row 16 above).

```bash
# Redis fan-out instead of the database
pip install 'autoair[collab,collab-redis]'
AUTOAIR_COLLAB__REDIS_URL=redis://redis:6379/0     # secret
```

Two replicas sharing SQLite or PostgreSQL are covered by integration tests (convergence, late joiners, snapshots across restarts, presence relay) and by a browser test with two users on two replicas. The user-facing feature is described in [Collaboration](/docs/user-guide/collaboration).

## Checklist for several replicas

1. `database.backend=postgresql` (or `file` on shared storage with sticky `/collab` routing).
2. `autoair db upgrade` in an init job; `database.migrate_on_start=false`.
3. A git or object-storage deploy target.
4. `AUTOAIR_COLLAB_SECRET` and `AUTOAIR_AI__USER_LLM__ENCRYPTION_KEY` set from a secret store.
5. The same AutoAir version and the same Airflow providers on every replica (identical images).
6. `autoair serve --workers N` counts as N processes for all of the above.
7. A separate AI service with several replicas uses the same `database.backend`, so assistant threads (stored per turn) are shared by its replicas.

See also [Kubernetes](/docs/deployment/kubernetes) and [PostgreSQL](/docs/deployment/postgres).
