"""AutoAir's own documentation: packaging (website sources -> pages.json), search and read."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from autoair.docs import build as docs_build
from autoair.docs.index import (
    DATA_PATH,
    MAX_READ_CHARS,
    MIN_READ_CHARS,
    DocsIndex,
    DocsUnavailable,
    checkout_root,
    docs_index,
    load_data,
    slug,
)

ROOT = Path(__file__).resolve().parents[2]
SITE = "https://ctolon.github.io/autoair"

MDX_PAGE = """---
title: Deploy targets
description: Where AutoAir writes DAG files.
sidebar_label: Deploy
---

import Tabs from '@theme/Tabs';

# Deploy targets

Intro with a [link](/docs/admin/iam#roles) and a [relative one](other.md#x) and
[a schema](pathname:///schemas/dag-spec.schema.json). Escaped \\{braces\\}.

{/* an MDX comment */}
<!-- an HTML comment -->

<Screenshot name="deploy" alt="The deploy dialog"
  caption="Deploy with a pull request." />

:::tip Pull requests
Use a pull request.
:::

## Git & pull requests

```python title="dag.py"
import os  # kept verbatim, not an MDX import
```

<Video name="tour" title="A tour" />
"""


def _repo(tmp: Path) -> Path:
    (tmp / "website" / "scripts").mkdir(parents=True)
    shutil.copy(ROOT / "website" / "site.config.ts", tmp / "website" / "site.config.ts")
    shutil.copy(
        ROOT / "website" / "scripts" / "sync-reference.mjs",
        tmp / "website" / "scripts" / "sync-reference.mjs",
    )
    docs = tmp / "website" / "docs"
    (docs / "user-guide").mkdir(parents=True)
    (docs / "reference").mkdir()
    (docs / "user-guide" / "deploy.mdx").write_text(MDX_PAGE, encoding="utf-8")
    (docs / "user-guide" / "other.md").write_text("# Other\n\nText.\n", encoding="utf-8")
    (docs / "user-guide" / "draft.md").write_text(
        "---\ntitle: Draft\ndraft: true\n---\n\nHidden.\n", encoding="utf-8"
    )
    (docs / "intro.mdx").write_text(
        '---\ntitle: "What is AutoAir"\n---\n\nAutoAir builds DAGs.\n', encoding="utf-8"
    )
    # The site's synced copy is ignored: the reference comes from docs/.
    (docs / "reference" / "configuration.md").write_text("stale copy\n", encoding="utf-8")
    (tmp / "docs").mkdir()
    (tmp / "docs" / "configuration-reference.md").write_text(
        "# Configuration reference\n\nSee [configuration.md](configuration.md#files).\n\n"
        "## Top level\n\n| `AUTOAIR_MODE` | `mode` |\n",
        encoding="utf-8",
    )
    return tmp


def test_build_cleans_mdx_and_rewrites_links(tmp_path: Path) -> None:
    data = docs_build.build(_repo(tmp_path))
    assert data["kind"] == docs_build.KIND
    assert data["format"] == docs_build.FORMAT
    assert data["docs_url"] == f"{SITE}/docs/"
    assert data["llms_txt"] == f"{SITE}/llms.txt"
    pages = {p["path"]: p for p in data["pages"]}
    assert set(pages) == {
        "intro",
        "user-guide/deploy",
        "user-guide/other",
        "reference/configuration",
    }
    page = pages["user-guide/deploy"]
    assert page["title"] == "Deploy targets"
    assert page["description"] == "Where AutoAir writes DAG files."
    assert page["url"] == f"{SITE}/docs/user-guide/deploy"
    assert page["section"] == "user-guide"
    assert page["source"] == "website/docs/user-guide/deploy.mdx"
    md = page["markdown"]
    assert not md.startswith("# ")  # the title is a field
    assert "import Tabs" not in md
    assert "MDX comment" not in md
    assert "HTML comment" not in md
    assert f"]({SITE}/docs/admin/iam#roles)" in md
    assert f"]({SITE}/docs/user-guide/other#x)" in md
    assert f"]({SITE}/schemas/dag-spec.schema.json)" in md
    assert "Escaped {braces}." in md
    assert "[Screenshot: The deploy dialog (Deploy with a pull request.)]" in md
    assert "[Video: A tour]" in md
    assert "**Tip: Pull requests**" in md
    assert ":::" not in md
    assert "```python\nimport os  # kept verbatim, not an MDX import\n```" in md
    assert pages["intro"]["title"] == "What is AutoAir"
    assert pages["intro"]["section"] == ""
    config = pages["reference/configuration"]
    assert config["source"] == "docs/configuration-reference.md"
    assert config["title"] == "Configuration reference"
    assert config["description"].startswith("Every AutoAir setting")
    assert f"]({SITE}/docs/admin/configuration#files)" in config["markdown"]
    assert "stale copy" not in json.dumps(data)


def test_render_is_deterministic_with_one_page_per_line(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    out = tmp_path / "pages.json"
    docs_build.write(repo, out)
    first = out.read_text(encoding="utf-8")
    docs_build.write(repo, out)
    assert out.read_text(encoding="utf-8") == first
    assert json.loads(first)["pages"]
    assert len(first.splitlines()) == len(json.loads(first)["pages"]) + 2


def test_build_honours_site_url_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DOCS_URL", "https://docs.example.com")
    monkeypatch.setenv("DOCS_BASE_URL", "/")
    data = docs_build.build(_repo(tmp_path))
    assert data["docs_url"] == "https://docs.example.com/docs/"


def test_build_fails_loudly_without_sources(tmp_path: Path) -> None:
    with pytest.raises(docs_build.BuildError):
        docs_build.build(tmp_path)
    assert docs_build.main(["--root", str(tmp_path), "--out", str(tmp_path / "x.json")]) == 1


def test_front_matter_scalars_and_folded_blocks() -> None:
    meta, body = docs_build.front_matter(
        "---\ntitle: 'It''s here'\ndescription: >\n  one\n  two\nid: \"x\"\ndraft: true\n---\nB"
    )
    assert meta == {"title": "It's here", "description": "one two", "id": "x", "draft": True}
    assert body == "B"


def test_the_repository_docs_build_and_include_the_configuration_reference() -> None:
    data = docs_build.build(ROOT)
    pages = {p["path"]: p for p in data["pages"]}
    assert len(pages) > 50
    assert {"intro", "ai/mcp", "ai/docs-mcp", "reference/configuration"} <= set(pages)
    assert "AUTOAIR_FEATURES__AUTOAIR_DOCS" in pages["reference/configuration"]["markdown"]
    for page in pages.values():
        assert page["title"]
        assert page["url"].startswith(f"{SITE}/docs/")
    # Components become text (the contributor guide keeps them inside code blocks).
    assert "[Screenshot: " in pages["intro"]["markdown"]
    assert "<Screenshot " not in pages["intro"]["markdown"]


# --------------------------------------------------------------------------- index


@pytest.fixture(scope="module")
def index() -> DocsIndex:
    return DocsIndex(docs_build.build(ROOT))


def test_slug_follows_docusaurus() -> None:
    assert slug("Git & pull requests") == "git--pull-requests"
    assert slug("`autoair kb` commands") == "autoair-kb-commands"
    assert slug("Custom {#my-id}") == "my-id"
    assert slug("[Linked](/docs/x) heading") == "linked-heading"


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("install AutoAir as an Airflow plugin", "installation/plugin"),
        ("single sign-on keycloak oidc", "admin/sso"),
        ("knowledge base air-gapped bundle import", "ai/knowledge-base-air-gapped"),
        ("AutoAir documentation MCP server for coding agents", "ai/docs-mcp"),
    ],
)
def test_search_finds_the_page_about_it(index: DocsIndex, query: str, expected: str) -> None:
    result = index.search(query, limit=5)
    assert expected in [h.path for h in result.hits], [h.path for h in result.hits]


def test_search_hits_cite_page_urls_with_anchors_and_are_bounded(index: DocsIndex) -> None:
    result = index.search("deploy DAGs to git with a pull request", limit=20)
    assert 0 < len(result.hits) <= 20
    for hit in result.hits:
        assert hit.url.startswith(f"{SITE}/docs/{hit.path}")
        assert len(hit.text) <= 1_500
        assert len(hit.snippet) <= 330
    per_page: dict[str, int] = {}
    for hit in result.hits:
        per_page[hit.path] = per_page.get(hit.path, 0) + 1
    assert max(per_page.values()) <= 2
    anchored = [h for h in result.hits if "#" in h.url]
    assert anchored
    _page, fragment = index.resolve(anchored[0].url)
    content = index.read(anchored[0].url)
    assert content.content.lstrip().startswith("#"), fragment  # starts at the heading


def test_search_section_filter_limit_and_empty_query(index: DocsIndex) -> None:
    hits = index.search("configuration", limit=3, section="admin").hits
    assert hits
    assert len(hits) <= 3
    assert all(h.path.startswith("admin/") for h in hits)
    assert index.search("   ").hits == []
    assert len(index.search("airflow", limit=500).hits) <= 20
    assert index.search("zzzxqv nonexistentword").hits == []


@pytest.mark.parametrize(
    "ref",
    [
        "ai/mcp",
        "/docs/ai/mcp",
        "docs/ai/mcp/",
        "ai/mcp.md",
        f"{SITE}/docs/ai/mcp",
        f"{SITE}/docs/ai/mcp#tools",
        "https://elsewhere.example/autoair/docs/ai/mcp",
    ],
)
def test_resolve_accepts_paths_routes_and_urls(index: DocsIndex, ref: str) -> None:
    page, _ = index.resolve(ref)
    assert page.path == "ai/mcp"


@pytest.mark.parametrize(
    "ref",
    [
        "../../etc/passwd",
        "/etc/passwd",
        "file:///etc/passwd",
        "ai/../../pyproject.toml",
        "website/docs/ai/mcp.md",
        "src/autoair/docs/pages.json",
        "",
        "x" * 2_000,
    ],
)
def test_resolve_only_returns_packaged_pages(index: DocsIndex, ref: str) -> None:
    with pytest.raises(KeyError):
        index.resolve(ref)


def test_unknown_page_suggests_close_matches(index: DocsIndex) -> None:
    with pytest.raises(KeyError, match="did you mean ai/mcp"):
        index.resolve("ai/mpc")


def test_read_is_bounded_and_pages_with_next_offset(index: DocsIndex) -> None:
    whole = index.read("reference/configuration", max_chars=MAX_READ_CHARS)
    assert whole.total_chars > MAX_READ_CHARS  # the largest page needs several reads
    assert len(whole.content) <= MAX_READ_CHARS
    first = index.read("reference/configuration", max_chars=10)  # raised to the minimum
    assert MIN_READ_CHARS * 4 // 5 <= len(first.content) <= MIN_READ_CHARS
    assert first.content.startswith("# Configuration reference\n\nSource: ")
    assert first.next_offset == len(first.content)
    text, offset = first.content, first.next_offset
    while offset is not None:
        part = index.read("reference/configuration", offset=offset, max_chars=MAX_READ_CHARS)
        assert part.offset == offset
        text += part.content
        offset = part.next_offset
    assert len(text) == whole.total_chars
    beyond = index.read("ai/mcp", offset=10**9)
    assert beyond.content == ""
    assert beyond.next_offset is None


def test_listing_covers_every_page(index: DocsIndex) -> None:
    listing = index.listing()
    assert listing.total == len(index.pages)
    assert listing.llms_txt == f"{SITE}/llms.txt"
    assert "ai" in listing.sections
    assert "admin" in listing.sections
    ai = index.listing("ai")
    assert ai.total
    assert all(p.section == "ai" for p in ai.pages)
    assert index.listing("nope").total == 0


def test_packaged_file_is_used_outside_a_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import autoair.docs.index as module

    data_file = tmp_path / "pages.json"
    docs_build.write(_repo(tmp_path / "repo"), data_file)
    monkeypatch.setattr(module, "_CHECKOUT", tmp_path / "site-packages")
    monkeypatch.setattr(module, "DATA_PATH", data_file)
    assert checkout_root() is None
    data = load_data()
    assert {p["path"] for p in data["pages"]} >= {"user-guide/deploy"}
    monkeypatch.setattr(module, "DATA_PATH", tmp_path / "missing.json")
    with pytest.raises(DocsUnavailable, match="no packaged documentation"):
        load_data()
    (tmp_path / "bad.json").write_text('{"kind": "other"}', encoding="utf-8")
    with pytest.raises(DocsUnavailable):
        DocsIndex(load_data(tmp_path / "bad.json"))


def test_checkout_builds_from_sources() -> None:
    assert checkout_root() == ROOT
    assert "ai/docs-mcp" in docs_index().pages
    assert DATA_PATH.name == "pages.json"
