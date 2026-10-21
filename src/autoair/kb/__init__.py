"""Documentation knowledge base: RAG over the Apache Airflow and Astronomer docs.

* :mod:`.config` - the ``kb`` settings section (import-light);
* :mod:`.sources`, :mod:`.fetch`, :mod:`.parse`, :mod:`.chunk` - what is read and how;
* :mod:`.ingest` - the incremental, lease-protected ingestion pipeline;
* :mod:`.stores` - local SQLite (default), pgvector, Elasticsearch;
* :mod:`.service` - ``kb_for(services)``: status, search (BM25 + embeddings, RRF,
  reranker), refreshes; :mod:`.bundle` - signed bundles for air-gapped installs;
* :mod:`.tools`, :mod:`.mcp`, :mod:`.cli`, :mod:`.scheduler` - agents, MCP, CLI, refreshes.

Nothing here imports pydantic-ai: keyword search works without ``autoair[ai]``.
"""

from __future__ import annotations
