"""Settings of the documentation knowledge base: the ``kb`` section (``AUTOAIR_KB__*``).

The knowledge base indexes the Apache Airflow and Astronomer documentation for retrieval
(agents, MCP, the admin panel). It is its own section rather than part of ``ai`` because it
works without the ``autoair[ai]`` extra (keyword search needs no model) and has its own
feature switch (``features.kb``). Embeddings and the reranker are the AI layer's
(``ai.embedding``, ``ai.retrieval.reranker``) and are used when they are configured.

This module stays import-light (pydantic + stdlib): ``autoair.settings`` imports it.
"""

from __future__ import annotations

from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ComponentId = Literal[
    "core",
    "task_sdk",
    "providers",
    "helm_chart",
    "airflowctl",
    "astronomer_airflow",
    "astro_cli",
    "astro",
    "astro_runtime",
    "autoair",
]
SourceKind = Literal["official", "site", "llms_txt", "git", "path", "bundle", "autoair"]
SourceLayout = Literal["auto", "airflow_repo", "sphinx_site", "files"]
StoreKind = Literal["local", "pgvector", "elasticsearch"]

#: Components indexed by default: everything about running Airflow itself, plus
#: Astronomer's vendor-neutral Airflow guides and AutoAir's own documentation (packaged,
#: no network). Astro product docs are opt-in.
DEFAULT_COMPONENTS: list[ComponentId] = [
    "core",
    "task_sdk",
    "providers",
    "helm_chart",
    "airflowctl",
    "astronomer_airflow",
    "autoair",
]
DEFAULT_DOMAINS = ["airflow.apache.org", "astronomer.io"]
SOURCE_ID = r"^[a-z0-9][a-z0-9_-]{0,40}$"


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_default=True)


def _no_credentials(value: str | None) -> str | None:
    """URLs of stores and sources never carry credentials (Airflow connections do)."""
    if value and "://" in value:
        parts = urlsplit(value)
        if parts.username or parts.password:
            raise ValueError(
                "do not put credentials in the URL: use an Airflow connection (conn_id)"
            )
    return value


