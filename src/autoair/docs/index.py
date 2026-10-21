"""AutoAir's own documentation inside the package: load, search and read it offline.

The data file ``autoair/docs/pages.json`` is generated from the website sources when the
wheel is built (:mod:`autoair.docs.build`, ``hatch_build.py``), so an installation always
carries the documentation of its own version and needs no network. In a source checkout
(``website/docs`` next to ``src/``) the pages are built from the sources instead, so they
are never stale while the docs are edited.

Search is BM25 over heading-aware chunks (the knowledge base's chunker and the AI layer's
pure-Python BM25 index): no store, no model, no extra. The index is derived from immutable
package data, so caching it per process is replica safe.
"""

from __future__ import annotations

import difflib
import json
import re
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, Field

from autoair.docs.build import DATA_FILE, FORMAT, KIND, BuildError, build

DATA_PATH = Path(__file__).with_name(DATA_FILE)
#: Repository root when this module runs from a source checkout (src/autoair/docs/).
_CHECKOUT = Path(__file__).resolve().parents[3]

#: Upper bounds of what one tool call returns (results go into a model's context window).
MAX_QUERY_CHARS = 500
MAX_SEARCH_RESULTS = 20
DEFAULT_SEARCH_RESULTS = 5
SNIPPET_CHARS = 320
HIT_TEXT_CHARS = 1_500
DEFAULT_READ_CHARS = 20_000
MIN_READ_CHARS = 1_000
MAX_READ_CHARS = 100_000
#: Longest page reference accepted by :meth:`DocsIndex.resolve`.
MAX_REF_CHARS = 1_000
#: Hits taken from one page before the next page gets a turn (diversity).
_MAX_PER_PAGE = 2

_ATX = re.compile(r"^(#{2,6})\s+(.*?)\s*#*\s*$")
_FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
_CUSTOM_ID = re.compile(r"\s*\{#([\w-]+)\}\s*$")
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_SLUG_DROP = re.compile(r"[^\w\- ]", re.UNICODE)


class DocsUnavailable(RuntimeError):
    """This installation has no documentation data (a broken or hand-made install)."""


class DocPage(BaseModel):
    path: str = Field(description="Route under /docs/, e.g. ai/mcp.")
    url: str = Field(description="Published URL of the page (cite it).")
    title: str
    description: str = ""
    section: str = Field(default="", description="Top-level section, e.g. ai or admin.")
    source: str = Field(default="", description="Source file in the AutoAir repository.")
    markdown: str = ""


class DocSummary(BaseModel):
    path: str
    title: str
    description: str = ""
    section: str = ""
    url: str


class DocHit(BaseModel):
    title: str = Field(description="Page title.")
    heading: str = Field(default="", description="Heading path of the passage.")
    path: str = Field(description="Page path for read_autoair_doc.")
    url: str = Field(description="URL of the passage (with the heading anchor): cite it.")
    snippet: str
    text: str = Field(default="", description="The passage (bounded).")
    score: float = 0.0


class DocsSearchResult(BaseModel):
    query: str
    version: str = Field(description="AutoAir version the documentation belongs to.")
    hits: list[DocHit] = Field(default_factory=list)


class DocContent(BaseModel):
    path: str
    url: str
    title: str
    description: str = ""
    section: str = ""
    version: str = Field(description="AutoAir version the documentation belongs to.")
    content: str = Field(description="Markdown of the page from offset (bounded).")
    offset: int = Field(description="Character offset of content in the whole page.")
    total_chars: int = Field(description="Length of the whole page.")
    next_offset: int | None = Field(
        default=None, description="Pass as offset to read on; null when the page is complete."
    )


class DocsListing(BaseModel):
    version: str
    docs_url: str = Field(description="Root of the published documentation.")
    llms_txt: str = Field(description="llms.txt index of the published site.")
    sections: list[str] = Field(default_factory=list)
    total: int = 0
    pages: list[DocSummary] = Field(default_factory=list)


def slug(text: str) -> str:
    """Fragment id Docusaurus gives a heading (github-slugger rules: lower case, drop
    punctuation, one hyphen per space; ``{#id}`` sets it explicitly)."""
    custom = _CUSTOM_ID.search(text)
    if custom:
        return custom.group(1)
    plain = _MD_LINK.sub(r"\1", text).strip().lower()
    return _SLUG_DROP.sub("", plain).replace(" ", "-")


def _heading_text(text: str) -> str:
    return _MD_LINK.sub(r"\1", _CUSTOM_ID.sub("", text)).replace("`", "").strip()


