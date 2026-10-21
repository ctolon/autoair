"""Ingestion pipeline and retrieval against a fake docs site (no network)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx2
import pytest

from autoair.ai.net import UrlNotAllowed
from autoair.kb.config import KbFetchSettings
from autoair.kb.fetch import HttpFetcher, PageTooLarge
from autoair.kb.ingest import AlreadyRunning, Ingestor
from tests.kb_fake import ASTRO, DOCS, FakeDocs, FakeEmbedder, make_kb, sphinx_index, sphinx_page


def _urls(kb: object) -> set[str]:
    return {p.url for p in kb.store.pages().values()}  # type: ignore[attr-defined]


def test_refresh_indexes_components_versions_and_citations(tmp_path: Path) -> None:
    fake = FakeDocs()
    kb = make_kb(tmp_path, fake)
    status = kb.refresh()
    assert status.outcome == "ok", status.errors
    stats = kb.stats()
    assert set(stats.by_component) == {
        "core",
        "task_sdk",
        "providers",
        "helm_chart",
        "airflowctl",
        "astronomer_airflow",
    }
    assert stats.versions["core"] == [fake.airflow]
    assert stats.versions["task_sdk"] == ["stable"]  # installed version not published
    assert any(e.level == "info" and "using stable" in e.message for e in status.errors)
    urls = _urls(kb)
    # excluded: genindex, the API reference, astronomer links on foreign hosts
    assert not [u for u in urls if "genindex" in u or "_api/" in u or "evil" in u]
    assert f"{ASTRO}airflow/airflow-passing-data-between-tasks" in urls
    result = kb.search("jinja template not found bash_command")
    assert result.mode == "lexical"
    top = result.hits[0]
    # short sections are merged (kb.chunk.min_chars): the chunk links to the first one
    assert "/howto/operator/bash.html#" in top.url
    assert "Jinja template not found" in top.text
    assert (top.component, top.package, top.version) == ("core", "apache-airflow", fake.airflow)
    assert top.source == "official"
    assert top.title == "BashOperator"
    assert "Jinja" in top.snippet


def test_air_gapped_default_skips_official_source(tmp_path: Path) -> None:
    fake = FakeDocs()
    kb = make_kb(tmp_path, fake, online=False)
    status = kb.refresh()
    assert status.sources[0].state == "skipped"
    assert fake.requests == []
    assert kb.search("bash").mode == "lexical"
    assert kb.search("bash").hits == []


def test_incremental_refresh_skips_unchanged_units_and_pages(tmp_path: Path) -> None:
    fake = FakeDocs()
    kb = make_kb(tmp_path, fake)
    kb.refresh()
    before = len(fake.requests)
    second = kb.refresh()
    fetched = fake.requests[before:]
    # only the indexes (searchindex.js / llms.txt) are read again: every unit is unchanged
    assert all(u.endswith(("searchindex.js", "llms.txt")) for u in fetched), fetched
    run = second.sources[0]
    assert run.pages_changed == 0
    assert run.pages_unchanged == run.pages_seen > 0
    # A changed index: pages are fetched conditionally (304), one page really changed.
    core = f"{DOCS}apache-airflow/{fake.airflow}/"
    fake.put(
        core + "searchindex.js",
        sphinx_index(
            {"index": "Apache Airflow", "howto/operator/bash": "BashOperator", "new": "New page"}
        ),
        "text/javascript",
    )
    fake.put(core + "new.html", sphinx_page("New page", [("x", "X", "<p>brand new text</p>")]))
    fake.put(core + "index.html", sphinx_page("Apache Airflow", [("y", "Y", "<p>changed</p>")]))
    third = kb.refresh().sources[0]
    assert fake.conditional_hits >= 1  # bash.html answered 304
    assert third.pages_changed == 2  # new.html + index.html
    assert kb.search("brand new text").hits[0].url.endswith("/new.html#x")


def test_removed_pages_are_deleted_only_after_a_complete_listing(tmp_path: Path) -> None:
    fake = FakeDocs()
    kb = make_kb(tmp_path, fake)
    kb.refresh()
    core = f"{DOCS}apache-airflow/{fake.airflow}/"
    bash = core + "howto/operator/bash.html"
    assert bash in _urls(kb)
    # The listing fails: nothing is removed.
    fake.put(core + "searchindex.js", "garbage", "text/javascript")
    status = kb.refresh()
    assert status.sources[0].state == "failed"
    assert status.outcome == "failed"
    assert bash in _urls(kb)
    # The page is gone from a complete listing: removed.
    fake.put(core + "searchindex.js", sphinx_index({"index": "Apache Airflow"}), "text/javascript")
    run = kb.refresh().sources[0]
    assert run.pages_removed == 1
    assert bash not in _urls(kb)


def test_version_change_replaces_old_version_pages(tmp_path: Path) -> None:
    fake = FakeDocs()
    kb = make_kb(tmp_path, fake, versions=["stable"])
    stable = f"{DOCS}apache-airflow/stable/"
    fake.put(stable + "searchindex.js", sphinx_index({"index": "Airflow"}), "text/javascript")
    fake.put(stable + "index.html", sphinx_page("Airflow", [("a", "A", "<p>stable docs</p>")]))
    kb.refresh()
    assert kb.stats().versions["core"] == ["stable"]
    kb2 = make_kb(tmp_path, fake, versions=["installed", "3.0.9"])
    v309 = f"{DOCS}apache-airflow/3.0.9/"
    fake.put(v309 + "searchindex.js", sphinx_index({"index": "Airflow"}), "text/javascript")
    fake.put(v309 + "index.html", sphinx_page("Airflow", [("a", "A", "<p>old docs</p>")]))
    kb2.refresh()
    assert kb2.stats().versions["core"] == sorted([fake.airflow, "3.0.9"])
    hits = kb2.search("old docs", filters={"version": "3.0.9"}).hits
    assert hits
    assert all(h.version == "3.0.9" for h in hits)


def test_filters_and_result_cap(tmp_path: Path) -> None:
    kb = make_kb(tmp_path, FakeDocs(), search={"max_results": 3})
    kb.refresh()
    hits = kb.search("operator", k=50, filters={"component": ["providers"]}).hits
    assert hits
    assert len(hits) <= 3
    assert {h.component for h in hits} == {"providers"}
    assert kb.search("operator", filters={"package": "nope"}).hits == []


def test_hybrid_search_with_embeddings_and_model_change(tmp_path: Path) -> None:
    fake = FakeDocs()
    emb = FakeEmbedder()
    kb = make_kb(tmp_path, fake, embedder=emb)
    kb.refresh()
    stats = kb.stats()
    assert stats.vectors == stats.chunks > 0
    assert stats.embedding_model == emb.embedding_model
    result = asyncio.run(kb.asearch("triggerer worker slot deferrable"))
    assert result.mode == "hybrid"
    assert result.hits[0].url.endswith("sensors/datetime.html#deferrable")
    assert kb.status().mode == "hybrid"
    # Another embedding model: vectors are rebuilt from the stored chunks, no refetch.
    other = FakeEmbedder(dim=32)
    other.embedding_model = "fake-embedding-2"
    kb2 = make_kb(tmp_path, fake, embedder=other)
    before = len(fake.requests)
    kb2.refresh()
    assert all(u.endswith(("searchindex.js", "llms.txt")) for u in fake.requests[before:])
    stats2 = kb2.stats()
    assert stats2.embedding_model == "fake-embedding-2"
    assert stats2.vectors == stats2.chunks


def test_embedding_failures_keep_the_keyword_index(tmp_path: Path) -> None:
    class Broken(FakeEmbedder):
        def embed(self, texts: object) -> list[list[float]]:  # type: ignore[override]
            raise RuntimeError("provider down")

    kb = make_kb(tmp_path, FakeDocs(), embedder=Broken())
    status = kb.refresh()
    assert any("embeddings failed" in e.message for e in status.errors)
    assert kb.stats().chunks > 0
    assert kb.stats().vectors == 0
    assert kb.search("bash").hits


def test_chunk_settings_change_rechunks_without_fetching(tmp_path: Path) -> None:
    fake = FakeDocs()
    make_kb(tmp_path, fake).refresh()
    kb = make_kb(tmp_path, fake, chunk={"size": 300, "overlap": 20, "min_chars": 0})
    before = len(fake.requests)
    kb.refresh()
    assert all(u.endswith(("searchindex.js", "llms.txt")) for u in fake.requests[before:])
    assert kb.store.get_meta("chunk_config") is not None


def test_only_one_ingestion_at_a_time(tmp_path: Path) -> None:
    fake = FakeDocs()
    kb = make_kb(tmp_path, fake)
    holder = kb.state.new_owner()
    assert kb.state.acquire(holder, 600)
    with pytest.raises(AlreadyRunning):
        kb.refresh()
    with pytest.raises(AlreadyRunning):
        kb.start_refresh()
    kb.state.release(holder)
    # An expired lease (dead replica) is taken over.
    assert kb.state.acquire(holder, -1)
    assert kb.refresh().outcome == "ok"
    assert kb.state.holder() is None


def test_cancel_stops_the_run(tmp_path: Path) -> None:
    fake = FakeDocs()
    kb = make_kb(tmp_path, fake)
    ing = Ingestor(kb)
    original = ing.progress

    def progress(**kw: object) -> None:
        kb.state.update_status(lambda s: setattr(s, "cancel_requested", True))
        original(force=True, **{k: v for k, v in kw.items() if k != "force"})

    ing.progress = progress  # type: ignore[method-assign]
    status = ing.run()
    assert status.outcome == "cancelled"
    assert status.state == "idle"


def test_page_and_time_limits(tmp_path: Path) -> None:
    kb = make_kb(tmp_path, FakeDocs(), fetch={"max_pages": 2})
    status = kb.refresh()
    assert status.outcome == "partial"
    assert kb.stats().pages == 2
    assert any("page limit" in e.message for e in status.errors)


def test_status_persisted_for_other_replicas(tmp_path: Path) -> None:
    fake = FakeDocs()
    make_kb(tmp_path, fake).refresh()
    other = make_kb(tmp_path, fake)  # another process reading the same state
    status = other.status()
    assert status.run.outcome == "ok"
    assert status.run.last_success
    assert status.store.pages > 0
    assert status.sources[0].pages == status.store.pages


def test_source_override_disables_and_narrows(tmp_path: Path) -> None:
    from autoair.kb.models import SourceOverride

    fake = FakeDocs()
    kb = make_kb(tmp_path, fake)
    info = kb.set_source("official", SourceOverride(components=["helm_chart"]))
    assert info.components == ["helm_chart"]
    assert info.overridden
    kb.refresh()
    assert set(kb.stats().by_component) == {"helm_chart"}
    kb.set_source("official", SourceOverride(enabled=False))
    assert kb.refresh().sources == []
    with pytest.raises(ValueError, match="unknown components"):
        kb.set_source("official", SourceOverride(components=["bogus"]))
    with pytest.raises(KeyError):
        kb.set_source("nope", None)
    kb.set_source("official", None)
    assert not kb.source_infos()[0].overridden


# --------------------------------------------------------------------------- fetch policy


def _fetcher(handler: object, **settings: object) -> HttpFetcher:
    cfg = KbFetchSettings(requests_per_second=100, retries=1, **settings)  # type: ignore[arg-type]
    return HttpFetcher(cfg, transport=httpx2.MockTransport(handler), resolve_dns=False)  # type: ignore[arg-type]


def test_fetch_refuses_hosts_outside_the_allow_list_and_redirects_out() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/hop":
            return httpx2.Response(302, headers={"location": "https://evil.example.com/x"})
        return httpx2.Response(200, text="ok")

    f = _fetcher(handler)
    with pytest.raises(UrlNotAllowed):
        f.get("https://evil.example.com/page")
    with pytest.raises(UrlNotAllowed):
        f.get("https://airflow.apache.org/hop")
    with pytest.raises(UrlNotAllowed):
        f.get("https://user:pw@airflow.apache.org/x")
    assert f.get("https://airflow.apache.org/docs/").status == 200


def test_fetch_blocks_private_addresses_unless_allowed() -> None:
    cfg = KbFetchSettings(allowed_domains=["127.0.0.1"])
    f = HttpFetcher(cfg, transport=httpx2.MockTransport(lambda r: httpx2.Response(200)))
    with pytest.raises(UrlNotAllowed):
        f.get("http://127.0.0.1/docs/")
    assert f.get("http://127.0.0.1/docs/", allow_private=True).status == 200


def test_fetch_size_limit_and_retries() -> None:
    calls: list[int] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(1)
        if request.url.path == "/big":
            return httpx2.Response(200, content=b"x" * (70 * 1024))
        if len(calls) == 2:
            return httpx2.Response(503, headers={"retry-after": "0"})
        return httpx2.Response(200, text="fine")

    f = _fetcher(handler, max_page_bytes=64 * 1024)
    with pytest.raises(PageTooLarge):
        f.get("https://airflow.apache.org/big")
    assert f.get("https://airflow.apache.org/again").text() == "fine"
    assert len(calls) == 3


def test_private_mirror_allows_only_its_own_host() -> None:
    cfg = KbFetchSettings(requests_per_second=100)
    f = HttpFetcher(cfg, transport=httpx2.MockTransport(lambda r: httpx2.Response(200)))
    assert f.get("http://10.1.2.3/docs/", domains=["10.1.2.3"], allow_private=True).status == 200
    with pytest.raises(UrlNotAllowed):  # the public allow-list is not extended to privates
        f.get("https://airflow.apache.org/docs/", domains=["10.1.2.3"], allow_private=True)


def test_changing_a_source_filter_relists_its_units(tmp_path: Path) -> None:
    fake = FakeDocs()
    narrow = [{"id": "official", "kind": "official", "include": ["index"]}]
    kb = make_kb(tmp_path, fake, sources=narrow, components=["core"])
    kb.refresh()
    assert {u.rsplit("/", 1)[-1] for u in _urls(kb)} == {"index.html"}
    wide = make_kb(tmp_path, fake, components=["core"])
    wide.refresh()
    assert {u.rsplit("/", 1)[-1] for u in _urls(wide)} == {"index.html", "bash.html"}


def test_unusable_sources_carry_a_reason_code(tmp_path: Path) -> None:
    """The admin UI translates why a source cannot be read through ``reason_code``."""
    from autoair.kb.models import SourceOverride

    kb = make_kb(tmp_path, FakeDocs(), online=False)
    info = kb.source_infos()[0]
    assert (info.usable, info.reason_code) == (False, "air_gapped")
    assert "kb.online" in (info.reason or "")
    kb.set_source("official", SourceOverride(enabled=False))
    info = kb.source_infos()[0]
    assert (info.reason, info.reason_code) == ("disabled", "disabled")