class KbSourceConfig(_Section):
    """One place the knowledge base reads documentation from."""

    id: str = Field(
        pattern=SOURCE_ID,
        description="Stable source id (lower-case letters, digits, '-', '_'); pages are "
        "kept per source, so renaming a source re-indexes it.",
    )
    kind: SourceKind = Field(
        description="official: airflow.apache.org and astronomer.io (needs kb.online); site: "
        "a mirror of the Airflow docs site (Sphinx layout) over HTTP; llms_txt: an llms.txt "
        "index of Markdown pages (Astronomer layout); git: a repository (e.g. a corporate "
        "mirror of apache/airflow); path: a local folder or an object-storage URL (s3://, "
        "gs://, abfs://); bundle: a KB bundle file made with `autoair kb build`; autoair: "
        "AutoAir's own documentation packaged with this version (no url, no network).",
    )
    enabled: bool = Field(
        default=True, description="Index this source (the admin panel can switch it at runtime)."
    )
    title: str | None = Field(default=None, description="Display name (default: the id).")
    url: str | None = Field(
        default=None,
        description="site: docs base URL (…/docs/); llms_txt: the llms.txt URL; git: remote "
        "URL (default: the connection's host); path: folder path or fsspec URL; bundle: file "
        "path or fsspec URL of the bundle. Never with credentials.",
    )
    conn_id: str | None = Field(
        default=None,
        description="Airflow connection with the credentials: git (password/token or SSH "
        "key, like storage.git.conn_id) or object storage (path/bundle, read through "
        "Airflow's ObjectStoragePath). AutoAir never stores the credentials.",
    )
    ref: str | None = Field(
        default=None, description="git: branch or tag to index (default: the remote HEAD)."
    )
    layout: SourceLayout = Field(
        default="auto",
        description="git/path: airflow_repo (the apache/airflow repository: rst sources "
        "per component), sphinx_site (a downloaded copy of airflow.apache.org/docs), files "
        "(any Markdown/rst/HTML/text files); auto detects it.",
    )
    format: Literal["auto", "html", "sources"] = Field(
        default="auto",
        description="site/official Sphinx pages: html (rendered pages, include the example "
        "code; default) or sources (the _sources/*.rst.txt files: smaller, without the "
        "included examples).",
    )
    components: list[ComponentId] | None = Field(
        default=None, description="Components taken from this source (default: kb.components)."
    )
    versions: list[str] | None = Field(
        default=None,
        description="Versions to index (default: kb.versions): installed, stable or an "
        "explicit version such as 3.1.0.",
    )
    component: str = Field(
        default="custom",
        pattern=r"^[a-z0-9][a-z0-9_-]{0,40}$",
        description="files layout: the component label of its pages.",
    )
    public_base_url: str | None = Field(
        default=None,
        description="Base URL for citations of files/repository pages (airflow_repo default: "
        "https://airflow.apache.org/docs/).",
    )
    include: list[str] = Field(
        default_factory=list,
        description="Only pages whose path matches one of these fnmatch patterns (JSON list).",
    )
    exclude: list[str] = Field(
        default_factory=list, description="Skip pages whose path matches (fnmatch, JSON list)."
    )
    allow_private: bool = Field(
        default=False,
        description="site/llms_txt: the host may resolve to a private address (an internal "
        "mirror). Only this source's own host is allowed then.",
    )
    storage_options: dict[str, Any] = Field(
        default_factory=dict,
        description="path/bundle without conn_id: fsspec options (non-secret ones such as "
        "region or endpoint_url; credentials belong in an Airflow connection).",
    )

    _check_url = field_validator("url", "public_base_url")(_no_credentials)

    @model_validator(mode="after")
    def _needs_url(self) -> KbSourceConfig:
        if self.kind in ("site", "llms_txt", "path", "bundle") and not self.url:
            raise ValueError(f"kb source {self.id!r} ({self.kind}) needs a url")
        if self.kind == "git" and not (self.url or self.conn_id):
            raise ValueError(f"kb source {self.id!r} (git) needs a url or a conn_id")
        return self


def _default_sources() -> list[KbSourceConfig]:
    return [
        KbSourceConfig(id="official", kind="official"),
        KbSourceConfig(id="autoair", kind="autoair", title="AutoAir documentation"),
    ]


class KbFetchSettings(_Section):
    """Limits and politeness of fetching documentation."""

    allowed_domains: list[str] = Field(
        default_factory=lambda: list(DEFAULT_DOMAINS),
        description="Hosts online sources may fetch from (sub-domains included); a site or "
        "llms_txt source's own host is added for that source. Every hop is checked against "
        "this list and must resolve to a public address (SSRF protection).",
    )
    timeout_seconds: float = Field(default=30, gt=0, description="Timeout of one request (s).")
    max_page_bytes: int = Field(
        default=8 * 1024 * 1024,
        ge=64 * 1024,
        description="Largest response or file read (bytes); larger pages are skipped and reported.",
    )
    max_pages: int = Field(
        default=25_000, ge=1, description="Pages one refresh may fetch or read in total."
    )
    max_pages_per_package: int = Field(
        default=3_000, ge=1, description="Pages per package version (one docs site package)."
    )
    max_run_seconds: float = Field(
        default=4 * 3600,
        gt=0,
        description="Wall-clock limit of one refresh (s); a refresh that runs out keeps what "
        "it indexed, removes nothing and reports a partial run.",
    )
    concurrency: int = Field(default=4, ge=1, le=32, description="Parallel fetches.")
    requests_per_second: float = Field(
        default=4.0, gt=0, le=100, description="Requests per second over all hosts."
    )
    max_redirects: int = Field(default=3, ge=0, le=10, description="Redirects followed.")
    retries: int = Field(
        default=2, ge=0, le=10, description="Retries of a request that failed with 429/5xx."
    )
    user_agent: str = Field(
        default="AutoAir-KB/1.0 (+https://github.com/ctolon/autoair)",
        description="User-Agent of documentation fetches.",
    )
    include_api_reference: bool = Field(
        default=False,
        description="Also index the generated API reference pages (_api/…) of Sphinx docs. "
        "Off by default: research already indexes the docstrings of the installed packages, "
        "and the API reference multiplies the size of the index.",
    )
    exclude_pages: list[str] = Field(
        default_factory=lambda: ["genindex", "search", "py-modindex", "_modules/*"],
        description="Sphinx page names never indexed (fnmatch, JSON list).",
    )
    git_timeout_seconds: float = Field(
        default=900, gt=0, description="Timeout of one git command (clone/fetch) (s)."
    )


