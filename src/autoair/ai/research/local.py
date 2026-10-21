"""Air-gapped research: what is installed, provider metadata, docs and source.

Nothing here imports the code it describes, except a provider's tiny
``get_provider_info`` entry point (the same metadata Airflow itself reads). Source files
are located on disk and parsed with :mod:`ast`; reads are confined to the interpreter's
site-packages, AutoAir's custom operator folders and ``ai.research.extra_source_dirs``.
"""

from __future__ import annotations

import ast
import contextlib
import fnmatch
import importlib.metadata as md
import logging
import re
import site
import sys
import sysconfig
import threading
from collections.abc import Iterable, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from autoair.ai.chunking import chunk_python, docstring_summary
from autoair.ai.rag import Document

log = logging.getLogger("autoair.ai.research")

READABLE_SUFFIXES = frozenset(
    {".py", ".pyi", ".md", ".rst", ".txt", ".yaml", ".yml", ".toml", ".cfg", ".json"}
)
_MODULE = re.compile(r"^[A-Za-z_][\w]*(\.[A-Za-z_][\w]*)*$")


def normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


class PackageInfo(BaseModel):
    name: str
    version: str
    summary: str = ""
    requires_python: str | None = None
    home_page: str | None = None
    is_provider: bool = False
    provider: dict[str, Any] | None = Field(
        default=None, description="Compact provider metadata (get_provider_info)."
    )


class SourceResult(BaseModel):
    target: str
    path: str
    symbol: str | None = None
    start_line: int = 1
    text: str
    truncated: bool = False
    symbols: list[str] = Field(default_factory=list, description="Top-level classes/functions.")


class ResearchError(ValueError):
    pass


# --------------------------------------------------------------------------- packages


def _meta(dist: md.Distribution, key: str) -> str | None:
    meta: Any = dist.metadata  # email.message.Message: .get() avoids KeyError deprecations
    value = meta.get(key)
    return str(value) if value else None


def _dist_name(dist: md.Distribution) -> str:
    return _meta(dist, "Name") or ""


@lru_cache(maxsize=1)
def _distributions() -> tuple[md.Distribution, ...]:
    seen: dict[str, md.Distribution] = {}
    for dist in md.distributions():
        name = _dist_name(dist)
        if name and normalise(name) not in seen:
            seen[normalise(name)] = dist
    return tuple(sorted(seen.values(), key=lambda d: normalise(_dist_name(d))))


def refresh() -> None:
    """Forget cached package metadata (after installs)."""
    _distributions.cache_clear()
    top_level_modules.cache_clear()
    _provider_entry_points.cache_clear()
    provider_info.cache_clear()


@lru_cache(maxsize=1)
def _provider_entry_points() -> dict[str, md.EntryPoint]:
    out: dict[str, md.EntryPoint] = {}
    for ep in md.entry_points(group="apache_airflow_provider"):
        dist = getattr(ep, "dist", None)
        if dist is not None:
            out[normalise(_dist_name(dist))] = ep
    return out


def _compact_provider(info: dict[str, Any]) -> dict[str, Any]:
    def modules(key: str) -> list[str]:
        out: list[str] = []
        for entry in info.get(key) or []:
            out.extend(str(m) for m in entry.get("python-modules") or [])
        return out

    return {
        "package-name": info.get("package-name"),
        "name": info.get("name"),
        "description": re.sub(r"<[^>]+>|`", "", str(info.get("description") or ""))[:600],
        "integrations": [str(i.get("integration-name")) for i in (info.get("integrations") or [])][
            :40
        ],
        "operators": modules("operators")[:80],
        "sensors": modules("sensors")[:80],
        "hooks": modules("hooks")[:80],
        "triggers": modules("triggers")[:40],
        "connection-types": [
            str(c.get("connection-type")) for c in info.get("connection-types") or []
        ][:30],
    }


@lru_cache(maxsize=512)
def provider_info(name: str) -> dict[str, Any] | None:
    """Compact ``get_provider_info()`` of an installed provider distribution."""
    ep = _provider_entry_points().get(normalise(name))
    if ep is None:
        return None
    try:
        info = ep.load()()
    except Exception:
        log.debug("provider info of %s failed", name, exc_info=True)
        return None
    return _compact_provider(info) if isinstance(info, dict) else None


def package_info(name: str) -> PackageInfo | None:
    wanted = normalise(name)
    dist = next((d for d in _distributions() if normalise(_dist_name(d)) == wanted), None)
    if dist is None:
        return None
    is_provider = wanted in _provider_entry_points()
    return PackageInfo(
        name=_dist_name(dist),
        version=str(dist.version),
        summary=_meta(dist, "Summary") or "",
        requires_python=_meta(dist, "Requires-Python"),
        home_page=_meta(dist, "Home-page"),
        is_provider=is_provider,
        provider=provider_info(name) if is_provider else None,
    )


