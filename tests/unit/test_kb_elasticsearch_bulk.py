"""Elasticsearch store write path: batched ``_bulk`` writes and one refresh per ingest run."""

from __future__ import annotations

import json
from pathlib import Path

import httpx2
import pytest

from autoair.kb.chunk import chunk_page
from autoair.kb.config import KbChunkSettings, KbElasticsearchSettings
from autoair.kb.ingest import encode_body
from autoair.kb.models import PageDoc, PageRecord, Section
from autoair.kb.stores.base import StoreError, query_tokens
from autoair.kb.stores.elasticsearch import ElasticsearchStore
from tests.kb_fake import FakeDocs, make_kb
from tests.kb_fake_es import FakeElasticsearch


def _store(fake: FakeElasticsearch, **settings: int) -> ElasticsearchStore:
    store = ElasticsearchStore(
        "http://es.invalid:9200",
        KbElasticsearchSettings(index_prefix="kb", replicas=0, **settings),
        transport=fake.transport(),
    )
    store.open()
    fake.reset_requests()
    return store


def _page(n: int, sections: int = 1, text: str = "deferrable sensors") -> PageDoc:
    return PageDoc(
        id=f"page{n}",
        source="s1",
        unit="s1:core:3.1.0",
        component="core",
        package="apache-airflow",
        version="3.1.0",
        url=f"https://docs/p{n}.html",
        title=f"Page {n}",
        content_hash=f"h{n}-{sections}",
        sections=[
            # long sections so that each one becomes its own chunk
            Section(heading=f"Part {i}", anchor=f"a{i}", text=f"{text} {i} " + "word " * 200)
            for i in range(sections)
        ],
    )


def _put(store: ElasticsearchStore, page: PageDoc) -> int:
    chunks = chunk_page(page, KbChunkSettings())
    meta = PageRecord.model_validate(
        {**page.model_dump(exclude={"sections"}), "chunks": len(chunks)}
    )
    store.put_page(meta, encode_body(page.sections), chunks, None, None)
    return len(chunks)


def test_put_page_is_buffered_and_flushed_with_one_bulk_and_one_refresh() -> None:
    fake = FakeElasticsearch()
    store = _store(fake)
    for n in range(5):
        _put(store, _page(n))
    assert fake.requests == []  # nothing sent per page
    store.flush()
    assert fake.count("POST", "/_bulk") == 1
    assert fake.count("POST", "/_refresh") == 1
    assert fake.count("POST", "/_delete_by_query") == 0
    assert set(store.pages()) == {f"page{n}" for n in range(5)}
    assert fake.count("POST", "/_refresh") == 1  # nothing pending: no second refresh


def test_reads_of_the_same_store_see_queued_writes() -> None:
    fake = FakeElasticsearch()
    store = _store(fake)
    _put(store, _page(1))
    assert [c.page_id for c, _ in store.lexical(query_tokens("deferrable"), 5, None)] == ["page1"]


def test_bulk_size_splits_requests_without_splitting_actions() -> None:
    fake = FakeElasticsearch()
    store = _store(fake, bulk_size=3)
    seen: list[str] = []
    handle = fake.handle

    def spy(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/_bulk":
            seen.append(request.content.decode())
        return handle(request)

    store._transport = httpx2.MockTransport(spy)
    store.close()  # rebuild the client with the spy transport
    for n in range(4):
        _put(store, _page(n, sections=2))  # 3 documents each: page + 2 chunks
    store.flush()
    assert len(seen) == 4
    for payload in seen:
        lines = [json.loads(x) for x in payload.splitlines()]
        actions = [x for x in lines if set(x) & {"index", "delete", "update"}]
        assert len(actions) <= 3
        # every index action is followed by its source line
        assert len(lines) == sum(1 if "delete" in a else 2 for a in actions)


def test_replacing_a_page_deletes_its_stale_chunks_by_id() -> None:
    fake = FakeElasticsearch()
    store = _store(fake)
    assert _put(store, _page(1, sections=3)) == 3
    store.flush()
    assert set(fake.live["kb-chunks"]) == {"page1#0", "page1#1", "page1#2"}
    assert _put(store, _page(1, sections=1, text="assets")) == 1
    store.flush()
    assert set(fake.live["kb-chunks"]) == {"page1#0"}
    # twice in one batch: the queued version is sent first, so the range stays right
    _put(store, _page(1, sections=4))
    _put(store, _page(1, sections=2))
    store.flush()
    assert set(fake.live["kb-chunks"]) == {"page1#0", "page1#1"}
    assert store.stats().chunks == 2


def test_touch_page_is_batched_and_ignores_deleted_pages() -> None:
    fake = FakeElasticsearch()
    store = _store(fake)
    _put(store, _page(1))
    store.flush()
    record = store.pages()["page1"]
    fake.reset_requests()
    store.touch_page(record.model_copy(update={"etag": '"e2"'}))
    store.touch_page(record.model_copy(update={"id": "gone", "etag": '"x"'}))
    assert fake.requests == []
    assert store.pages()["page1"].etag == '"e2"'
    assert fake.count("POST", "/_bulk") == 1


def test_bulk_item_errors_raise_with_the_first_reason() -> None:
    fake = FakeElasticsearch()
    store = _store(fake)
    with pytest.raises(StoreError, match=r"1 of 1 actions .*kb-chunks/nope.*missing"):
        store.put_vectors({"nope": [1.0, 0.0]})


def test_ingest_run_refreshes_once(tmp_path: Path) -> None:
    fake = FakeElasticsearch()
    store = _store(fake)
    kb = make_kb(tmp_path, FakeDocs(), store=store)
    status = kb.refresh()
    assert status.outcome == "ok", status.errors
    changed = sum(s.pages_changed for s in status.sources)
    assert changed > 1
    # one refresh before each source lists its pages (only when writes are pending), one
    # at the end; never one per page
    assert fake.count("POST", "/_refresh") <= len(status.sources) + 1
    assert fake.count("POST", "/_delete_by_query") == 0
    # a second replica (another store instance) sees the result without any flush
    other = ElasticsearchStore(
        "http://es.invalid:9200",
        KbElasticsearchSettings(index_prefix="kb", replicas=0),
        transport=fake.transport(),
    )
    assert len(other.pages()) == len(store.pages()) >= changed
    assert other.lexical(query_tokens("bash_command"), 3, None)