class KbChunkSettings(_Section):
    """Heading-aware chunking of pages."""

    size: int = Field(default=1_200, ge=200, le=20_000, description="Target characters per chunk.")
    overlap: int = Field(default=150, ge=0, description="Characters shared by adjacent chunks.")
    min_chars: int = Field(
        default=200,
        ge=0,
        description="Sections shorter than this are merged with the next section of the "
        "same page (0: never merge).",
    )
    max_chunks_per_page: int = Field(
        default=400, ge=1, description="Chunks kept per page (the rest is dropped)."
    )

    @model_validator(mode="after")
    def _check_overlap(self) -> KbChunkSettings:
        if self.overlap >= self.size // 2:
            raise ValueError(
                f"kb.chunk.overlap ({self.overlap}) must be smaller than half of "
                f"kb.chunk.size ({self.size})"
            )
        return self


class KbSearchSettings(_Section):
    """Hybrid retrieval over the knowledge base."""

    top_k: int = Field(default=6, ge=1, le=50, description="Default number of results.")
    max_results: int = Field(
        default=20, ge=1, le=100, description="Upper bound of results any caller can ask for."
    )
    candidate_pool: int = Field(
        default=50, ge=5, le=1_000, description="Candidates per ranker before fusion/rerank."
    )
    rrf_k: int = Field(default=60, ge=1, description="Reciprocal Rank Fusion constant.")
    max_per_page: int = Field(
        default=2, ge=1, le=20, description="Results from the same page (diversity)."
    )
    embeddings: bool = Field(
        default=True,
        description="Use dense embeddings (ai.embedding, needs autoair[ai]) next to BM25. "
        "Without an embedder the knowledge base is keyword (BM25) only.",
    )
    rerank: bool = Field(
        default=True, description="Rerank with ai.retrieval.reranker when one is configured."
    )
    rerank_top_n: int = Field(default=20, ge=1, le=200, description="Candidates reranked.")
    snippet_chars: int = Field(default=320, ge=80, le=4_000, description="Snippet length.")
    max_text_chars: int = Field(
        default=1_500, ge=200, le=20_000, description="Chunk text returned per result."
    )
    context_max_chars: int = Field(
        default=3_000,
        ge=500,
        description="Documentation passages injected into one prompt (review, fix, writers).",
    )


class KbLocalStoreSettings(_Section):
    path: str | None = Field(
        default=None,
        description="SQLite file of the local index (default <cache_dir or state>/kb/kb.sqlite3).",
    )
    shared: bool = Field(
        default=False,
        description="The file is on storage every replica uses: one replica indexes for all. "
        "False: each host keeps (and refreshes) its own copy.",
    )