def installed_packages(
    pattern: str | None = None, *, providers_only: bool = False, limit: int = 500
) -> list[PackageInfo]:
    providers = _provider_entry_points()
    out: list[PackageInfo] = []
    for dist in _distributions():
        name = _dist_name(dist)
        norm = normalise(name)
        if providers_only and norm not in providers:
            continue
        if pattern and not (
            fnmatch.fnmatch(norm, normalise(pattern)) or normalise(pattern) in norm
        ):
            continue
        out.append(
            PackageInfo(
                name=name,
                version=str(dist.version),
                summary=(_meta(dist, "Summary") or "")[:200],
                is_provider=norm in providers,
            )
        )
        if len(out) >= limit:
            break
    return out


@lru_cache(maxsize=4)
def top_level_modules(limit: int = 4000) -> list[str]:
    """Importable top-level module names of installed distributions (for writers):
    ``top_level.txt`` when present, else the top-level entries of the RECORD."""
    names: set[str] = set()
    for dist in _distributions():
        top = dist.read_text("top_level.txt")
        if top:
            names.update(line.strip() for line in top.splitlines() if line.strip())
            continue
        for file in dist.files or []:
            first = file.parts[0] if file.parts else ""
            if not first or first == ".." or first.endswith((".dist-info", ".data", ".pth")):
                continue
            name = first.removesuffix(".py")
            if name.isidentifier() and (len(file.parts) > 1 or first.endswith(".py")):
                names.add(name)
    return sorted(n for n in names if not n.startswith("_"))[:limit]


# --------------------------------------------------------------------------- source access


def allowed_roots(extra: Iterable[Path] = ()) -> list[Path]:
    roots: list[Path] = []
    candidates: list[str] = []
    with contextlib.suppress(AttributeError):  # virtualenvs without getsitepackages
        candidates.extend(site.getsitepackages())
    user = site.getusersitepackages() if hasattr(site, "getusersitepackages") else None
    if isinstance(user, str):
        candidates.append(user)
    paths = sysconfig.get_paths()
    candidates.extend([paths.get("purelib", ""), paths.get("platlib", "")])
    for value in [*candidates, *(str(p) for p in extra)]:
        if not value:
            continue
        p = Path(value).expanduser()
        try:
            resolved = p.resolve()
        except OSError:
            continue
        if resolved.is_dir() and resolved not in roots:
            roots.append(resolved)
    return roots


def _inside(path: Path, roots: Sequence[Path]) -> bool:
    return any(path == r or path.is_relative_to(r) for r in roots)


def module_file(module: str, roots: Sequence[Path]) -> Path | None:
    """Locate a module's source without importing it (``a.b.c`` -> ``a/b/c.py`` or
    ``a/b/c/__init__.py`` under an allowed root)."""
    if not _MODULE.match(module):
        return None
    rel = Path(*module.split("."))
    search = [*roots, *(Path(p).resolve() for p in sys.path if p and Path(p).is_dir())]
    for root in search:
        for candidate in (root / f"{rel}.py", root / rel / "__init__.py", root / f"{rel}.pyi"):
            if candidate.is_file():
                resolved = candidate.resolve()
                if _inside(resolved, roots):
                    return resolved
    return None


def resolve_target(target: str, roots: Sequence[Path]) -> Path:
    """A module name or a file path, confined to ``roots`` (no traversal, no symlink
    escapes, only text-like suffixes)."""
    target = target.strip()
    path: Path | None = None
    if _MODULE.match(target) and "/" not in target:
        path = module_file(target, roots)
        if path is None:
            # ``pkg.mod.Class`` -> module ``pkg.mod``
            head = target.rsplit(".", 1)[0]
            path = module_file(head, roots) if "." in target else None
    if path is None:
        raw = Path(target).expanduser()
        candidates = [raw] if raw.is_absolute() else [r / raw for r in roots]
        for candidate in candidates:
            try:
                resolved = candidate.resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            if _inside(resolved, roots):
                path = resolved
                break
    if path is None:
        raise ResearchError(
            f"{target!r} is not an installed module or a file inside the allowed folders"
        )
    if not _inside(path, roots):
        raise ResearchError("access outside the allowed folders is not permitted")
    if path.suffix.lower() not in READABLE_SUFFIXES or not path.is_file():
        raise ResearchError(f"{path.name}: only source/text files can be read")
    return path


