# State and scaling

Everything AutoAir keeps between requests, where it lives, whether several AutoAir replicas
can share it, and what makes it safe. Read this before running more than one replica
(several `autoair serve` pods, several Airflow api-server replicas with the plugin, or a
separate AI service). Operator-facing instructions are in
[Scaling](https://ctolon.github.io/autoair/docs/admin/scaling); the settings are in
[the `database` reference](../configuration-reference.md#database) and the [Database guide](https://ctolon.github.io/autoair/docs/admin/database).

## The two state backends

`database.backend` selects where AutoAir's own state lives:

| Backend | Install | Use it for |
|---|---|---|
| `file` (default) | nothing | One replica, or several replicas on one shared volume / object store. JSON documents and JSON-lines logs on fsspec URLs under `<dags_folder>/<subfolder>/.autoair/`. |
| `sqlite` | `autoair[sqlite]` | One host, several worker processes (one SQLite file, WAL journal, `BEGIN IMMEDIATE` write transactions). |
| `postgresql` | `autoair[postgres]` | Several replicas on several hosts. Transactions, row versions, unique constraints, advisory locks, `LISTEN/NOTIFY`. |

The code talks to small repository protocols (`autoair/persistence/protocols.py`); each has
a file implementation (next to the domain code) and a SQL implementation
(`autoair/persistence/sql.py`, SQLAlchemy Core; 2.x, or 1.4 inside Airflow 3.0, which pins
`sqlalchemy<2`: only APIs both lines share, RETURNING only where the dialect compiles it). The HTTP API is the same on every backend.

## Inventory

"Multi-replica safe" is answered for the **file** backend on a shared volume and for the
**SQL** backends.

### Persistent state

| # | State | Location (file backend) | File: multi-replica safe? | SQL backend | Fix / notes |
|---|---|---|---|---|---|
| 1 | Saved templates (dag / fragment / operator) | `<state>/templates/<id>.json` (`templates_url`) | Yes for create/delete (atomic replace per file); concurrent edits of the *same* template: last writer wins | `documents` rows, collection `templates` | Operator-template version numbers are computed read-then-write: two saves of one template at the same instant can get the same version (rare; last writer wins). |
| 2 | Audit trail | `<state>/audit/<day>/<host>-<pid>.jsonl` (`audit_url`) | Yes (one file per process and day, never shared), reads scan files | `audit` table, indexed by time, DAG, user, action | Admin audit browser filters + CSV export work on both; SQL pages with `LIMIT/OFFSET`. |
| 3 | AI memory (conventions) | `<state>/ai-memory/memory.json` (`ai.memory_url`) | Local disks: yes (`flock`); object stores: last writer wins | `ai_memory` table; unique `(scope, owner, text_hash)` makes adds idempotent; capacity checked under an advisory lock | |
| 4 | AI audit log + retention | `<state>/ai-audit/<day>/<host>-<pid>.jsonl` (`ai.audit.url`) | Writes yes; retention runs in every process (idempotent deletes) | `ai_audit` table with token/cost columns (admin AI-usage aggregates in SQL) | Retention takes `pg_try_advisory_xact_lock`: exactly one replica prunes. |
| 5 | AI knowledge-base overrides + UI toggles | `<state>/ai-knowledge/docs/<id>.md`, `state.json` (`ai.knowledge.store_url`) | Yes (overrides are one file each; `state.json` toggles are updated under a lock) | `documents` collections `ai_knowledge_docs`, `ai_knowledge_state` | Toggle changes are atomic updates (row lock / file lock). Each replica's retrieval index refreshes on `ai.retrieval.index_ttl_seconds`. |
| 6 | Per-user LLM settings + API keys | `<state>/ai-users/<sha256(user)>.json`, Fernet key `ai.user_llm.encryption_key` or `<state>/ai-user-llm.key` | Records yes; the generated key file is **per host** (another host cannot decrypt) | `documents` collection `ai_user_llm`; without a configured key the Fernet key is created once in the database (`secrets` collection) and shared | Keys stay encrypted at rest on both backends; set `ai.user_llm.encryption_key` explicitly in production. |
| 7 | Deploy-target choice (UI) | `<state>/deploy-target.json` | **No**: read at start-up only; other replicas keep the old target until restarted | `documents` collection `deploy_target`; a background thread re-reads its version every `database.sync_seconds` and applies changes without restart | Secrets are never stored (`***` placeholders). |
| 8 | Custom icons + icon assignments | `<icons>/<name>.png`, `<icons>/assignments.json` (`icons_url`) | Yes (one file per image; assignments are updated under a lock) | PNG bytes in `documents` (`icons`), assignments in `icon_assignments`, versioned updates | Icons are small (≤ 128 px PNG), so storing them in the database is fine. |
| 9 | Spec reviews (approvals) | `<state>/proposals/<dag>/reviews.json` (`iam.proposals_url`) | **No**: read-modify-write of one JSON index per DAG under a process lock — concurrent reviews on two replicas lose one | `reviews` table, unique `(dag_id, etag, user, verdict, comment_hash)` with `ON CONFLICT` upsert: a double-submitted approval is one row | The file backend is correct for one replica. Reviews are also in the audit trail. |
| 10 | Deploy proposals | `<state>/proposals/<dag>/proposals/<etag>.json` | Yes (one file per version) | `proposals` table, primary key `(dag_id, etag)` (saving twice is idempotent) | TTL `iam.proposal_ttl_seconds` applied on both. |
| 11 | Collaboration room snapshots | — (rooms are in memory only) | — | `documents` collection `collab_rooms` (merged Yjs updates), `collab_holders` (which replica has a room open) | See [Collaboration](#collaboration). |
| 12 | Users seen (admin panel) | `<state>/users/<hash>.json` | Yes (one file per user) | `documents` collection `users_seen` | User, AutoAir roles, last seen. |
| 13 | Replica heartbeats | — | — | `replicas` table | The admin overview lists live replicas. |
| 14 | DAG specs, generated files, versions/history | The deploy target: local folder (`.autoair/history/<dag>/`), object storage or git | Local folder: only on a shared volume (an `flock` serialises writers on one host); object storage: per-object atomic; git: the repository is the source of truth | — (stays on the deploy target) | Deliberately not stored in the database: the deploy target is what Airflow reads. Use git or object storage for several replicas. |
| 15 | Login rate limiter (standalone sign-in) | process memory (`LoginLimiter`) | **No**: each replica counts its own failures (N replicas = N × the limit) | `login_failures` table, sliding window, per-key advisory lock (`database.shared_rate_limit`) | Per replica on the file backend. |
| 16 | Collaboration ticket key | `collab_secret`, else Airflow's `[api_auth] jwt_secret`, else `<state>/collab.key` (0600, shared by every process on the state folder) | Yes when the replicas share the state folder (or one of the keys above) | Key created once in `documents/secrets` | A per-process random key is used only when no key can be read (logged); tickets then fail across replicas. |
| 17 | Runtime feature-flag overrides (admin panel) | `<state>/feature-overrides.json` | Yes on a shared state folder (local disks: `flock`; object stores: last writer wins); every replica re-reads it every `features_override_ttl` s | `documents` collection `feature_overrides`, versioned updates | Locked flags (`features_locked`, environment) ignore overrides. Changes are audited as `feature_override`. |
| 18 | Documentation knowledge base: ingestion lease, run status, admin source overrides | `<state>/kb-state/lock-<store>.json`, `status-<store>.json`, `sources.json` (`kb.state_url`) | Lease and status: yes on a shared state folder on local disks (`flock`); object stores: last writer wins. `<store>` is a hash of the index's identity, so replicas sharing an index share one lease | `documents` collection `kb_state`, versioned updates | The lease (`kb.lock_ttl_seconds`, renewed while a refresh runs) lets exactly one replica ingest into an index; overrides hold enabled/components/versions only, never credentials. |
| 19 | Documentation knowledge-base index (pages, chunks, vectors) | `kb.store=local`: `<cache_dir or state>/kb/kb.sqlite3` (`kb.local.path`) | Per host unless `kb.local.shared=true` (then one replica indexes for all on the shared file) | `kb.store=pgvector` (PostgreSQL tables `autoair_kb_*`) or `elasticsearch` (indices `autoair-kb-*`): shared by every replica | Derived from the sources: a lost local index is rebuilt by the next refresh. Git checkouts and the KB embedding cache live in `<kb cache>` (`kb.cache_dir`) per host. |

### Process-local caches (safe to keep per replica)

| State | Where | Why it is fine per replica / what to know |
|---|---|---|
| Live collaboration rooms (Yjs docs, awareness) | `pycrdt-websocket` server in each process | Default `collab.sync=memory`: put several replicas behind **sticky sessions** (all editors of a DAG on one replica). With a SQL database or Redis, rooms are shared — see below. |
| Bearer-token verification cache (`TokenVerifier`) | per process, `auth_cache_ttl` (60 s) | Logout `forget()`s the token on the replica that served it; another replica may trust it until its cache entry expires (≤ `auth_cache_ttl`). Airflow ≥ 3.2 revokes the token itself. |
| `custom_view` permission answers (`CustomViewCache`) | per process, `iam.custom_view_cache_seconds` | Bounded TTL; role changes in Airflow show up after the TTL on every replica. |
| Per-user LLM clients (`UserLlmCache`) | per process, keyed by `(user, updated_at)` | Keyed by the stored record's version, so a change made on another replica is picked up on the next request. |
| Operator catalog | memory + disk cache `cache_dir` (per host) | Derived from installed packages; every replica must have the same providers installed. "Rebuild catalog" in the admin panel acts on the replica serving the request. |
| Marketplace indexes | memory, `marketplace_cache_ttl` | Re-fetched after the TTL; "Clear" acts per replica. |
| AI retrieval index, query and embedding caches | memory + `<cache_dir>/ai-embeddings.sqlite3` | Derived data; rebuilt after `ai.retrieval.index_ttl_seconds` (admin "Rebuild index" per replica). |
| Signed-identity replay cache (`ai/access.py`) | per process | A separate AI service with several replicas detects a replayed identity only on the replica that saw it first; identities expire after `MAX_IDENTITY_AGE` (300 s), which bounds the window. |
| Git provider lookups (`storage/git/target.py` LRU), DagBag worker pool, module caches (`lru_cache`, active sections) | per process | Derived / configuration data. |

## Concurrency rules on SQL

* **No blind read-modify-write.** Documents carry an integer `version`; `update()` runs in one
  transaction holding a per-document lock (`pg_advisory_xact_lock` on PostgreSQL, the
  `BEGIN IMMEDIATE` write lock on SQLite), so concurrent replicas queue instead of
  overwriting each other. Conditional writes (`expected_version`) raise `DocumentConflict`.
* **Idempotency through constraints**: the same review verdict, the same memory text and the
  same proposal are `ON CONFLICT` upserts, never duplicate rows.
* **Leader-only maintenance** (AI audit retention) uses `pg_try_advisory_xact_lock`; a
  replica that does not get the lock skips the run.
* Contract tests (`tests/unit/test_persistence_contract.py`) run every repository on files,
  SQLite and — with `AUTOAIR_TEST_POSTGRES_URL` — PostgreSQL, including many threads doing
  read-modify-write at once (no lost increments).

## Schema migrations

AutoAir ships a small versioned migrator (`autoair/persistence/migrations.py`) instead of
Alembic:

* AutoAir owns a handful of tables and often runs **inside Airflow's process**, whose own
  Alembic environment and `alembic_version` table must not be touched or confused;
* it needs nothing but SQLAlchemy (the `sqlite`/`postgres` extras stay small);
* migrations are numbered Python functions applied in one transaction under an advisory
  lock, recorded in `<prefix>schema_version`; several replicas starting at once are safe.

`autoair db upgrade` applies pending migrations, `autoair db current` shows the version;
`database.migrate_on_start=true` (default) upgrades on start-up. In production with several
replicas, run `autoair db upgrade` from an init job and set `migrate_on_start=false`: a
replica then refuses to start on an older schema instead of migrating it.

## Collaboration

Rooms are Yjs documents relayed by `pycrdt-websocket`. `collab.sync` decides how replicas
share them (`autoair/collab_sync.py`):

| Mode | When | How |
|---|---|---|
| `memory` | default with the file backend | One process owns a room. Several replicas need **sticky sessions** keyed by DAG (e.g. hash of the `/collab/<dag_id>` path) so all editors of a DAG reach the same replica. |
| `database` | default with `sqlite` / `postgresql` | Every update and awareness message from a replica's clients is appended to `<prefix>collab_updates` and applied by the other replicas. PostgreSQL wakes readers with `NOTIFY` (`LISTEN` connection per replica); SQLite polls every `collab.poll_seconds`. Rows are pruned after `collab.retention_seconds`. |
| `redis` | `collab.redis_url` set | Redis pub/sub on `collab.redis_channel` (`autoair[collab-redis]`). Snapshots still go to the SQL database when one is configured. |

With a shared mode:

* a replica opening a room it does not hold asks the others for the room state
  (`sync-request`) and serves its first client only after that state arrived (or
  `collab.join_timeout`), so a newcomer never sees an empty room and re-seeds it from a stale
  spec; replicas record which rooms they hold (`collab_holders`), so the wait is skipped
  when nobody else has the room open;
* changed rooms are saved every `collab.snapshot_seconds` and when they close, **merged**
  with the stored snapshot (never overwritten), and restored when the room re-opens within
  `collab.snapshot_ttl_seconds` (e.g. a rolling restart). Older snapshots are ignored: the
  stored spec stays the source of truth;
* tickets validate on every replica (shared key, see #16 above).

Tested by `tests/integration/test_collab_replicas.py` (two and three uvicorn apps sharing
SQLite/PostgreSQL: convergence, late joiners, snapshots across restarts, presence relay,
`NOTIFY` wake-up) and the browser E2E `frontend/e2e/collab-two-users.spec.ts` (two users on
two replicas: add node, change a parameter, move, presence, cursors, concurrent edits,
per-user undo).
