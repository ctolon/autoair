---
paths:
  - "src/autoair/persistence/**"
  - "src/autoair/collab.py"
  - "src/autoair/collab_sync.py"
  - "src/autoair/feature_overrides.py"
  - "docs/architecture/state-and-scaling.md"
  - "tests/unit/test_persistence_contract.py"
  - "tests/integration/test_persistence_api.py"
  - "tests/integration/test_collab_replicas.py"
---

# Persistence and replica safety

Procedure for new state, a table or a migration: skill `add-persistence-document`. Read
`docs/architecture/state-and-scaling.md` first: it lists every piece of state and whether it is
replica-safe, and new state is added to its inventory.

- Backends: `database.backend` = `file` (default) | `sqlite` | `postgresql` (SQLAlchemy Core,
  2.x with psycopg 3, or 1.4 with psycopg2 on Airflow 3.0: use the `_returning()` /
  `postgres_driver()` helpers in `persistence/sql.py` and `db.py`, not 2.0-only APIs). `document_store(settings, "<collection>", ...)` returns the SQL or file store; there
  is no central registry, so grep before picking a collection name.
- Writes are versioned: `update(key, fn)` under a per-document lock, or `put(...,
  expected_version=...)` with a retry on `DocumentConflict` (HTTP 409). Never a blind `get` then
  `put`. A side effect that must happen once across replicas is claimed in the document first
  (status + claimer + timestamp, with an expiry) and its outcome recorded with another
  compare-and-set.
- Migrations (`persistence/migrations.py`, `MIGRATIONS`) are forward-only, run in one
  transaction under an advisory lock, and are never edited once released. No Alembic: AutoAir
  runs inside Airflow's process.
- Queries are bounded (`LIMIT`, indexed filters); no unbounded scans in request paths.
- No secrets in documents; store an Airflow `conn_id` instead.
- Tests parametrise with `tests/db_backends.py::BACKENDS` (file, SQLite, PostgreSQL). PostgreSQL
  only against a throw-away database you started:
  `AUTOAIR_TEST_POSTGRES_URL=postgresql+psycopg://<user>:<pw>@<host>:5432/<db> uv run pytest -q -m postgres`.