class KbPgvectorSettings(_Section):
    url: str | None = Field(
        default=None,
        description="SQLAlchemy URL of the PostgreSQL database with the vector extension "
        "(postgresql+psycopg://host:5432/db), without credentials. Default: AutoAir's own "
        "database when database.backend is postgresql.",
    )
    conn_id: str | None = Field(
        default=None,
        description="Airflow Postgres connection with host, login and password (overrides url).",
    )
    table_prefix: str = Field(
        default="autoair_kb_",
        pattern=r"^[a-z][a-z0-9_]{0,30}$",
        description="Prefix of the knowledge-base tables.",
    )
    schema_name: str | None = Field(
        default=None,
        pattern=r"^[a-z_][a-z0-9_]{0,62}$",
        description="Schema of the tables (default: the search path).",
    )
    hnsw_index: bool = Field(
        default=True, description="Create an HNSW index for the embeddings (pgvector >= 0.5)."
    )
    create_extension: bool = Field(
        default=True,
        description="Run CREATE EXTENSION IF NOT EXISTS vector (needs the privilege; create "
        "it once as a superuser otherwise and set false).",
    )
    pool_size: int = Field(default=4, ge=1, le=64, description="Connections kept open.")
    ssl_mode: (
        Literal["disable", "allow", "prefer", "require", "verify-ca", "verify-full"] | None
    ) = Field(default=None, description="PostgreSQL sslmode (driver default when unset).")
    ssl_root_cert: str | None = Field(
        default=None, description="CA bundle that verifies the server (sslrootcert)."
    )

    _check_url = field_validator("url")(_no_credentials)


class KbElasticsearchSettings(_Section):
    url: str | None = Field(
        default=None,
        description="Elasticsearch base URL (https://es.example:9200), without credentials. "
        "Requests go through AutoAir's HTTP stack (outbound.clients.kb for TLS/proxy).",
    )
    conn_id: str | None = Field(
        default=None,
        description="Airflow connection with login/password or extra.api_key (and host when "
        "url is unset).",
    )
    index_prefix: str = Field(
        default="autoair-kb",
        pattern=r"^[a-z0-9][a-z0-9_-]{0,60}$",
        description="Prefix of the knowledge-base indices.",
    )
    timeout_seconds: float = Field(default=30, gt=0, description="Timeout of one request (s).")
    num_candidates: int = Field(
        default=200, ge=10, le=10_000, description="kNN num_candidates per shard."
    )
    bulk_size: int = Field(default=500, ge=1, le=10_000, description="Documents per bulk request.")
    shards: int = Field(default=1, ge=1, le=64, description="Primary shards of new indices.")
    replicas: int = Field(default=1, ge=0, le=16, description="Replicas of new indices.")

    _check_url = field_validator("url")(_no_credentials)


class KbBundleSettings(_Section):
    public_keys: list[str] = Field(
        default_factory=list,
        description="Base64 Ed25519 public keys a bundle may be signed with (JSON list). When "
        "set, every imported bundle must carry a valid signature from one of them.",
    )
    require_signature: bool = Field(
        default=False,
        description="Refuse unsigned bundles even when no public key is configured (then "
        "every bundle is refused). Checksums are always verified.",
    )
    max_bytes: int = Field(
        default=4 * 1024**3,
        ge=1024,
        description="Largest bundle accepted (bytes); uploads from the admin panel are also "
        "bound by server.max_body_bytes.",
    )
    max_pages: int = Field(default=200_000, ge=1, description="Largest number of pages imported.")


