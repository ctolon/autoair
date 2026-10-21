"""The knowledge base's built-in ``autoair`` source: AutoAir's packaged documentation."""

from __future__ import annotations

from pathlib import Path

from autoair import __version__
from autoair.kb.components import COMPONENTS, component_infos
from tests.kb_fake import FakeDocs, make_kb

AUTOAIR_ONLY = [{"id": "autoair", "kind": "autoair"}]


def test_default_sources_index_autoair_docs_air_gapped(tmp_path: Path) -> None:
    fake = FakeDocs()
    # The defaults: official (skipped, kb.online is false) and the packaged AutoAir docs.
    kb = make_kb(
        tmp_path,
        fake,
        online=False,
        sources=[{"id": "official", "kind": "official"}, *AUTOAIR_ONLY],
    )
    assert kb.any_usable()
    status = kb.refresh()
    assert status.outcome == "ok", status.errors
    runs = {r.source: r for r in status.sources}
    assert runs["official"].state == "skipped"
    assert runs["autoair"].state == "ok"
    assert runs["autoair"].pages_changed > 50
    assert fake.requests == []  # no network at all
    stats = kb.stats()
    assert set(stats.by_component) == {"autoair"}
    assert stats.versions["autoair"] == [__version__]
    result = kb.search("install AutoAir as an Airflow plugin", filters={"component": ["autoair"]})
    assert result.hits
    top = result.hits[0]
    assert top.url.startswith("https://ctolon.github.io/autoair/docs/")
    assert (top.source, top.component, top.package, top.version) == (
        "autoair",
        "autoair",
        "autoair",
        __version__,
    )
    assert any(
        h.url.startswith("https://ctolon.github.io/autoair/docs/installation/plugin")
        for h in result.hits
    )


def test_unchanged_docs_are_skipped_and_the_component_switch_removes_them(tmp_path: Path) -> None:
    kb = make_kb(tmp_path, None, online=False, sources=AUTOAIR_ONLY)
    first = kb.refresh()
    pages = first.sources[0].pages_changed
    again = kb.refresh()
    assert again.sources[0].pages_changed == 0
    assert again.sources[0].pages_unchanged == pages
    off = make_kb(
        tmp_path,
        None,
        online=False,
        sources=[{"id": "autoair", "kind": "autoair", "components": ["core"]}],
    )
    removed = off.refresh()
    assert removed.sources[0].pages_removed == pages
    assert off.stats().pages == 0


def test_include_patterns_select_pages(tmp_path: Path) -> None:
    kb = make_kb(
        tmp_path,
        None,
        online=False,
        sources=[{"id": "autoair", "kind": "autoair", "include": ["ai/*"]}],
    )
    kb.refresh()
    urls = {p.url for p in kb.store.pages().values()}
    assert urls
    assert all("/docs/ai/" in u for u in urls)


def test_source_info_and_component() -> None:
    assert COMPONENTS["autoair"].default is True
    info = next(c for c in component_infos() if c.id == "autoair")
    assert info.family == "autoair"
