---
name: add-kb-store-or-source
description: Add or change a documentation knowledge-base store backend (kb.store, the KbStore protocol in src/autoair/kb/stores) or a source kind (kb.sources[].kind in src/autoair/kb/config.py and sources.py). Use for a new search backend, a new place the KB reads documentation from, or changes to how an existing store or source works.
---

# Add a KB store or source kind

The knowledge base (`src/autoair/kb/`) works without `autoair[ai]`; read
`docs/ai-architecture.md` section 9a first. Its settings are `KbSettings` in
`src/autoair/kb/config.py` (section `kb`).

## A store backend

1. Implement the `KbStore` protocol (`kb/stores/base.py`) in `kb/stores/<name>.py`: pages with
   their compressed bodies, chunks with a keyword index, optional vectors of one embedding
   model. Return lexical and dense rankings only; `service.py` fuses them (RRF), so do not rank
   across both inside the store. Use the helpers in `base.py` (`query_tokens`, `index_terms`,
   `clean_filters`, `normalise`).
2. Buffered writes are allowed (Elasticsearch batches `_bulk`): they must become durable and
   visible to every replica in `flush()`, which the ingestion run calls once at its end, and
   the same instance must read its own writes before that.
3. Settings: add the name to `StoreKind` and a `Kb<Name>Settings` section with descriptions and
   defaults in `kb/config.py`. Credentials only through a `conn_id` (an Airflow connection) or
   the existing database settings, never in a URL (`_no_credentials`).
4. Register it in `build_store` (`kb/stores/__init__.py`) with a lazy import, like `pgvector` and
   `elasticsearch`. Packages it needs become an optional extra (`kb-<name>` in
   `pyproject.toml`); HTTP goes through `autoair.net.tls` with client `kb`.
5. Errors raise `StoreError` with a message that names the setting to fix and never the
   credentials.
6. Tests: add a fixture to `tests/unit/test_kb_store_contract.py` so the whole contract runs
   against the store. If it needs a server, make the fixture skip without its environment
   variable (as `AUTOAIR_TEST_ELASTICSEARCH_URL` and `AUTOAIR_TEST_POSTGRES_URL` do) and give it
   an in-memory fake when that is practical (`tests/kb_fake_es.py`).
7. Docs: `website/docs/ai/knowledge-base-stores.md` (a section per store and "Switching
   stores"), CHANGELOG.

## A source kind

1. Add the kind to `SourceKind` in `kb/config.py`, describe it in the `kind` field description,
   and extend `KbSourceConfig._needs_url` when it needs a `url` or `conn_id`.
2. Plan its units in `plan_units` (`kb/sources.py`). HTTP sources read through `_HttpReader`
   (SSRF checks; `own_host`, `private=source.allow_private`); folders, git checkouts and object
   storage through `open_fs` (`kb/fetch.py`) and `_FsReader` (size limit `kb.fetch.max_page_bytes`). Raise
   `SourceError` when a source cannot be listed.
3. A new reason why a source cannot be read goes into `UNUSABLE_REASONS` / `unusable_code`, and
   the admin UI needs `admin:kb.reason.<code>` in `frontend/src/i18n/locales/{en,tr}/admin.json`.
4. Tests in `tests/unit/test_kb_sources.py` with local folders or `tests/kb_fake.py`
   (`FakeDocs`, `make_kb`); no network.
5. Docs: "Source kinds" in `website/docs/ai/knowledge-base-sources.md`, CHANGELOG.

## Check

```bash
uv run pytest -q tests/unit/test_kb_store_contract.py tests/unit/test_kb_sources.py tests/unit/test_kb_config.py
uv run pytest -q tests/integration/test_kb_api.py
uv run python scripts/gen_config_docs.py && uv run python scripts/gen_config_docs.py --check
uv run nox -s noai        # the KB still works without autoair[ai]
```

## Done when

- [ ] Contract tests pass for the new store (or source tests for the new kind)
- [ ] No pydantic-ai import reachable from `autoair.kb`; `nox -s noai` passes
- [ ] Settings described, config reference regenerated, en + tr strings for new reason codes
- [ ] Website page and CHANGELOG updated
