"""Blueprint catalog: Astronomer Blueprint templates available as canvas nodes.

Discovery mirrors the blueprint project's own registry (``blueprint.registry``) without
importing anything in this process:

* packages advertising modules under the ``airflow_blueprint.blueprints`` entry-point group
  (read from distribution metadata; the module files are located on ``sys.path``);
* ``blueprints.template_dirs``: ``*.py`` files under the folders (names starting with ``_``
  skipped, like the registry does);
* ``blueprints.modules``: explicitly configured modules.

Files are parsed with :mod:`ast` and kept when they define a class whose base is
``Blueprint`` / ``Blueprint[Config]`` (the registry's own AST pre-filter). Names and versions
follow ``Blueprint.parse_name_and_version`` (snake_case class name, ``V<n>`` suffix, or the
``name`` / ``version`` class attributes). Config schemas need the Pydantic model, so one child
process per build imports the modules and reads ``Blueprint.get_schema()``
(:mod:`autoair.blueprints.worker`).

Builds are derived data: cached in memory and on disk under a key made of the source files'
paths, sizes and modification times, so every replica computes the same catalog.
"""

from __future__ import annotations

import ast
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, entry_points
from importlib.metadata import version as dist_version
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from autoair import __version__
from autoair._subprocess import user_code_env
from autoair.blueprints.config import BlueprintSettings
from autoair.blueprints.models import (
    BlueprintCatalogData,
    BlueprintCatalogError,
    BlueprintCatalogStatus,
    BlueprintDetail,
    BlueprintPreview,
    BlueprintSource,
    BlueprintSummary,
)
from autoair.blueprints.worker import MARKER

log = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "airflow_blueprint.blueprints"
DISTRIBUTION = "airflow-blueprint"
_BASE_NAME = "Blueprint"
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SNAKE = re.compile(r"^[a-z][a-z0-9_]*$")


def package_version() -> str | None:
    try:
        return dist_version(DISTRIBUTION)
    except PackageNotFoundError:
        return None


# --------------------------------------------------------------------------- static analysis


def to_snake_case(class_name: str) -> str:
    """``Blueprint._to_snake_case``: ``MultiSourceETL`` -> ``multi_source_etl``."""
    snake = re.sub("([A-Z]+)([A-Z][a-z])", r"\1_\2", class_name)
    snake = re.sub(r"([a-z\d])([A-Z])", r"\1_\2", snake)
    return snake.lower()


def name_and_version(
    class_name: str, explicit_name: object = None, explicit_version: object = None
) -> tuple[str, int]:
    """``Blueprint.parse_name_and_version`` from statically read attributes."""
    match = re.match(r"^(.+?)V(\d+)$", class_name)
    base, inferred = (match.group(1), int(match.group(2))) if match else (class_name, 1)
    name = explicit_name if isinstance(explicit_name, str) else to_snake_case(base)
    version = (
        explicit_version
        if isinstance(explicit_version, int)
        and not isinstance(explicit_version, bool)
        and explicit_version >= 1
        else inferred
    )
    return name, version


@dataclass(frozen=True)
class StaticBlueprint:
    class_name: str
    name: str
    version: int
    config_class: str | None
    docstring: str | None
    supports_params: bool


def _base_target(base: ast.expr) -> tuple[bool, str | None]:
    """Whether ``base`` is ``Blueprint`` / ``Blueprint[Config]`` (also ``x.Blueprint``), and
    the config class name written in the subscript."""
    target = base.value if isinstance(base, ast.Subscript) else base
    is_bp = (isinstance(target, ast.Name) and target.id == _BASE_NAME) or (
        isinstance(target, ast.Attribute) and target.attr == _BASE_NAME
    )
    if not is_bp or not isinstance(base, ast.Subscript):
        return is_bp, None
    arg = base.slice
    if isinstance(arg, ast.Name):
        return True, arg.id
    if isinstance(arg, ast.Attribute):
        return True, arg.attr
    return True, None


def _class_constants(node: ast.ClassDef) -> dict[str, object]:
    out: dict[str, object] = {}
    for stmt in node.body:
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(stmt, ast.Assign):
            targets, value = list(stmt.targets), stmt.value
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            targets, value = [stmt.target], stmt.value
        if isinstance(value, ast.Constant):
            for t in targets:
                if isinstance(t, ast.Name):
                    out[t.id] = value.value
    return out


