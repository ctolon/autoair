---
title: Knowledge base stores
description: Where the documentation index lives - the dependency-free local SQLite store, PostgreSQL with pgvector, or Elasticsearch - with setup, TLS and multi-replica notes.
sidebar_label: Stores
---

`kb.store` selects the index backend. All three implement the same interface and pass the same
contract tests; AutoAir fuses the lexical and vector rankings itself, so results do not depend on
store-specific ranking features.

| `kb.store` | Install | Lexical ranking | Vectors | Several replicas |
|---|---|---|---|---|
| `local` (default) | nothing | SQLite FTS5 `bm25()` | float32 blobs, brute force | one index per host (or a shared file) |
| `pgvector` | `autoair[kb-pgvector]` + the `vector` extension | `tsvector` + `ts_rank_cd` (GIN index) | `vector(n)`, HNSW cosine index | shared |
| `elasticsearch` | `autoair[kb-elasticsearch]` (no packages) | BM25 `match` | `dense_vector`, kNN cosine | shared |

Every store keeps the parsed sections of each page (compressed). That is what makes bundle
exports, re-chunking after a `kb.chunk` change and re-embedding after a model change possible
without fetching again.

The ingestion lease and status are kept per index (the store's identity), in AutoAir's state
backend, so replicas that share an index refresh it one at a time.

## Local (SQLite)

The index is one SQLite file, `kb.local.path` (default `<cache_dir or state>/kb/kb.sqlite3`), in
WAL mode so other processes on the host can read while one writes.

- Keyword search uses FTS5 over AutoAir's identifier-aware token stream; titles and headings
  count three times. A Python build without FTS5 falls back to an in-memory BM25 index over the
  same rows.
- Vectors are searched by brute force, with numpy when it is installed. For a large index with
  embeddings (tens of thousands of chunks) prefer pgvector or Elasticsearch.
- `kb.local.shared` (false): set it when the file is on storage every replica uses; one replica
  then indexes for all. With the default, each host keeps and refreshes its own copy (the lease
  is per host).

Read-only calls (status, search) never create the file.

## PostgreSQL with pgvector

```bash
pip install 'autoair[kb-pgvector]'
AUTOAIR_KB__STORE=pgvector
# default: AutoAir's own database when database.backend=postgresql
AUTOAIR_KB__PGVECTOR__CONN_ID=kb_postgres          # or
AUTOAIR_KB__PGVECTOR__URL=postgresql+psycopg://kb-db.corp:5432/autoair_kb
```

Where the connection comes from, first match wins:

1. `kb.pgvector.conn_id`: an Airflow Postgres connection (host, port, schema = database,
   login, password);
2. `kb.pgvector.url`: a SQLAlchemy URL **without** credentials (a URL with a user name or
   password is rejected); libpq's environment and `.pgpass` supply them;
3. AutoAir's own database (`database.url`, `database.password`) when `database.backend` is
   `postgresql`.

Tables: `<table_prefix>pages`, `<table_prefix>chunks`, `<table_prefix>meta`
(`kb.pgvector.table_prefix`, default `autoair_kb_`), in `kb.pgvector.schema_name` when set.
On open AutoAir runs `CREATE EXTENSION IF NOT EXISTS vector`, which needs the privilege to create
extensions. If the AutoAir role lacks it, have a superuser run `CREATE EXTENSION vector;` once
in that database and set `kb.pgvector.create_extension=false`. The embedding column gets its
dimension from the first vectors written, and an HNSW index (`kb.pgvector.hnsw_index`, needs
pgvector 0.5 or later) is created then. Changing the embedding model clears the vectors and
re-embeds.

**TLS.** psycopg uses libpq's own network stack: `outbound.*` does not apply. Set
`kb.pgvector.ssl_mode` (`require`, `verify-ca`, `verify-full`, ...) and `kb.pgvector.ssl_root_cert`.
`kb.pgvector.pool_size` (4) bounds the connections.

## Elasticsearch

```bash
pip install 'autoair[kb-elasticsearch]'   # stable install name; installs nothing
AUTOAIR_KB__STORE=elasticsearch
AUTOAIR_KB__ELASTICSEARCH__URL=https://es.corp:9200
AUTOAIR_KB__ELASTICSEARCH__CONN_ID=kb_elasticsearch
```

AutoAir talks to the REST API with its own HTTP client (httpx2 through the `kb` client of
`outbound.*`), so the CA bundle, client certificate and proxy configured there apply, for example
`AUTOAIR_OUTBOUND__CLIENTS='{"kb": {"ca_bundle": "/etc/ssl/es-ca.pem"}}'`. The official
`elasticsearch` Python client is not used: its transport (`elastic-transport`, on urllib3, or an
httpx node that is not httpx2) would bypass those settings. The fusion of BM25 and kNN happens in
AutoAir, so Elasticsearch's `rrf` retriever, a paid feature, is not needed. Elasticsearch 8.x and
9.x work.

Credentials: the connection's `extra.api_key` (sent as `Authorization: ApiKey ...`), else its
login and password (basic auth). Without `url` the connection's host and port are used. The
credentials exist only in the client's request headers; they are never logged or returned.

Indices: `<index_prefix>-pages` (metadata and the compressed body, not searchable),
`<index_prefix>-chunks` and `<index_prefix>-meta` (`kb.elasticsearch.index_prefix`, default
`autoair-kb`), created with `kb.elasticsearch.shards` (1) and `replicas` (1). The token stream is
indexed with a whitespace analyzer. The `dense_vector` field (cosine) is added with the first
embeddings; because a vector mapping cannot change, a new embedding model drops the chunks index
and the next refresh re-chunks and re-embeds the stored pages. Other settings:
`timeout_seconds` (30), `num_candidates` (200, kNN candidates per shard), `bulk_size` (500).

The AutoAir user needs the `read`, `write` and `manage` index privileges on
`<index_prefix>-*` (`manage` covers creating the indices, the mapping update for vectors and
dropping the chunks index).

## Switching stores

The index is not migrated between stores. After changing `kb.store`, run a refresh: pages are
fetched again (or import a bundle exported from the old store with `GET /api/kb/export`).

A Docker Compose demo with pgvector and Elasticsearch is in `deploy/compose/` (see its
README, section *Knowledge base stores*).
