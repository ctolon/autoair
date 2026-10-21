---
paths:
  - "src/autoair/kb/**"
  - "src/autoair/api/routers/kb.py"
  - "frontend/src/admin/tabs/KbTab.tsx"
  - "tests/**/test_kb_*.py"
  - "website/docs/ai/knowledge-base*.md"
---

# Documentation knowledge base (`autoair.kb`)

Architecture: `docs/ai-architecture.md` section 9a. New store backend or source kind: skill
`add-kb-store-or-source`.

- The KB works without `autoair[ai]`: it never imports pydantic-ai, has its own settings
  section (`kb.*` in `kb/config.py`) and flag (`features.kb`); without an embedder, search is
  BM25 only. `uv run nox -s noai` runs `tests/integration/test_kb_noai.py`.
- Pipeline: `sources.py` -> `fetch.py` -> `parse.py` -> `chunk.py` -> `ingest.py`. Unchanged
  units are skipped; pages are removed only after a complete listing.
- Replicas: one ingestion run per index holds the lease document `lock-<store>` in the
  `kb_state` collection (heartbeat-renewed); `status-<store>` carries progress for every
  replica.
- Stores implement `KbStore` (`kb/stores/base.py`). Writes may be buffered (Elasticsearch
  `_bulk`) until `KbStore.flush()`, which the ingestion run calls once at its end; the store
  returns lexical and dense rankings and `service.py` fuses them, so every store behaves the
  same. `tests/unit/test_kb_store_contract.py` runs against every store.
- HTTP goes through `net/tls.py` client `kb` and the SSRF check in `ai/net.py`; a source host
  may be private only with `allow_private`. Credentials come from an Airflow `conn_id`; URLs
  never carry credentials (validated in `KbSourceConfig`), and `StoreError` messages never
  include them.
- Why a source cannot be read is a code (`UNUSABLE_REASONS` in `sources.py`, `reason_code` in
  the API) translated in the admin UI as `admin:kb.reason.<code>`; a new code needs en and tr.
- Tests: HTTP fakes in `tests/kb_fake.py`, Elasticsearch in-memory fake in
  `tests/kb_fake_es.py`. A real Elasticsearch only from a throw-away container:
  `AUTOAIR_TEST_ELASTICSEARCH_URL=http://127.0.0.1:<port> uv run pytest -q
  tests/unit/test_kb_store_contract.py`; pgvector with `AUTOAIR_TEST_POSTGRES_URL`.