def parse_blueprints(source: str, filename: str = "<blueprints>") -> list[StaticBlueprint]:
    """Blueprint subclasses defined at the top level of ``source`` (never executed)."""
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError:
        return []
    found: list[StaticBlueprint] = []
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name.startswith("_"):
            continue
        for base in node.bases:
            is_bp, config = _base_target(base)
            if not is_bp:
                continue
            consts = _class_constants(node)
            name, version = name_and_version(node.name, consts.get("name"), consts.get("version"))
            found.append(
                StaticBlueprint(
                    class_name=node.name,
                    name=name,
                    version=version,
                    config_class=config,
                    docstring=ast.get_docstring(node),
                    supports_params=consts.get("supports_params") is True,
                )
            )
            break
    return found


# --------------------------------------------------------------------------- sources


@dataclass(frozen=True)
class SourceFile:
    module: str
    path: Path
    source: BlueprintSource
    path_entry: str | None = None  # folder the child process puts on sys.path
    package: str | None = None
    package_version: str | None = None


def _module_name(relative: Path) -> str | None:
    parts = list(relative.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts or not all(_IDENT.match(p) for p in parts):
        return None
    return ".".join(parts)


def _package_files(module: str, root: Path) -> Iterable[tuple[str, Path]]:
    """The module itself and, for a package, every submodule (``_``-prefixed ones skipped,
    like the registry's entry-point walk)."""
    yield module, root
    if root.name != "__init__.py":
        return
    pkg_dir = root.parent
    for path in sorted(pkg_dir.rglob("*.py")):
        rel = path.relative_to(pkg_dir)
        if path == root or any(p.startswith("_") for p in rel.with_suffix("").parts[:-1]):
            continue
        if path.name.startswith("_") and path.name != "__init__.py":
            continue
        sub = _module_name(rel)
        if sub:
            yield f"{module}.{sub}", path


def _path_entry(module: str, root: Path) -> str:
    """The ``sys.path`` folder ``module`` was found in (its file is ``root``), so the child
    process imports it from the same place without inheriting a working directory."""
    base = root.parent if root.name == "__init__.py" else root.with_suffix("")
    for _ in module.split("."):
        base = base.parent
    return str(base)


def _find_module(module: str) -> Path | None:
    from autoair.catalog.static import find_module_file

    return find_module_file(module)


class _Scan:
    def __init__(self, settings: BlueprintSettings) -> None:
        self.settings = settings
        self.files: list[SourceFile] = []
        self.errors: list[BlueprintCatalogError] = []
        self._seen: set[str] = set()

    def add(self, item: SourceFile) -> None:
        if item.module in self._seen:
            return
        if len(self.files) >= self.settings.max_files:
            self.errors.append(
                BlueprintCatalogError(
                    location=str(item.path),
                    error=f"skipped: more than blueprints.max_files ({self.settings.max_files})",
                )
            )
            return
        self._seen.add(item.module)
        self.files.append(item)

    def entry_points(self) -> None:
        for ep in entry_points(group=ENTRY_POINT_GROUP):
            module = ep.value.strip()
            dist = getattr(ep, "dist", None)
            package = dist.name if dist is not None else None
            pversion = dist.version if dist is not None else None
            root = _find_module(module) if _IDENT.match(module.replace(".", "_")) else None
            if root is None:
                self.errors.append(
                    BlueprintCatalogError(
                        location=f"entry point {ep.name} = {ep.value}",
                        error="module source not found",
                    )
                )
                continue
            entry = _path_entry(module, root)
            for name, path in _package_files(module, root):
                self.add(SourceFile(name, path, "entry_point", entry, package, pversion))

    def template_dirs(self) -> None:
        for folder in self.settings.template_dirs:
            base = Path(folder).expanduser()
            if not base.is_dir():
                self.errors.append(
                    BlueprintCatalogError(location=folder, error="not a readable folder")
                )
                continue
            for path in sorted(base.rglob("*.py")):
                if path.name.startswith("_"):
                    continue
                module = _module_name(path.relative_to(base))
                if module is None:
                    continue
                self.add(SourceFile(module, path, "template_dir", str(base.resolve())))

    def modules(self) -> None:
        for module in self.settings.modules:
            root = _find_module(module)
            if root is None:
                self.errors.append(BlueprintCatalogError(location=module, error="module not found"))
                continue
            self.add(SourceFile(module, root, "module", _path_entry(module, root)))


def scan_sources(
    settings: BlueprintSettings,
) -> tuple[list[SourceFile], list[BlueprintCatalogError]]:
    scan = _Scan(settings)
    if settings.entry_points:
        scan.entry_points()
    scan.template_dirs()
    scan.modules()
    return scan.files, scan.errors


def _stat_key(files: list[SourceFile], settings: BlueprintSettings) -> str:
    parts: list[Any] = [__version__, package_version(), settings.model_dump(mode="json")]
    for f in files:
        try:
            st = f.path.stat()
            parts.append((f.module, str(f.path), st.st_size, st.st_mtime_ns))
        except OSError:
            parts.append((f.module, str(f.path), None, None))
    digest = hashlib.sha256(json.dumps(parts, default=str).encode()).hexdigest()
    return digest[:24]


def _first_sentence(doc: str | None) -> str | None:
    if not doc:
        return None
    text = " ".join(doc.strip().split())
    match = re.match(r"(.+?[.!?])(\s|$)", text)
    return (match.group(1) if match else text)[:300]


# --------------------------------------------------------------------------- build


def _run_worker(
    request: dict[str, Any], timeout: float
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    code = "from autoair.blueprints.worker import main; main()"
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            input=json.dumps(request),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=user_code_env(),  # blueprint modules are user code: no AutoAir secrets
            # Not the API process's working directory: only the listed paths join sys.path.
            cwd=tempfile.gettempdir(),
        )
    except subprocess.TimeoutExpired:
        return [], {"*": f"schema subprocess timed out after {timeout}s"}
    marker = proc.stdout.rfind(MARKER)
    if marker < 0:
        tail = (proc.stderr or proc.stdout).strip().splitlines()[-5:]
        return [], {"*": "schema subprocess failed: " + (" | ".join(tail) or str(proc.returncode))}
    try:
        data = json.loads(proc.stdout[marker + len(MARKER) :])
    except ValueError:
        return [], {"*": "schema subprocess returned invalid output"}
    return list(data.get("results", [])), dict(data.get("errors", {}))


def build_catalog(
    settings: BlueprintSettings, *, read_schemas: bool = True
) -> BlueprintCatalogData:
    """One full build: static discovery, then schemas (and default previews) in a child."""
    files, errors = scan_sources(settings)
    key = _stat_key(files, settings)
    found: dict[str, tuple[SourceFile, list[StaticBlueprint]]] = {}
    for f in files:
        try:
            text = f.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            errors.append(BlueprintCatalogError(location=str(f.path), error=str(exc)))
            continue
        classes = parse_blueprints(text, str(f.path))
        if classes:
            found[f.module] = (f, classes)

    records: dict[tuple[str, str], dict[str, Any]] = {}
    worker_errors: dict[str, str] = {}
    if read_schemas and found:
        if package_version() is None:
            worker_errors["*"] = (
                "airflow-blueprint is not installed in AutoAir's environment "
                "(pip install 'autoair[blueprint]'): config schemas cannot be read"
            )
        else:
            request = {
                "path": sorted({f.path_entry for f, _ in found.values() if f.path_entry}),
                "modules": [
                    {"module": m, "classes": [c.class_name for c in cs]}
                    for m, (_f, cs) in found.items()
                ],
                "preview": True,
            }
            results, worker_errors = _run_worker(request, settings.schema_timeout)
            for r in results:
                records[(r["module"], r["class_name"])] = r
    global_error = worker_errors.get("*")
    for module, error in worker_errors.items():
        if module != "*":
            errors.append(BlueprintCatalogError(location=module, error=error))
    if global_error:
        errors.append(BlueprintCatalogError(location="*", error=global_error))

    details: dict[tuple[str, int], BlueprintDetail] = {}
    for module, (f, classes) in found.items():
        for c in classes:
            rec = records.get((module, c.class_name), {})
            name, version = rec.get("name", c.name), rec.get("version", c.version)
            if not isinstance(name, str) or not _SNAKE.match(name):
                errors.append(
                    BlueprintCatalogError(
                        location=f"{module}.{c.class_name}",
                        error=f"blueprint name {name!r} is not snake_case",
                    )
                )
                continue
            schema_error = rec.get("error") or worker_errors.get(module) or global_error
            if read_schemas and not rec and not schema_error:
                schema_error = "class not found when the module was imported"
            preview = rec.get("preview")
            try:
                detail = BlueprintDetail(
                    name=name,
                    version=int(version),
                    class_name=c.class_name,
                    module=module,
                    class_path=f"{module}.{c.class_name}",
                    config_class=rec.get("config_class", c.config_class) if rec else c.config_class,
                    summary=_first_sentence(rec.get("docstring") or c.docstring),
                    docstring=rec.get("docstring") or c.docstring,
                    source=f.source,
                    package=f.package,
                    package_version=f.package_version,
                    supports_params=bool(rec.get("supports_params", c.supports_params)),
                    schema_error=schema_error if schema_error and not rec.get("schema") else None,
                    config_schema=rec.get("schema"),
                    preview=BlueprintPreview.model_validate(preview) if preview else None,
                )
            except (ValidationError, TypeError, ValueError) as exc:
                errors.append(
                    BlueprintCatalogError(
                        location=f"{module}.{c.class_name}",
                        error=f"skipped: {str(exc).splitlines()[0]}",
                    )
                )
                continue
            ident = (detail.name, detail.version)
            if ident in details:
                other = details[ident]
                errors.append(
                    BlueprintCatalogError(
                        location=detail.class_path,
                        error=f"duplicate blueprint {detail.name!r} v{detail.version} "
                        f"(also {other.class_path}); the first one is used",
                    )
                )
                continue
            details[ident] = detail
    latest: dict[str, int] = {}
    for name, version in details:
        latest[name] = max(latest.get(name, 0), version)
    out = [
        d.model_copy(update={"latest": d.version == latest[d.name]})
        for d in sorted(details.values(), key=lambda d: (d.name, d.version))
    ]
    return BlueprintCatalogData(key=key, built_at=time.time(), blueprints=out, errors=errors)


# --------------------------------------------------------------------------- service


def _default_cache_dir() -> Path:
    from autoair.catalog.service import default_cache_dir

    return default_cache_dir() / "blueprints"


class BlueprintCatalog:
    """Process-local view of the blueprint catalog (derived data, rebuilt when sources change).

    ``settings`` is a section or a callable returning one (read on every access).
    ``enabled`` reports ``features.blueprints`` per call, so validation refuses blueprint
    steps while an admin has switched the feature off.
    """

    def __init__(
        self,
        settings: BlueprintSettings | Callable[[], BlueprintSettings] | None = None,
        *,
        cache_dir: Path | None = None,
        read_schemas: bool = True,
        enabled: Callable[[], bool] | None = None,
    ) -> None:
        self._settings = settings or BlueprintSettings()
        self.enabled: Callable[[], bool] = enabled or (lambda: True)
        self.cache_dir = cache_dir
        self.read_schemas = read_schemas
        self._lock = threading.Lock()
        self._data: BlueprintCatalogData | None = None
        self._checked_at = 0.0

    @property
    def settings(self) -> BlueprintSettings:
        current = self._settings
        return current if isinstance(current, BlueprintSettings) else current()

    def _cache_file(self, key: str) -> Path:
        return (self.cache_dir or _default_cache_dir()) / f"blueprints-{key}.json"

    def _read_cache(self, key: str) -> BlueprintCatalogData | None:
        try:
            raw = self._cache_file(key).read_text(encoding="utf-8")
            return BlueprintCatalogData.model_validate_json(raw)
        except (OSError, ValidationError):
            return None

    def _write_cache(self, data: BlueprintCatalogData) -> None:
        path = self._cache_file(data.key)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(data.model_dump_json(), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            log.warning("blueprint catalog cache not written: %s", exc)

    def data(self, *, force: bool = False) -> BlueprintCatalogData:
        settings = self.settings
        with self._lock:
            now = time.monotonic()
            fresh = (
                self._data is not None
                and not force
                and now - self._checked_at < settings.rescan_interval
            )
            if fresh:
                assert self._data is not None
                return self._data
            files, _errors = scan_sources(settings)
            key = _stat_key(files, settings)
            self._checked_at = now
            if self._data is not None and self._data.key == key and not force:
                return self._data
            data = None if force else self._read_cache(key)
            if data is None:
                data = build_catalog(settings, read_schemas=self.read_schemas)
                if self.read_schemas:
                    self._write_cache(data)
            self._data = data
            return data

    def refresh(self) -> BlueprintCatalogData:
        return self.data(force=True)

    def summaries(self) -> list[BlueprintSummary]:
        return [BlueprintSummary.model_validate(d.model_dump()) for d in self.data().blueprints]

    def get(self, name: str, version: int | None = None) -> BlueprintDetail | None:
        matches = [d for d in self.data().blueprints if d.name == name]
        if version is None:
            return max(matches, key=lambda d: d.version, default=None)
        return next((d for d in matches if d.version == version), None)

    def status(self) -> BlueprintCatalogStatus:
        data = self.data()
        return BlueprintCatalogStatus(
            available=package_version() is not None,
            package_version=package_version(),
            blueprints=len(data.blueprints),
            errors=data.errors,
            built_at=data.built_at,
        )


__all__ = [
    "ENTRY_POINT_GROUP",
    "BlueprintCatalog",
    "build_catalog",
    "name_and_version",
    "parse_blueprints",
    "scan_sources",
    "to_snake_case",
]
