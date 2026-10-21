---
name: add-persistence-document
description: Store new AutoAir state that must survive restarts or be shared by replicas (a document collection, a SQL table, a migration). Use when adding server-side state, a cache that must agree across replicas, or changing src/autoair/persistence.
---

# Add a persistence document type or migration

State backends: `database.backend` = `file` (default) | `sqlite` | `postgresql`. Read
`docs/architecture/state-and-scaling.md` first: it lists every piece of state and whether it
is replica-safe.

## A document collection (most cases)

1. Get a store at the call site:

   ```python
   from autoair.persistence import document_store
   store = document_store(settings, "my_collection", file_url=..., suffix=".json")
   ```

   It returns `SqlDocumentStore` on SQL backends and `FileDocumentStore` otherwise; the
   `DocumentStore` protocol is in `persistence/documents.py`. There is no central registry,
   so pick a unique collection name (grep for it).
2. Writes are versioned: `update(key, fn)` runs `fn(old_bytes) -> new_bytes` under a
   per-document lock; `put(key, data, expected_version=...)` is a conditional write. Never
   `get` then `put` without one of them; `DocumentConflict` becomes HTTP 409
   (`api/errors.py`). Read-modify-write of a model: re-read, apply, `put(...,
   expected_version=doc.version)`, retry on `DocumentConflict` (`AnalysisStore.mutate` in
   `ai/assistants/troubleshoot.py`). To make one replica do a side effect once (an Airflow
   call), claim it first in the document (status `running` + claimer + timestamp, with an
   expiry for crashed claimers) and record the outcome afterwards with another
   compare-and-set (`claim_action` / `finish_action`); test it with two threads on SQLite
   (`tests/unit/test_troubleshoot_action_claim.py`).
3. No secrets in documents. If a value is a credential, store an Airflow `conn_id` instead.
4. Add the collection to the inventory table in `docs/architecture/state-and-scaling.md`.

## A table or migration

1. Table in `persistence/schema.py::build_tables` (all names use the configured prefix).
2. Migration in `persistence/migrations.py`: append `Migration(version=N, description=...,
   apply=fn)` to `MIGRATIONS`, where `fn(conn, db)` gets a SQLAlchemy `Connection` and the
   `Database`. Migrations are forward-only, run in one transaction under an
   advisory lock, and are never edited once released. AutoAir does not
   use Alembic (it runs inside Airflow's process; see the doc above).
3. Repository: a protocol in `persistence/protocols.py`, the SQL implementation in
   `persistence/sql.py`, the file implementation next to the domain code.
4. Queries are bounded (`LIMIT`, indexed filters); no unbounded scans in request paths.

## Tests

```bash
uv run pytest -q tests/unit/test_persistence_contract.py tests/integration/test_persistence_api.py
uv run pytest -q tests/unit/test_db_cli.py
# PostgreSQL (throw-away server; never the shared demo's database):
AUTOAIR_TEST_POSTGRES_URL=postgresql+psycopg://postgres:pw@localhost:5432/autoair_test \
  uv run pytest -q -m postgres
```

Parametrise new tests with `tests/db_backends.py::BACKENDS` so they run on files, SQLite
and PostgreSQL; each test gets a unique table prefix.

## Done when

- [ ] Works on file, sqlite and postgresql; concurrent writers do not lose updates
- [ ] Migration numbered, idempotent, covered by a test
- [ ] State inventory doc updated
