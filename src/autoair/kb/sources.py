"""Source planning: which pages each configured source contributes.

A source yields **units** (one docs package at one version, e.g. ``apache-airflow`` 3.1.0)
with a fingerprint (the Sphinx search index, the ``llms.txt`` index, the git commit…): when
a unit's fingerprint is unchanged since the last refresh it is skipped without reading a
single page. Each unit lists **page references** that know how to load their content
(conditionally, with the stored ETag / Last-Modified).

Kinds and layouts:

* ``official`` - airflow.apache.org (Sphinx sites) and astronomer.io (``llms.txt``);
  needs ``kb.online``;
* ``site`` - an HTTP mirror of airflow.apache.org/docs; ``llms_txt`` - an ``llms.txt``;
* ``git`` / ``path`` - a checkout or folder (local or object storage) in the
  ``airflow_repo`` layout (apache/airflow sources), ``sphinx_site`` layout (a downloaded
  copy of the docs site) or ``files`` layout (any Markdown/rst/HTML/text);
* ``bundle`` - a KB bundle (pages already parsed).
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import posixpath
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

from autoair.kb.components import (
    COMPONENTS,
    OFFICIAL_DOCS,
    PROVIDER_PREFIX,
    PROVIDERS_INDEX,
    installed_providers,
    installed_version,
    provider_repo_path,
    provider_wanted,
    resolve_versions,
)
from autoair.kb.config import KbSettings, KbSourceConfig
from autoair.kb.fetch import (
    FetchError,
    HttpFetcher,
    host_of,
    matches,
    open_fs,
    read_file,
    walk_files,
)
from autoair.kb.models import PageDoc
from autoair.kb.parse import IncludeResolver, format_of

log = logging.getLogger("autoair.kb")

_SETINDEX = re.compile(r"^\s*Search\.setIndex\((.*)\)\s*;?\s*$", re.DOTALL)
_MD_LINK = re.compile(r"\[([^\]]+)\]\((\S+?)\)")
_PROVIDER_LINK = re.compile(r"apache-airflow-providers-[a-z0-9][a-z0-9-]*[a-z0-9]")
_VERSION_LINE = re.compile(r'^\s*version\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)
_CHART_VERSION = re.compile(r"^version:\s*['\"]?([\w.+-]+)", re.MULTILINE)
_DOC_SUFFIXES = (".rst", ".md")
_FILE_SUFFIXES = (".md", ".mdx", ".markdown", ".rst", ".txt", ".html", ".htm")


@dataclass
class Loaded:
    not_modified: bool = False
    content: str = ""
    fmt: str = "text"
    etag: str | None = None
    last_modified: str | None = None
    resolve: IncludeResolver | None = None
    doc: PageDoc | None = None


@dataclass
class PageRef:
    url: str
    title: str
    load: Callable[[str | None, str | None], Loaded]


@dataclass
class Unit:
    key: str
    source: str
    component: str
    package: str
    version: str
    fingerprint: str | None
    pages: list[PageRef] = field(default_factory=list)
    note: str | None = None


class SourceError(Exception):
    """The source cannot be listed (its pages are kept, nothing is removed)."""


@dataclass
class PlanContext:
    settings: KbSettings
    fetcher: HttpFetcher
    cache_dir: Path
    warn: Callable[[str, str | None], None]


def _sha(data: bytes | str) -> str:
    raw = data.encode() if isinstance(data, str) else data
    return hashlib.sha256(raw).hexdigest()


# --------------------------------------------------------------------------- readers


class _Reader:
    """Reads files of a docs site: over HTTP or from a filesystem."""

    base: str

    def get(self, rel: str, etag: str | None = None, modified: str | None = None) -> Loaded:
        raise NotImplementedError

    def exists(self, rel: str) -> bool:
        try:
            self.get(rel)
        except FileNotFoundError:
            return False
        return True


class _HttpReader(_Reader):
    def __init__(self, base: str, ctx: PlanContext, *, own_host: bool, private: bool) -> None:
        self.base = base if base.endswith("/") else base + "/"
        self.fetcher = ctx.fetcher
        self.domains = [host_of(self.base)] if own_host else []
        self.private = private

    def get(self, rel: str, etag: str | None = None, modified: str | None = None) -> Loaded:
        url = urljoin(self.base, rel)
        resp = self.fetcher.get(
            url,
            etag=etag,
            last_modified=modified,
            domains=self.domains,
            allow_private=self.private,
        )
        if resp.not_modified:
            return Loaded(not_modified=True, etag=resp.etag or etag, last_modified=modified)
        if resp.status == 404:
            raise FileNotFoundError(url)
        if resp.status >= 400:
            raise FetchError(f"HTTP {resp.status} from {url}")
        return Loaded(
            content=resp.text(),
            fmt=format_of(rel, resp.content_type),
            etag=resp.etag,
            last_modified=resp.last_modified,
        )


class _FsReader(_Reader):
    def __init__(self, fs: Any, root: str, max_bytes: int, base: str) -> None:
        self.fs, self.root, self.max_bytes, self.base = fs, root.rstrip("/"), max_bytes, base

    def get(self, rel: str, etag: str | None = None, modified: str | None = None) -> Loaded:
        path = f"{self.root}/{rel.lstrip('/')}"
        try:
            data = read_file(self.fs, path, self.max_bytes)
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise FetchError(f"cannot read {path}: {exc}") from exc
        return Loaded(content=data.decode("utf-8", errors="replace"), fmt=format_of(rel))


# --------------------------------------------------------------------------- Sphinx sites


def _search_index(text: str) -> dict[str, Any]:
    m = _SETINDEX.match(text)
    if not m:
        raise FetchError("not a Sphinx searchindex.js")
    data = json.loads(m.group(1))
    if not isinstance(data, dict):
        raise FetchError("unexpected searchindex.js")
    return data


def _excluded(docname: str, settings: KbSettings) -> bool:
    if docname.startswith("_api/") and not settings.fetch.include_api_reference:
        return True
    return any(fnmatch.fnmatchcase(docname, p) for p in settings.fetch.exclude_pages)


def _site_unit(
    reader: _Reader,
    ctx: PlanContext,
    source: KbSourceConfig,
    *,
    component: str,
    site_package: str,
    package: str,
    version: str,
    from_installed: bool,
    public_base: str,
) -> Unit | None:
    settings = ctx.settings
    note = None
    try:
        index_text = reader.get(f"{site_package}/{version}/searchindex.js").content
    except FileNotFoundError:
        if not from_installed:
            ctx.warn(f"{site_package} {version}: no such docs version", source.id)
            return None
        note = f"docs for {package} {version} not published; using stable"
        version = "stable"
        try:
            index_text = reader.get(f"{site_package}/{version}/searchindex.js").content
        except FileNotFoundError:
            ctx.warn(f"{site_package}: no docs found", source.id)
            return None
    index = _search_index(index_text)
    docnames = list(index.get("docnames") or [])
    filenames = list(index.get("filenames") or docnames)
    titles = list(index.get("titles") or [])
    fmt = "sources" if source.format == "sources" else "html"
    unit = Unit(
        key=f"{source.id}:{site_package}:{version}",
        source=source.id,
        component=component,
        package=package,
        version=version,
        fingerprint=_sha(f"{_sha(index_text)}:{fmt}:{settings.fetch.include_api_reference}"),
        note=note,
    )
    base = public_base if public_base.endswith("/") else public_base + "/"
    for i, docname in enumerate(docnames):
        if _excluded(docname, settings) or not matches(docname, source.include, source.exclude):
            continue
        if len(unit.pages) >= settings.fetch.max_pages_per_package:
            ctx.warn(f"{site_package} {version}: page cap reached", source.id)
            break
        url = f"{base}{site_package}/{version}/{docname}.html"
        if fmt == "sources":
            filename = filenames[i] if i < len(filenames) else f"{docname}.rst"
            rel = f"{site_package}/{version}/_sources/{filename}.txt"
        else:
            rel = f"{site_package}/{version}/{docname}.html"
        title = str(titles[i]) if i < len(titles) else docname

        def load(etag: str | None, modified: str | None, rel: str = rel) -> Loaded:
            loaded = reader.get(rel, etag, modified)
            if not loaded.not_modified and rel.endswith(".txt"):
                loaded.fmt = format_of(rel[: -len(".txt")])
            return loaded

        unit.pages.append(PageRef(url=url, title=title, load=load))
    return unit


def _all_providers(reader: _Reader) -> list[str]:
    try:
        text = reader.get(f"{PROVIDERS_INDEX}/stable/packages-ref.html").content
    except FileNotFoundError as exc:
        raise SourceError("the providers package list is not available") from exc
    return sorted(set(_PROVIDER_LINK.findall(text)))


def _apache_units(
    reader: _Reader,
    ctx: PlanContext,
    source: KbSourceConfig,
    components: list[str],
    versions: list[str],
    public_base: str,
) -> Iterator[Unit]:
    settings = ctx.settings
    for cid in components:
        comp = COMPONENTS.get(cid)
        if comp is None or comp.family != "apache":
            continue
        if cid == "providers":
            installed = installed_providers()
            names = list(installed) if settings.providers == "installed" else _all_providers(reader)
            names = [n for n in names if n != PROVIDERS_INDEX]
            packages = [
                n
                for n in names
                if provider_wanted(n, settings.provider_include, settings.provider_exclude)
            ]
            # Provider versions differ from Airflow's: only installed / stable apply.
            keywords = [v for v in versions if v in ("installed", "stable")] or ["installed"]
            for name in [PROVIDERS_INDEX, *packages]:
                for version, from_installed in resolve_versions(keywords, installed.get(name)):
                    unit = _site_unit(
                        reader,
                        ctx,
                        source,
                        component=cid,
                        site_package=name,
                        package=name,
                        version=version,
                        from_installed=from_installed,
                        public_base=public_base,
                    )
                    if unit is not None:
                        yield unit
            continue
        assert comp.site_package is not None
        current = installed_version(comp.distribution) if comp.distribution else None
        for version, from_installed in resolve_versions(versions, current):
            unit = _site_unit(
                reader,
                ctx,
                source,
                component=cid,
                site_package=comp.site_package,
                package=comp.distribution or comp.site_package,
                version=version,
                from_installed=from_installed,
                public_base=public_base,
            )
            if unit is not None:
                yield unit


# --------------------------------------------------------------------------- llms.txt


def _llms_links(text: str) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """(page links, version index links) of an llms.txt."""
    pages: list[tuple[str, str]] = []
    versions: list[tuple[str, str]] = []
    in_versions = False
    for line in text.splitlines():
        if line.startswith("## "):
            in_versions = line[3:].strip().lower() == "versions"
            continue
        for title, url in _MD_LINK.findall(line):
            if url.endswith("llms.txt"):
                if in_versions:
                    versions.append((title, url))
            elif url.endswith((".md", ".mdx")) and not in_versions:
                pages.append((title, url))
    return pages, versions


def _llms_unit(
    url: str,
    ctx: PlanContext,
    source: KbSourceConfig,
    component: str,
    version_wanted: str,
    *,
    own_host: bool,
    private: bool,
) -> Unit | None:
    reader = _HttpReader(url, ctx, own_host=own_host, private=private)
    try:
        text = reader.get(url).content
    except FileNotFoundError:
        ctx.warn(f"{url}: not found", source.id)
        return None
    pages, versions = _llms_links(text)
    label = "current"
    explicit = version_wanted not in ("installed", "stable", "current")
    chosen: tuple[str, str] | None = None
    if explicit:
        chosen = next((v for v in versions if version_wanted in v[0]), None)
        if chosen is None:
            ctx.warn(f"{url}: no version {version_wanted}", source.id)
            return None
    elif not pages and versions:
        chosen = next((v for v in versions if "current" in v[0].lower()), versions[0])
    if chosen is not None:
        label = version_wanted if explicit else "current"
        text = reader.get(chosen[1]).content
        pages, _ = _llms_links(text)
    unit = Unit(
        key=f"{source.id}:{component}:{label}",
        source=source.id,
        component=component,
        package=component,
        version=label,
        fingerprint=_sha(text),
    )
    allowed = (
        list(reader.domains)
        if reader.private
        else [*ctx.settings.fetch.allowed_domains, *reader.domains]
    )
    from autoair.ai.net import host_matches

    for title, link in pages:
        if not host_matches(host_of(link), allowed) or not matches(
            urlsplit(link).path, source.include, source.exclude
        ):
            continue
        if len(unit.pages) >= ctx.settings.fetch.max_pages_per_package:
            ctx.warn(f"{url}: page cap reached", source.id)
            break
        canonical = re.sub(r"\.mdx?$", "", link)

        def load(etag: str | None, modified: str | None, link: str = link) -> Loaded:
            loaded = reader.get(link, etag, modified)
            loaded.fmt = "md"
            return loaded

        unit.pages.append(PageRef(url=canonical, title=title, load=load))
    return unit


def _astronomer_units(
    ctx: PlanContext, source: KbSourceConfig, components: list[str], versions: list[str]
) -> Iterator[Unit]:
    for cid in components:
        comp = COMPONENTS.get(cid)
        if comp is None or comp.llms_url is None:
            continue
        for version in dict.fromkeys(
            v if v not in ("installed", "stable") else "current" for v in versions
        ):
            unit = _llms_unit(
                comp.llms_url, ctx, source, cid, version, own_host=False, private=False
            )
            if unit is not None:
                yield unit


# --------------------------------------------------------------------------- folders


def _read_version(reader: _FsReader, rel: str, pattern: re.Pattern[str]) -> str | None:
    try:
        m = pattern.search(reader.get(rel).content)
    except (FileNotFoundError, FetchError):
        return None
    return m.group(1) if m else None


def _provider_yaml(text: str) -> tuple[str | None, str | None]:
    name = re.search(r"^package-name:\s*([\w.-]+)", text, re.MULTILINE)
    version = re.search(r"^versions:\s*\n\s*-\s*['\"]?([\w.+-]+)", text, re.MULTILINE)
    return (name.group(1) if name else None), (version.group(1) if version else None)


def _resolver(reader: _FsReader, docs_root: str, file_rel: str) -> IncludeResolver:
    """Resolve rst includes inside the checkout (never outside its root)."""

    def resolve(target: str, _opts: dict[str, str]) -> str | None:
        target = target.strip()
        if not target or "://" in target:
            return None
        if target.startswith("/"):
            rel = posixpath.normpath(posixpath.join(docs_root, target.lstrip("/")))
        else:
            rel = posixpath.normpath(posixpath.join(posixpath.dirname(file_rel), target))
        if rel.startswith("..") or rel.startswith("/"):
            return None
        try:
            return reader.get(rel).content
        except (FileNotFoundError, FetchError):
            return None

    return resolve


def _repo_units(
    reader: _FsReader,
    ctx: PlanContext,
    source: KbSourceConfig,
    components: list[str],
    fingerprint: str | None,
) -> Iterator[Unit]:
    settings = ctx.settings
    public = source.public_base_url or OFFICIAL_DOCS
    public = public if public.endswith("/") else public + "/"
    targets: list[tuple[str, str, str, str, str | None]] = []  # comp, docs, site pkg, pkg, ver
    for cid in components:
        comp = COMPONENTS.get(cid)
        if comp is None or comp.family != "apache":
            continue
        if cid == "providers":
            installed = installed_providers()
            for path, _rel in walk_files(
                reader.fs, f"{reader.root}/providers", ("provider.yaml",), limit=2_000
            ):
                try:
                    text = read_file(reader.fs, path, 1024 * 1024).decode("utf-8", "replace")
                except (OSError, FetchError):
                    continue
                name, version = _provider_yaml(text)
                if not name or not name.startswith(PROVIDER_PREFIX):
                    continue
                if settings.providers == "installed" and name not in installed:
                    continue
                if not provider_wanted(name, settings.provider_include, settings.provider_exclude):
                    continue
                targets.append((cid, provider_repo_path(name), name, name, version))
            continue
        assert comp.repo_path is not None
        assert comp.site_package is not None
        project = comp.repo_path.split("/", 1)[0]
        if cid == "helm_chart":
            version = _read_version(reader, "chart/Chart.yaml", _CHART_VERSION)
        else:
            version = _read_version(reader, f"{project}/pyproject.toml", _VERSION_LINE)
        targets.append(
            (
                cid,
                comp.repo_path,
                comp.site_package,
                comp.distribution or comp.site_package,
                version,
            )
        )
    for cid, docs_root, site_package, package, version in targets:
        label = version or "unversioned"
        unit = Unit(
            key=f"{source.id}:{site_package}:{label}",
            source=source.id,
            component=cid,
            package=package,
            version=label,
            fingerprint=_sha(f"{fingerprint}:{docs_root}") if fingerprint else None,
        )
        try:
            files = list(
                walk_files(
                    reader.fs,
                    f"{reader.root}/{docs_root}",
                    _DOC_SUFFIXES,
                    limit=settings.fetch.max_pages_per_package * 4,
                )
            )
        except FetchError:
            continue
        for path, rel in files:
            docname = rel.rsplit(".", 1)[0]
            if docname.startswith(("_api/", "_build/", "_templates/", "_static/")) or _excluded(
                docname, settings
            ):
                continue
            if not matches(docname, source.include, source.exclude):
                continue
            if len(unit.pages) >= settings.fetch.max_pages_per_package:
                ctx.warn(f"{docs_root}: page cap reached", source.id)
                break
            file_rel = path[len(reader.root) :].lstrip("/")
            url = f"{public}{site_package}/{version or 'stable'}/{docname}.html"

            def load(
                _e: str | None, _m: str | None, file_rel: str = file_rel, docs_root: str = docs_root
            ) -> Loaded:
                loaded = reader.get(file_rel)
                loaded.resolve = _resolver(reader, docs_root, file_rel)
                return loaded

            unit.pages.append(PageRef(url=url, title=docname, load=load))
        if unit.pages:
            yield unit


def _sphinx_dir_units(
    reader: _FsReader,
    ctx: PlanContext,
    source: KbSourceConfig,
    components: list[str],
    versions: list[str],
) -> Iterator[Unit]:
    public = source.public_base_url or OFFICIAL_DOCS
    yield from _apache_units(reader, ctx, source, components, versions, public)


def _files_units(
    reader: _FsReader, ctx: PlanContext, source: KbSourceConfig, fingerprint: str | None
) -> Iterator[Unit]:
    explicit = [v for v in (source.versions or []) if v not in ("installed", "stable")]
    version = explicit[0] if explicit else "unversioned"
    component = str(source.components[0]) if source.components else source.component
    unit = Unit(
        key=f"{source.id}:files:{version}",
        source=source.id,
        component=component,
        package=source.id,
        version=version,
        fingerprint=fingerprint,
    )
    base = source.public_base_url
    for _path, rel in walk_files(
        reader.fs, reader.root, _FILE_SUFFIXES, limit=ctx.settings.fetch.max_pages * 2
    ):
        if not matches(rel, source.include, source.exclude):
            continue
        if len(unit.pages) >= ctx.settings.fetch.max_pages_per_package:
            ctx.warn(f"{source.id}: page cap reached", source.id)
            break
        url = (base.rstrip("/") + "/" + rel) if base else f"kb://{source.id}/{rel}"

        def load(_e: str | None, _m: str | None, rel: str = rel) -> Loaded:
            return reader.get(rel)

        unit.pages.append(PageRef(url=url, title=rel, load=load))
    if unit.pages:
        yield unit


def _detect(reader: _FsReader) -> str:
    fs, root = reader.fs, reader.root
    if fs.exists(f"{root}/airflow-core/docs") or fs.exists(f"{root}/providers"):
        return "airflow_repo"
    try:
        if fs.glob(f"{root}/*/*/searchindex.js"):
            return "sphinx_site"
    except OSError:
        pass
    return "files"


def _folder_units(
    reader: _FsReader,
    ctx: PlanContext,
    source: KbSourceConfig,
    components: list[str],
    versions: list[str],
    fingerprint: str | None,
) -> Iterator[Unit]:
    layout = source.layout if source.layout != "auto" else _detect(reader)
    if layout == "airflow_repo":
        yield from _repo_units(reader, ctx, source, components, fingerprint)
    elif layout == "sphinx_site":
        yield from _sphinx_dir_units(reader, ctx, source, components, versions)
    else:
        yield from _files_units(reader, ctx, source, fingerprint)


# --------------------------------------------------------------------------- entry point


#: Why a source cannot be read, by code (the admin UI shows ``kb.reason.<code>``).
UNUSABLE_REASONS = {
    "disabled": "disabled",
    "air_gapped": "air-gapped: the official sites need kb.online=true",
}


def unusable_code(source: KbSourceConfig, settings: KbSettings) -> str | None:
    """The :data:`UNUSABLE_REASONS` code of why ``source`` cannot be read now."""
    if source.kind == "official" and not settings.online:
        return "air_gapped"
    return None


def usable(source: KbSourceConfig, settings: KbSettings) -> str | None:
    """Why ``source`` cannot be read now (``None``: it can)."""
    code = unusable_code(source, settings)
    return UNUSABLE_REASONS[code] if code else None


def plan_units(
    source: KbSourceConfig,
    ctx: PlanContext,
    *,
    components: list[str],
    versions: list[str],
) -> Iterator[Unit]:
    """The units of ``source`` (raises :class:`SourceError` when it cannot be listed)."""
    try:
        if source.kind == "official":
            reader = _HttpReader(OFFICIAL_DOCS, ctx, own_host=False, private=False)
            yield from _apache_units(reader, ctx, source, components, versions, OFFICIAL_DOCS)
            yield from _astronomer_units(ctx, source, components, versions)
        elif source.kind == "site":
            assert source.url is not None
            reader = _HttpReader(source.url, ctx, own_host=True, private=source.allow_private)
            public = source.public_base_url or source.url
            yield from _apache_units(reader, ctx, source, components, versions, public)
        elif source.kind == "llms_txt":
            assert source.url is not None
            component = str(source.components[0]) if source.components else source.component
            for version in dict.fromkeys(
                v if v not in ("installed", "stable") else "current" for v in versions
            ):
                unit = _llms_unit(
                    source.url,
                    ctx,
                    source,
                    component,
                    version,
                    own_host=True,
                    private=source.allow_private,
                )
                if unit is not None:
                    yield unit
        elif source.kind in ("git", "path"):
            fingerprint: str | None = None
            if source.kind == "git":
                from autoair.kb.fetch import checkout

                sparse = _sparse(source)
                work, sha = checkout(
                    source.url,
                    source.ref,
                    ctx.cache_dir / "git" / source.id,
                    conn_id=source.conn_id,
                    sparse=sparse,
                    timeout=ctx.settings.fetch.git_timeout_seconds,
                )
                fingerprint = sha
                fs, root = open_fs(str(work), None, {})
                base = f"git:{source.id}"
            else:
                assert source.url is not None
                fs, root = open_fs(source.url, source.conn_id, source.storage_options)
                base = source.url
            folder = _FsReader(fs, root, ctx.settings.fetch.max_page_bytes, base)
            yield from _folder_units(folder, ctx, source, components, versions, fingerprint)
        # bundle sources are imported by autoair.kb.bundle (pages are already parsed).
    except (FetchError, OSError, ValueError) as exc:
        raise SourceError(str(exc)) from exc


def _sparse(source: KbSourceConfig) -> list[str]:
    if source.layout not in ("airflow_repo",):
        return []
    return [
        "/airflow-core/docs/",
        "/airflow-core/pyproject.toml",
        "/airflow-core/src/airflow/example_dags/",
        "/task-sdk/docs/",
        "/task-sdk/pyproject.toml",
        "/airflow-ctl/docs/",
        "/airflow-ctl/pyproject.toml",
        "/chart/docs/",
        "/chart/Chart.yaml",
        "/providers/**/docs/",
        "/providers/**/provider.yaml",
        "/providers/**/tests/system/",
        "/providers/**/example_dags/",
        "/devel-common/src/docs/",
    ]


__all__ = [
    "Loaded",
    "PageRef",
    "PlanContext",
    "SourceError",
    "Unit",
    "plan_units",
    "usable",
]