def _symbols(tree: ast.Module) -> list[str]:
    return [
        n.name
        for n in tree.body
        if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def read_source(
    target: str,
    *,
    symbol: str | None = None,
    roots: Sequence[Path],
    max_chars: int = 60_000,
) -> SourceResult:
    path = resolve_target(target, roots)
    if path.stat().st_size > 5 * 1024 * 1024:
        raise ResearchError(f"{path.name} is too large")
    text = path.read_text(encoding="utf-8", errors="replace")
    symbols: list[str] = []
    start = 1
    if path.suffix in (".py", ".pyi"):
        try:
            tree = ast.parse(text)
        except SyntaxError:
            tree = None
        if tree is not None:
            symbols = _symbols(tree)
            wanted = symbol or (
                target.rsplit(".", 1)[-1] if target.rsplit(".", 1)[-1] in symbols else None
            )
            if wanted:
                parts = wanted.split(".")
                node: Any = tree
                found: Any = None
                for part in parts:
                    found = next(
                        (
                            n
                            for n in getattr(node, "body", [])
                            if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                            and n.name == part
                        ),
                        None,
                    )
                    if found is None:
                        break
                    node = found
                if found is None:
                    raise ResearchError(
                        f"{wanted!r} not found in {path.name} (symbols: {', '.join(symbols[:40])})"
                    )
                lines = text.splitlines()
                first = min([found.lineno, *(d.lineno for d in found.decorator_list)])
                text = "\n".join(lines[first - 1 : found.end_lineno or found.lineno])
                start = first
                symbol = wanted
    truncated = len(text) > max_chars
    return SourceResult(
        target=target,
        path=str(path),
        symbol=symbol,
        start_line=start,
        text=text[:max_chars],
        truncated=truncated,
        symbols=symbols[:200],
    )


# --------------------------------------------------------------------------- index


def _module_of(rel: str) -> str:
    rel = rel.removesuffix(".py").removesuffix("/__init__")
    return rel.replace("/", ".")


class InstalledIndex:
    """Documents describing the installed environment (built once, rebuilt when the set
    of distributions changes): one ``package`` document per provider and ``source``
    documents of module/class docstrings and example DAGs."""

    def __init__(self, patterns: Sequence[str], *, max_modules: int = 4000) -> None:
        self.patterns = [normalise(p) for p in patterns]
        self.max_modules = max_modules
        self._lock = threading.Lock()
        self._key: tuple[tuple[str, str], ...] = ()
        self._docs: list[Document] = []

    def _wanted(self, dist: md.Distribution) -> bool:
        norm = normalise(_dist_name(dist))
        return any(fnmatch.fnmatch(norm, p) for p in self.patterns)

    def documents(self) -> list[Document]:
        dists = [d for d in _distributions() if self._wanted(d)]
        key = tuple((normalise(_dist_name(d)), str(d.version)) for d in dists)
        with self._lock:
            if key == self._key:
                return self._docs
            self._docs = self._build(dists)
            self._key = key
            return self._docs

    def _build(self, dists: Sequence[md.Distribution]) -> list[Document]:
        docs: list[Document] = []
        budget = self.max_modules
        for dist in dists:
            name = _dist_name(dist)
            info = provider_info(name)
            if info is not None:
                text = "\n".join(
                    x
                    for x in (
                        str(info.get("description") or ""),
                        "Integrations: " + ", ".join(info.get("integrations") or []),
                        "Operators: " + ", ".join(info.get("operators") or []),
                        "Sensors: " + ", ".join(info.get("sensors") or []),
                        "Hooks: " + ", ".join(info.get("hooks") or []),
                        "Connection types: " + ", ".join(info.get("connection-types") or []),
                    )
                    if x.split(": ", 1)[-1]
                )
                docs.append(
                    Document(
                        id=f"package:{normalise(name)}",
                        kind="package",
                        title=f"{name} {dist.version} ({info.get('name') or ''})",
                        text=text,
                        uri=f"autoair://packages/{normalise(name)}",
                        meta={"package": normalise(name), "version": str(dist.version)},
                    )
                )
            for file in dist.files or []:
                rel = str(file)
                if not rel.endswith(".py") or "/tests/" in rel or rel.startswith("tests/"):
                    continue
                if budget <= 0:
                    return docs
                example = "example_dags/" in rel or "/example_" in rel
                if (
                    not example
                    and not any(
                        seg in rel
                        for seg in (
                            "/operators/",
                            "/sensors/",
                            "/hooks/",
                            "/triggers/",
                            "/decorators/",
                            "/transfers/",
                            "/notifications/",
                            "/sdk/",
                            "/models/baseoperator",
                            "/utils/",
                        )
                    )
                    and not rel.endswith("__init__.py")
                ):
                    continue
                try:
                    path = Path(str(dist.locate_file(file)))
                    source = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                budget -= 1
                module = _module_of(rel)
                chunks = (
                    chunk_python(source, module) if example else docstring_summary(source, module)
                )
                for n, chunk in enumerate(chunks):
                    docs.append(
                        Document(
                            id=f"source:{module}#{n}",
                            kind="source",
                            title=("Example: " if example else "") + chunk.title,
                            text=chunk.text,
                            uri=f"autoair://source/{module}",
                            meta={
                                "package": normalise(name),
                                "module": module,
                                "symbol": chunk.anchor,
                                "example": example,
                                "line": chunk.start_line,
                            },
                        )
                    )
        return docs


__all__ = [
    "InstalledIndex",
    "PackageInfo",
    "ResearchError",
    "SourceResult",
    "allowed_roots",
    "installed_packages",
    "module_file",
    "normalise",
    "package_info",
    "provider_info",
    "read_source",
    "refresh",
    "resolve_target",
    "top_level_modules",
]
