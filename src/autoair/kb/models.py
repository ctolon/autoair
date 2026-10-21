"""Data models of the knowledge base: parsed pages, chunks, hits, status and sources."""

from __future__ import annotations

import hashlib
from typing import Any, Literal

from pydantic import BaseModel, Field


def digest(*parts: str, size: int = 32) -> str:
    h = hashlib.sha256()
    for part in parts:
        h.update(part.encode("utf-8", "surrogatepass"))
        h.update(b"\0")
    return h.hexdigest()[:size]


class Section(BaseModel):
    """A heading and the text below it (up to the next heading)."""

    heading: str = Field(default="", description="Heading path, e.g. 'S3 > Create a bucket'.")
    anchor: str | None = Field(default=None, description="Fragment id of the heading.")
    level: int = 1
    text: str = ""


class PageMeta(BaseModel):
    """Where a page comes from (everything but its content)."""

    id: str
    source: str
    unit: str = Field(description="Package version the page belongs to (unit of refresh).")
    component: str
    package: str
    version: str
    url: str = Field(description="Canonical URL users open (citations).")
    title: str
    updated: str | None = None
    content_hash: str = ""
    etag: str | None = None
    last_modified: str | None = None
    fetched_at: str | None = None


class PageDoc(PageMeta):
    """A parsed page."""

    sections: list[Section] = Field(default_factory=list)


class PageRecord(PageMeta):
    """A stored page (without its sections)."""

    chunks: int = 0


class Chunk(BaseModel):
    id: str
    page_id: str
    n: int
    source: str
    component: str
    package: str
    version: str
    url: str = Field(description="Page URL with the section anchor.")
    title: str = Field(description="Page title.")
    heading: str = ""
    text: str
    hash: str = Field(description="sha256 of the text (embedding cache key).")
    updated: str | None = None


class KbHit(BaseModel):
    """A search result with everything a citation needs."""

    id: str
    title: str
    heading: str = ""
    url: str
    snippet: str
    text: str = ""
    source: str
    component: str
    package: str
    version: str
    updated: str | None = None
    score: float = 0.0


class KbSearchResult(BaseModel):
    query: str
    mode: Literal["hybrid", "lexical", "empty"] = Field(
        description="hybrid: BM25 + embeddings; lexical: BM25 only; empty: nothing indexed."
    )
    reranked: bool = False
    hits: list[KbHit] = Field(default_factory=list)


class SourceRun(BaseModel):
    """What the last refresh did with one source."""

    source: str
    state: Literal["ok", "partial", "failed", "skipped"] = "ok"
    detail: str | None = None
    units: int = 0
    pages_seen: int = 0
    pages_changed: int = 0
    pages_unchanged: int = 0
    pages_removed: int = 0
    pages_failed: int = 0
    chunks_written: int = 0


class KbError(BaseModel):
    level: Literal["error", "warning", "info"] = "error"
    source: str | None = None
    url: str | None = None
    message: str
    at: str | None = None


class KbProgress(BaseModel):
    source: str | None = None
    unit: str | None = None
    phase: str = "idle"
    done: int = 0
    total: int | None = None


class KbRunStatus(BaseModel):
    """The persisted status of the knowledge base of one store (shared by replicas)."""

    state: Literal["idle", "running", "failed"] = "idle"
    run_id: str | None = None
    mode: Literal["refresh", "rebuild", "import"] | None = None
    owner: str | None = Field(default=None, description="Replica running the refresh.")
    started_at: str | None = None
    finished_at: str | None = None
    last_refresh: str | None = Field(default=None, description="End of the last refresh.")
    last_success: str | None = Field(default=None, description="End of the last full refresh.")
    outcome: Literal["ok", "partial", "failed", "cancelled"] | None = None
    cancel_requested: bool = False
    progress: KbProgress = Field(default_factory=KbProgress)
    sources: list[SourceRun] = Field(default_factory=list)
    errors: list[KbError] = Field(default_factory=list)
    embedding_model: str | None = None


class StoreStats(BaseModel):
    kind: str
    location: str = Field(description="Where the index lives (no credentials).")
    ok: bool = True
    error: str | None = None
    pages: int = 0
    chunks: int = 0
    vectors: int = 0
    embedding_model: str | None = None
    by_component: dict[str, int] = Field(default_factory=dict, description="Pages per component.")
    by_source: dict[str, int] = Field(default_factory=dict, description="Pages per source.")
    versions: dict[str, list[str]] = Field(
        default_factory=dict, description="Versions indexed per component."
    )


class ComponentInfo(BaseModel):
    id: str
    title: str
    family: Literal["apache", "astronomer", "autoair"]
    default: bool
    description: str = ""


class SourceInfo(BaseModel):
    """A configured source with the admin overrides applied."""

    id: str
    kind: str
    title: str
    enabled: bool
    usable: bool = Field(description="Can be read now (e.g. official needs kb.online).")
    reason: str | None = Field(default=None, description="Why it is not usable.")
    reason_code: str | None = Field(
        default=None,
        description="Stable code of reason (disabled, air_gapped) for the UI's translation.",
    )
    url: str | None = None
    conn_id: str | None = None
    ref: str | None = None
    layout: str | None = None
    components: list[str] = Field(default_factory=list)
    versions: list[str] = Field(default_factory=list)
    overridden: bool = Field(default=False, description="Changed in the admin panel.")
    pages: int = 0


class KbStatus(BaseModel):
    enabled: bool
    online: bool
    store: StoreStats
    run: KbRunStatus
    mode: Literal["hybrid", "lexical"] = "lexical"
    embedder: str | None = Field(default=None, description="Embedding model, if any.")
    reranker: str | None = None
    refresh_interval_hours: float = 0
    next_refresh: str | None = None
    sources: list[SourceInfo] = Field(default_factory=list)
    components: list[ComponentInfo] = Field(default_factory=list)
    providers: str = "installed"


class SourceOverride(BaseModel):
    """Admin-panel changes of one source (``None`` keeps the configured value)."""

    enabled: bool | None = None
    components: list[str] | None = Field(default=None, max_length=20)
    versions: list[str] | None = Field(default=None, max_length=20)


class ImportReport(BaseModel):
    pages: int = 0
    changed: int = 0
    unchanged: int = 0
    removed: int = 0
    chunks: int = 0
    signed: bool = False
    signature_verified: bool = False
    sources: list[str] = Field(default_factory=list)


def page_id(source: str, url: str) -> str:
    return digest(source, url)


def as_meta(data: dict[str, Any]) -> PageMeta:
    return PageMeta.model_validate({k: v for k, v in data.items() if k in PageMeta.model_fields})


__all__ = [
    "Chunk",
    "ComponentInfo",
    "ImportReport",
    "KbError",
    "KbHit",
    "KbProgress",
    "KbRunStatus",
    "KbSearchResult",
    "KbStatus",
    "PageDoc",
    "PageMeta",
    "PageRecord",
    "Section",
    "SourceInfo",
    "SourceOverride",
    "SourceRun",
    "StoreStats",
    "as_meta",
    "digest",
    "page_id",
]
