"""Contract of every knowledge-base store.

Always runs against the local store and the Elasticsearch store on an in-memory fake
(``tests/kb_fake_es.py``); against pgvector when ``AUTOAIR_TEST_POSTGRES_URL``
points at a throw-away PostgreSQL with the ``vector`` extension available, and against
Elasticsearch when ``AUTOAIR_TEST_ELASTICSEARCH_URL`` is set (unique table prefix / index
prefix per test, removed afterwards).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from autoair.kb.chunk import chunk_page
from autoair.kb.config import KbChunkSettings, KbElasticsearchSettings, KbPgvectorSettings
from autoair.kb.ingest import decode_body, encode_body
from autoair.kb.models import PageDoc, PageRecord, Section
from autoair.kb.stores.base import KbStore, query_tokens
from autoair.kb.stores.local import LocalStore
from tests.kb_fake import FakeEmbedder
from tests.kb_fake_es import FakeElasticsearch

PG = os.environ.get("AUTOAIR_TEST_POSTGRES_URL")
ES = os.environ.get("AUTOAIR_TEST_ELASTICSEARCH_URL")


def _pgvector(tmp_path: Path) -> Iterator[KbStore]:
    from autoair.kb.stores.base import StoreError
    from autoair.kb.stores.pgvector import PgvectorStore

    prefix = f"t{uuid.uuid4().hex[:8]}_"
    store = PgvectorStore(PG or "", KbPgvectorSettings(table_prefix=prefix))
    try:
        store.open()
    except StoreError as exc:
        pytest.skip(f"pgvector unavailable: {exc}")
    try:
        yield store
    finally:
        for table in ("chunks", "pages", "meta"):
            store._run(f'DROP TABLE IF EXISTS "{prefix}{table}" CASCADE')
        store.close()


def _elasticsearch(tmp_path: Path) -> Iterator[KbStore]:
    from autoair.kb.stores.elasticsearch import ElasticsearchStore

    prefix = f"autoair-test-{uuid.uuid4().hex[:8]}"
    store = ElasticsearchStore(ES or "", KbElasticsearchSettings(index_prefix=prefix, replicas=0))
    store.open()
    try:
        yield store
    finally:
        store.flush()
        for suffix in ("pages", "chunks", "meta"):
            store._req("DELETE", f"/{prefix}-{suffix}", ok=(404,))
        store.close()


def _fake_elasticsearch(tmp_path: Path) -> Iterator[KbStore]:
    from autoair.kb.stores.elasticsearch import ElasticsearchStore

    fake = FakeElasticsearch()
    store = ElasticsearchStore(
        "http://es.invalid:9200",
        KbElasticsearchSettings(index_prefix="autoair-test", replicas=0),
        transport=fake.transport(),
    )
    store.open()
    yield store
    store.close()


def _local(tmp_path: Path) -> Iterator[KbStore]:
    store = LocalStore(tmp_path / "kb.sqlite3")
    store.open()
    yield store
    store.close()


BACKENDS = [
    pytest.param(_local, id="local"),
    pytest.param(
        _pgvector,
        id="pgvector",
        marks=pytest.mark.skipif(not PG, reason="AUTOAIR_TEST_POSTGRES_URL not set"),
    ),
    pytest.param(_fake_elasticsearch, id="elasticsearch-fake"),
    pytest.param(
        _elasticsearch,
        id="elasticsearch",
        marks=pytest.mark.skipif(not ES, reason="AUTOAIR_TEST_ELASTICSEARCH_URL not set"),
    ),
]


@pytest.fixture(params=BACKENDS)
def store(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[KbStore]:
    yield from request.param(tmp_path)


def _page(
    n: int, text: str, *, component: str = "core", version: str = "3.1.0", source: str = "s1"
) -> PageDoc:
    return PageDoc(
        id=f"page{n}",
        source=source,
        unit=f"{source}:{component}:{version}",
        component=component,
        package=f"pkg-{component}",
        version=version,
        url=f"https://docs/{component}/{version}/p{n}.html",
        title=f"Page {n}",
        content_hash=f"h{n}-{len(text)}",
        etag=f'"e{n}"',
        sections=[Section(heading=f"Page {n} > Part", anchor="part", text=text)],
    )


def _put(store: KbStore, page: PageDoc, emb: FakeEmbedder | None = None) -> int:
    chunks = chunk_page(page, KbChunkSettings())
    vectors = emb.embed([c.text for c in chunks]) if emb else None
    meta = PageRecord.model_validate(
        {**page.model_dump(exclude={"sections"}), "chunks": len(chunks)}
    )
    store.put_page(
        meta, encode_body(page.sections), chunks, vectors, emb.embedding_model if emb else None
    )
    return len(chunks)


def _ids(results: list[Any]) -> list[str]:
    return [c.page_id for c, _ in results]


def test_pages_chunks_lexical_and_filters(store: KbStore) -> None:
    _put(store, _page(1, "The S3KeySensor waits for a key in a bucket."))
    _put(store, _page(2, "KubernetesPodOperator runs a pod.", component="providers"))
    _put(store, _page(3, "Pools limit parallelism of tasks.", version="3.0.0"))
    pages = store.pages()
    assert set(pages) == {"page1", "page2", "page3"}
    assert pages["page1"].etag == '"e1"'
    assert pages["page1"].chunks == 1
    assert set(store.pages("s1")) == {"page1", "page2", "page3"}
    assert store.pages("zz") == {}
    hits = store.lexical(query_tokens("s3 key sensor"), 5, None)
    assert _ids(hits)[0] == "page1"
    assert hits[0][1] > 0
    chunk = hits[0][0]
    assert chunk.url == "https://docs/core/3.1.0/p1.html#part"
    assert chunk.heading == "Page 1 > Part"
    assert store.lexical(query_tokens("pod"), 5, {"component": ["core"]}) == []
    assert _ids(store.lexical(query_tokens("pod"), 5, {"component": ["providers"]})) == ["page2"]
    assert _ids(store.lexical(query_tokens("pools"), 5, {"version": ["3.0.0"]})) == ["page3"]
    assert store.lexical([], 5, None) == []
    stats = store.stats()
    assert (stats.pages, stats.chunks) == (3, 3)
    assert stats.by_component == {"core": 2, "providers": 1}
    assert sorted(stats.versions["core"]) == ["3.0.0", "3.1.0"]


def test_replace_touch_delete_and_clear(store: KbStore) -> None:
    _put(store, _page(1, "first version about xcom"))
    _put(store, _page(1, "second version about assets"))
    assert store.lexical(query_tokens("xcom"), 5, None) == []
    assert _ids(store.lexical(query_tokens("assets"), 5, None)) == ["page1"]
    record = store.pages()["page1"]
    store.touch_page(record.model_copy(update={"etag": '"new"', "fetched_at": "2026-01-01"}))
    assert store.pages()["page1"].etag == '"new"'
    bodies = list(store.iter_bodies())
    assert len(bodies) == 1
    assert decode_body(bodies[0][1])[0].text == "second version about assets"
    _put(store, _page(2, "other source text", source="s2"))
    assert store.delete_pages(["page1"]) == 1
    assert store.lexical(query_tokens("assets"), 5, None) == []
    store.set_meta("unit:x", "fp")
    assert store.get_meta("unit:x") == "fp"
    store.clear("s2")
    assert store.pages() == {}
    _put(store, _page(3, "again"))
    store.clear()
    assert store.pages() == {}
    assert store.get_meta("unit:x") is None


def test_vectors_dense_search_and_model_reset(store: KbStore) -> None:
    emb = FakeEmbedder()
    store.reset_vectors(emb.embedding_model)
    assert store.vector_model() == emb.embedding_model
    _put(store, _page(1, "deferrable sensors wait in the triggerer"), emb)
    _put(store, _page(2, "helm chart values for kubernetes"), emb)
    _put(store, _page(3, "triggerer deferrable", component="providers"), emb)
    assert store.stats().vectors == 3
    q = emb.embed(["deferrable triggerer sensors"])[0]
    dense = store.dense(q, 2, None)
    assert _ids(dense)[0] in ("page1", "page3")
    assert len(dense) == 2
    assert _ids(store.dense(q, 5, {"component": ["providers"]})) == ["page3"]
    assert store.dense([0.1, 0.2], 5, None) == []  # other dimension: no results
    assert store.missing_vectors(10) == []
    # chunks written without vectors are reported missing, then filled
    _put(store, _page(4, "no vector yet"))
    missing = store.missing_vectors(10)
    assert [c.page_id for c in missing] == ["page4"]
    store.put_vectors({missing[0].id: emb.embed([missing[0].text])[0]})
    assert store.missing_vectors(10) == []
    store.reset_vectors("other-model")
    assert store.vector_model() == "other-model"
    assert store.dense(q, 5, None) == []