class KbSettings(_Section):
    """The documentation knowledge base (RAG over Airflow and Astronomer docs)."""

    online: bool = Field(
        default=False,
        description="Let the official source fetch from airflow.apache.org and astronomer.io. "
        "Off (air-gapped): only the git, path, mirror and bundle sources you configure.",
    )
    components: list[ComponentId] = Field(
        default_factory=lambda: list(DEFAULT_COMPONENTS),
        description="Components to index: core, task_sdk, providers, helm_chart, airflowctl "
        "(Apache), astronomer_airflow (Astronomer's Airflow guides), astro_cli, astro, "
        "astro_runtime (Astronomer products; off by default), autoair (AutoAir's own "
        "documentation, read by the autoair source). JSON list.",
    )
    providers: Literal["installed", "all"] = Field(
        default="installed",
        description="Provider packages to index: installed (the providers installed here, "
        "at their installed versions: matches the code agents may write and keeps the index "
        "small) or all (every provider the docs list, latest stable docs for the ones not "
        "installed).",
    )
    provider_include: list[str] = Field(
        default_factory=lambda: ["*"],
        description="Provider name patterns to index (fnmatch on e.g. amazon, common-sql).",
    )
    provider_exclude: list[str] = Field(
        default_factory=list, description="Provider name patterns never indexed."
    )
    versions: list[str] = Field(
        default_factory=lambda: ["installed"],
        description="Versions per component: installed (the installed package version; "
        "stable when not installed or not published), stable, or explicit versions (several "
        "are indexed side by side and can be filtered). JSON list.",
    )
    sources: list[KbSourceConfig] = Field(
        default_factory=_default_sources,
        description='Documentation sources (JSON list), e.g. [{"id": "official", "kind": '
        '"official"}, {"id": "autoair", "kind": "autoair"}, {"id": "mirror", "kind": "git", '
        '"url": "https://git.corp/airflow.git", "conn_id": "kb_git", "ref": "3.1.0", '
        '"layout": "airflow_repo"}]. Default: official and autoair (AutoAir\'s packaged '
        "documentation); a list set here replaces the default, so keep the autoair entry "
        "to keep AutoAir's documentation searchable.",
    )
    store: StoreKind = Field(
        default="local",
        description="Where the index lives: local (SQLite with FTS5 BM25 and vectors, no "
        "dependencies), pgvector (PostgreSQL, autoair[kb-pgvector]) or elasticsearch "
        "(autoair[kb-elasticsearch]).",
    )
    refresh_interval_hours: float = Field(
        default=24,
        ge=0,
        description="Incremental refresh interval of the server (hours); 0 disables "
        "scheduled refreshes (manual: admin panel or `autoair kb refresh`).",
    )
    refresh_on_start: bool = Field(
        default=True,
        description="Build the index soon after start-up when it is empty and a source is usable.",
    )
    lock_ttl_seconds: float = Field(
        default=600,
        ge=30,
        description="Lease of the ingestion lock (renewed while a refresh runs); a replica "
        "that dies frees it after this long.",
    )
    max_errors: int = Field(
        default=50, ge=1, le=1_000, description="Errors kept in the status of a refresh."
    )
    cache_dir: str | None = Field(
        default=None,
        description="Folder of git checkouts and the embedding cache (default <cache_dir or "
        "state>/kb).",
    )
    state_url: str | None = Field(
        default=None,
        description="Where status, lock and admin overrides live when database.backend is "
        "file (fsspec URL; default <state>/kb-state).",
    )
    fetch: KbFetchSettings = Field(default_factory=KbFetchSettings, description="Fetch limits.")
    chunk: KbChunkSettings = Field(default_factory=KbChunkSettings, description="Chunking.")
    search: KbSearchSettings = Field(default_factory=KbSearchSettings, description="Retrieval.")
    local: KbLocalStoreSettings = Field(
        default_factory=KbLocalStoreSettings, description="Local (SQLite) store."
    )
    pgvector: KbPgvectorSettings = Field(
        default_factory=KbPgvectorSettings, description="pgvector store."
    )
    elasticsearch: KbElasticsearchSettings = Field(
        default_factory=KbElasticsearchSettings, description="Elasticsearch store."
    )
    bundle: KbBundleSettings = Field(
        default_factory=KbBundleSettings, description="KB bundles (export/import)."
    )

    @field_validator("sources")
    @classmethod
    def _unique_ids(cls, value: list[KbSourceConfig]) -> list[KbSourceConfig]:
        seen: set[str] = set()
        for source in value:
            if source.id in seen:
                raise ValueError(f"duplicate kb source id {source.id!r}")
            seen.add(source.id)
        return value


def kb_settings_of(settings: object | None) -> KbSettings:
    section = getattr(settings, "kb", None)
    return section if isinstance(section, KbSettings) else KbSettings()


__all__ = [
    "DEFAULT_COMPONENTS",
    "ComponentId",
    "KbBundleSettings",
    "KbChunkSettings",
    "KbElasticsearchSettings",
    "KbFetchSettings",
    "KbLocalStoreSettings",
    "KbPgvectorSettings",
    "KbSearchSettings",
    "KbSettings",
    "KbSourceConfig",
    "SourceKind",
    "SourceLayout",
    "StoreKind",
    "kb_settings_of",
]