def sections_of(page: DocPage) -> list[Any]:
    """Heading-aware sections of a page (knowledge-base :class:`~autoair.kb.models.Section`
    objects with Docusaurus anchors); code blocks are kept."""
    from autoair.kb.models import Section

    out: list[Section] = []
    path: list[str] = [page.title]
    anchor: str | None = None
    level = 1
    buf: list[str] = []
    fence: str | None = None

    def flush() -> None:
        text = "\n".join(buf).strip()
        buf.clear()
        if text:
            out.append(Section(heading=" > ".join(path), anchor=anchor, level=level, text=text))

    for line in page.markdown.splitlines():
        opener = _FENCE.match(line)
        if fence is not None:
            if opener and opener.group(1)[0] == fence[0] and len(opener.group(1)) >= len(fence):
                fence = None
            buf.append(line)
            continue
        if opener:
            fence = opener.group(1)
            buf.append(line)
            continue
        m = _ATX.match(line)
        if m:
            flush()
            level = len(m.group(1))
            path = [*path[: level - 1], _heading_text(m.group(2))]
            anchor = slug(m.group(2)) or None
            continue
        buf.append(line)
    flush()
    return out


class DocsIndex:
    """Search and read access to one set of documentation pages."""

    def __init__(self, data: dict[str, Any]) -> None:
        if data.get("kind") != KIND or data.get("format") != FORMAT:
            raise DocsUnavailable("unsupported documentation data (kind/format)")
        from autoair import __version__

        self.version = __version__
        self.docs_url = str(data.get("docs_url") or "")
        self.llms_txt = str(data.get("llms_txt") or "")
        self.pages: dict[str, DocPage] = {}
        for raw in data.get("pages") or []:
            page = DocPage.model_validate(raw)
            self.pages[page.path] = page
        if not self.pages:
            raise DocsUnavailable("the documentation data has no pages")
        self._docs_path = urlsplit(self.docs_url).path or "/docs/"
        self._lock = threading.Lock()
        self._bm25: Any = None
        self._chunks: list[Any] = []

    # -- index ----------------------------------------------------------------------------

    def _index(self) -> tuple[Any, list[Any]]:
        with self._lock:
            if self._bm25 is None:
                from autoair.ai.rag import BM25Index, Document
                from autoair.kb.chunk import chunk_page
                from autoair.kb.config import KbChunkSettings
                from autoair.kb.models import PageDoc

                settings = KbChunkSettings()
                chunks: list[Any] = []
                for page in self.pages.values():
                    doc = PageDoc(
                        id=page.path,
                        source="autoair",
                        unit="autoair",
                        component="autoair",
                        package="autoair",
                        version=self.version,
                        url=page.url,
                        title=page.title,
                        sections=sections_of(page),
                    )
                    chunks.extend(chunk_page(doc, settings))
                docs = [
                    Document(
                        id=c.id,
                        kind="doc",
                        title=f"{c.title} {c.heading.split(' > ', 1)[-1]}",
                        text=c.text,
                        uri=c.url,
                        meta={"section": self.pages[c.page_id].section, "path": c.page_id},
                    )
                    for c in chunks
                ]
                self._chunks = chunks
                self._bm25 = BM25Index(docs)
            return self._bm25, self._chunks

    # -- search ---------------------------------------------------------------------------

    def search(
        self, query: str, limit: int = DEFAULT_SEARCH_RESULTS, section: str | None = None
    ) -> DocsSearchResult:
        from autoair.ai.query import expand_query
        from autoair.ai.rag import tokenize

        query = query.strip()[:MAX_QUERY_CHARS]
        limit = max(1, min(limit, MAX_SEARCH_RESULTS))
        result = DocsSearchResult(query=query, version=self.version)
        if not query:
            return result
        bm25, chunks = self._index()
        expanded = [t for t in tokenize(expand_query(query)) if t not in tokenize(query)]
        filters = {"section": section} if section else None
        ranked = bm25.search(query, k=max(limit * 8, 40), filters=filters, extra_terms=expanded)
        tokens = tokenize(query)
        per_page: dict[str, int] = {}
        for i, score in ranked:
            chunk = chunks[i]
            if per_page.get(chunk.page_id, 0) >= _MAX_PER_PAGE:
                continue
            per_page[chunk.page_id] = per_page.get(chunk.page_id, 0) + 1
            heading = chunk.heading.split(" > ", 1)[1] if " > " in chunk.heading else ""
            result.hits.append(
                DocHit(
                    title=chunk.title,
                    heading=heading,
                    path=chunk.page_id,
                    url=chunk.url,
                    snippet=_snippet(chunk.text, tokens, SNIPPET_CHARS),
                    text=chunk.text[:HIT_TEXT_CHARS],
                    score=round(float(score), 4),
                )
            )
            if len(result.hits) >= limit:
                break
        return result

    # -- read -----------------------------------------------------------------------------

    def resolve(self, ref: str) -> tuple[DocPage, str | None]:
        """The packaged page ``ref`` names, and its fragment. ``ref`` is a page path
        (``ai/mcp``), a site route (``/docs/ai/mcp``) or a URL of the published site; only
        packaged pages are ever returned (nothing is read from disk or the network)."""
        raw = ref.strip()
        if not raw or len(raw) > MAX_REF_CHARS:
            raise KeyError("give a page path such as ai/mcp, or a documentation URL")
        parts = urlsplit(raw)
        path = unquote(parts.path) if parts.scheme or raw.startswith("/") else raw.split("#")[0]
        fragment = parts.fragment or None
        if parts.scheme and parts.scheme not in ("http", "https"):
            raise KeyError(f"not an AutoAir documentation reference: {raw}")
        for prefix in (self._docs_path, "/docs/", "docs/"):
            if path.startswith(prefix):
                path = path[len(prefix) :]
                break
        path = re.sub(r"\.(mdx?|html)$", "", path.strip("/"))
        page = self.pages.get(path) or self.pages.get(path.removesuffix("/index"))
        if page is None:
            close = difflib.get_close_matches(path, list(self.pages), n=3, cutoff=0.5)
            hint = f"; did you mean {', '.join(close)}?" if close else ""
            raise KeyError(f"no AutoAir documentation page {path or raw!r}{hint}")
        return page, fragment

    def read(self, ref: str, offset: int = 0, max_chars: int = DEFAULT_READ_CHARS) -> DocContent:
        page, fragment = self.resolve(ref)
        max_chars = max(MIN_READ_CHARS, min(max_chars, MAX_READ_CHARS))
        header = f"# {page.title}\n\nSource: {page.url}\n\n"
        if page.description:
            header += f"{page.description}\n\n"
        text = header + page.markdown
        if fragment and offset == 0:
            offset = _heading_offset(text, fragment)
        offset = max(0, min(offset, len(text)))
        end = min(len(text), offset + max_chars)
        if end < len(text):
            # End on a line break when one is near, so the next read starts on a line.
            cut = text.rfind("\n", offset + max_chars * 4 // 5, end)
            end = cut + 1 if cut > offset else end
        return DocContent(
            path=page.path,
            url=page.url,
            title=page.title,
            description=page.description,
            section=page.section,
            version=self.version,
            content=text[offset:end],
            offset=offset,
            total_chars=len(text),
            next_offset=end if end < len(text) else None,
        )

    # -- list -----------------------------------------------------------------------------

    def listing(self, section: str | None = None) -> DocsListing:
        pages = [
            DocSummary(
                path=p.path, title=p.title, description=p.description, section=p.section, url=p.url
            )
            for p in self.pages.values()
            if section is None or p.section == section
        ]
        return DocsListing(
            version=self.version,
            docs_url=self.docs_url,
            llms_txt=self.llms_txt,
            sections=self.sections(),
            total=len(pages),
            pages=pages,
        )

    def sections(self) -> list[str]:
        return sorted({p.section for p in self.pages.values() if p.section})


def _heading_offset(text: str, fragment: str) -> int:
    fence: str | None = None
    pos = 0
    for line in text.splitlines(keepends=True):
        opener = _FENCE.match(line)
        if fence is not None:
            if opener and opener.group(1)[0] == fence[0]:
                fence = None
        elif opener:
            fence = opener.group(1)
        else:
            m = _ATX.match(line.rstrip("\n"))
            if m and slug(m.group(2)) == fragment:
                return pos
        pos += len(line)
    return 0


def _snippet(text: str, tokens: Sequence[str], size: int) -> str:
    lower = text.lower()
    start = 0
    for token in tokens:
        pos = lower.find(token)
        if pos >= 0:
            start = max(0, pos - size // 4)
            break
    chunk = " ".join(text[start : start + size].split())
    return ("…" if start else "") + chunk + ("…" if start + size < len(text) else "")


def checkout_root() -> Path | None:
    """The repository root when running from a source checkout, else ``None``."""
    root = _CHECKOUT
    if (root / "website" / "docs").is_dir() and (root / "pyproject.toml").is_file():
        return root
    return None


def load_data(path: Path | None = None) -> dict[str, Any]:
    """The documentation data: ``path``, the sources of a checkout, or the packaged file."""
    if path is None:
        root = checkout_root()
        if root is not None:
            try:
                return build(root)
            except (BuildError, OSError) as exc:
                if not DATA_PATH.is_file():
                    raise DocsUnavailable(f"cannot build the documentation: {exc}") from exc
        path = DATA_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DocsUnavailable(
            f"this AutoAir installation has no packaged documentation ({path.name} is missing)"
        ) from exc
    except (OSError, ValueError) as exc:
        raise DocsUnavailable(f"unreadable documentation data: {exc}") from exc
    if not isinstance(data, dict):
        raise DocsUnavailable("unreadable documentation data")
    return data


_cache: dict[str, DocsIndex] = {}
_cache_lock = threading.Lock()


def docs_index() -> DocsIndex:
    """The documentation of this installation (built once per process)."""
    with _cache_lock:
        index = _cache.get("index")
        if index is None:
            index = _cache["index"] = DocsIndex(load_data())
        return index


def reset_cache() -> None:
    with _cache_lock:
        _cache.clear()


__all__ = [
    "DATA_PATH",
    "DocContent",
    "DocHit",
    "DocPage",
    "DocSummary",
    "DocsIndex",
    "DocsListing",
    "DocsSearchResult",
    "DocsUnavailable",
    "checkout_root",
    "docs_index",
    "load_data",
    "reset_cache",
    "sections_of",
    "slug",
]
