---
name: verify-backend
description: Run AutoAir's Python checks in fast-to-slow order (ruff, mypy, focused pytest, config docs, hard-coded audit, release metadata, Airflow matrix, PostgreSQL). Use before reporting any change under src/autoair, tests/, scripts/ or deploy/ as done.
---

# Verify a backend change

Run from the repository root. Stop at the first failure, fix, rerun that step.

```bash
# 1. Lint and format (seconds). CI scope is src tests deploy; include scripts/ too.
uv run ruff check src tests scripts deploy
uv run ruff format --check src tests scripts deploy   # `uv run ruff format <files>` to fix

# 2. Types (strict, src only; ~1 min cold)
uv run mypy src

# 3. Focused tests first, then the suites you touched
uv run pytest -q tests/unit/test_<area>.py -k <name>
uv run pytest -q tests/unit
uv run pytest -q tests/integration                 # real Airflow from the dev venv

# 4. Generated artefacts and audits (seconds)
uv run python scripts/gen_config_docs.py --check   # settings changed -> run without --check
uv run python scripts/scan_hardcoded.py --check    # new literals -> skill hardcoded-audit-triage
uv run python scripts/gen_shared_constants.py --check  # Python constants mirrored in frontend/src/lib/generated/
uv run python scripts/check_release.py             # versions agree

# 5. Whole suite (minutes)
uv run pytest -q --ignore=tests/live

# 6. Core install without pydantic-ai (`autoair[ai]` absent): app, MCP, CLI, KB work with AI off
uv run nox -s noai
```

## Slower, when the change warrants it

| Change | Command |
|---|---|
| Codegen output | `AUTOAIR_UPDATE_GOLDEN=1 uv run pytest tests/integration/test_airflow_dags.py -k golden_source`, then review `git diff tests/golden/expected` |
| Version-dependent code | `uv run nox -s "tests(3.0)"`, `uv run nox -s oldest` (Python 3.10 + Airflow 3.0); full matrix `uv run nox` (see skill airflow-compat-check) |
| AI imports or optional extras | `uv run nox -s noai` (fresh venv with `.[standalone,mcp]`, asserts `pydantic_ai` is absent; runs `tests/integration/test_ai_unavailable.py` and `test_kb_noai.py`) |
| KB Elasticsearch store | `AUTOAIR_TEST_ELASTICSEARCH_URL=http://127.0.0.1:<port> uv run pytest -q tests/unit/test_kb_store_contract.py` against a throw-away container you started (the in-memory fake in `tests/kb_fake_es.py` always runs) |
| Persistence / collab replicas | `AUTOAIR_TEST_POSTGRES_URL=postgresql+psycopg://... uv run pytest -q tests/unit/test_persistence_contract.py tests/integration/test_persistence_api.py tests/integration/test_collab_replicas.py` |
| Real auth managers / GitLab | `-m live` suites; CI only (`live-auth`, `live-gitlab` jobs) |
| API schema | `cd frontend && npm run codegen` then skill verify-frontend |

Markers (`--strict-markers`): `airflow` (auto-skipped without Airflow), `postgres`
(`AUTOAIR_TEST_POSTGRES_URL`), `live` (`AUTOAIR_LIVE_AIRFLOW_URL`).

## Pitfalls

- `tests/conftest.py` defaults `AIRFLOW_HOME` to `.airflow-dev`, the shared demo's home. CI
  uses a temporary one. For runs that write Airflow state (DagBag, `airflow db`), set
  `AIRFLOW_HOME=$(mktemp -d)`.
- The PostgreSQL URL must point at a throw-away database you started; tests create and drop
  tables with a random prefix, but never aim them at a shared database.
- Other agents edit this working tree concurrently. A failure in a file you did not touch is
  probably theirs: report it, do not "fix" it.
- No fuzzing: cover edge cases with explicit tests.

## Done when

- [ ] ruff, format, mypy clean for the files you changed
- [ ] focused tests and the affected suites pass
- [ ] config docs / hard-coded audit / golden files regenerated if the change requires it
