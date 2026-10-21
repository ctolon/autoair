"""Where operator classes come from, beyond what providers declare in ``provider.yaml``.

* :class:`ProviderPackagesSource` — every module of every installed provider distribution
  (official *and* third-party ``apache_airflow_provider`` packages), so operators in modules a
  provider forgot to declare — or community providers that declare none — are found.
* :class:`PathsSource` — custom operators in plain folders: Airflow's ``plugins_folder`` and
  ``dags_folder`` (both on ``sys.path`` in Airflow, so ``from my_ops.jira import X`` works in
  DAG files) plus any folder configured in ``AUTOAIR_CUSTOM_OPERATOR_PATHS``.
* :class:`PackagesSource` — every module of installed packages named in
  ``AUTOAIR_EXTRA_OPERATOR_PACKAGES`` (a company's internal operator library), or of the
  top-level packages of the pip distributions named in ``AUTOAIR_EXTRA_OPERATOR_DISTRIBUTIONS``
  (:func:`distribution_packages`).
* :class:`FilesSource` — single files and package folders named in
  ``AUTOAIR_EXTRA_OPERATOR_PATHS`` (their import root is inferred from ``__init__.py`` files).

What is scanned is filtered by the ``AUTOAIR_DISCOVERY__*`` settings
(:class:`~autoair.sections.DiscoverySettings`: skipped directories, module/class regexes,
excluded distributions, the file size cap…), applied with :func:`configure`.

Discovery is static: files are read and parsed, never imported here. A cheap text pre-filter
skips files without a class that mentions an operator or sensor. Custom roots are added to
``sys.path`` and ``PYTHONPATH`` so the import probes, the DagBag check and the task runner
(all subprocesses) can import the same modules the generated DAG files import.
"""

from __future__ import annotations

import contextlib
import fnmatch
import logging
import os
import re
import sys
import threading
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from importlib.metadata import Distribution, PackageNotFoundError, distribution, distributions
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import Field
from pydantic_settings import SettingsConfigDict

from autoair.catalog.models import OperatorCategory
from autoair.catalog.static import clear_locator_cache, find_module_file
from autoair.config_sources import AutoAirBaseSettings
from autoair.sections import (
    CatalogSettings,
    DiscoverySettings,
    any_search,
    compile_patterns,
)

if TYPE_CHECKING:
    from autoair.catalog.service import ModuleRef

log = logging.getLogger(__name__)

PROVIDER_ENTRY_POINT_GROUP = "apache_airflow_provider"  # Airflow's provider entry point
_CLASS_RE = re.compile(rb"^[ \t]*class[ \t]+\w+[ \t]*\(", re.M)  # Python syntax


def _norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()  # PEP 503 normalised distribution name


@dataclass(frozen=True)
class DiscoveryFilters:
    """Compiled :class:`~autoair.sections.DiscoverySettings`."""

    settings: DiscoverySettings
    skip_dirs: frozenset[str]
    skip_module: tuple[re.Pattern[str], ...]
    skip_class: tuple[re.Pattern[str], ...]
    hint: tuple[re.Pattern[bytes], ...]
    exclude_distributions: tuple[str, ...]

    @classmethod
    def from_settings(cls, s: DiscoverySettings) -> DiscoveryFilters:
        # An empty class_hint_pattern disables the pre-filter (every file defining a class is
        # parsed), like an empty skip_* pattern disables skipping.
        hints = (
            [p for p in (s.class_hint_pattern, *s.class_hint_patterns_extra) if p]
            if s.class_hint_pattern
            else []
        )
        return cls(
            settings=s,
            skip_dirs=s.skip_dir_names(),
            skip_module=compile_patterns(s.skip_module_pattern, s.skip_module_patterns_extra),
            skip_class=compile_patterns(s.skip_class_pattern, s.skip_class_patterns_extra),
            hint=tuple(re.compile(p.encode()) for p in hints),
            exclude_distributions=tuple(
                _norm(p) for p in (*s.exclude_distributions, *s.exclude_distributions_extra)
            ),
        )

    def skip_module_name(self, module: str) -> bool:
        pattern = next((p.pattern for p in self.skip_module if p.search(module)), None)
        if pattern is not None:
            note_skip("module", module, "discovery.skip_module_pattern", pattern)
        return pattern is not None

    def skip_class_name(self, name: str) -> bool:
        return any_search(self.skip_class, name)

    def excluded_distribution(self, name: str) -> bool:
        return any(fnmatch.fnmatchcase(_norm(name), p) for p in self.exclude_distributions)

    def excluded_distribution_pattern(self, name: str) -> str | None:
        return next(
            (p for p in self.exclude_distributions if fnmatch.fnmatchcase(_norm(name), p)), None
        )


# Skips noted while a catalog build enumerates its sources (catalog diagnostics).
_RECORDING = threading.local()


@contextlib.contextmanager
def recording_skips() -> Iterator[list[tuple[str, str, str, str | None]]]:
    """Collect ``(kind, name, reason, pattern)`` of what discovery skips in this thread."""
    previous = getattr(_RECORDING, "items", None)
    items: list[tuple[str, str, str, str | None]] = []
    _RECORDING.items = items
    try:
        yield items
    finally:
        _RECORDING.items = previous


def note_skip(kind: str, name: str, reason: str, pattern: str | None = None) -> None:
    items = getattr(_RECORDING, "items", None)
    if items is not None:
        items.append((kind, name, reason, pattern))


_FILTERS = DiscoveryFilters.from_settings(DiscoverySettings())
_APPLY_LOCK = threading.RLock()


def configure(settings: DiscoverySettings | None) -> DiscoveryFilters:
    """Apply discovery filters to this process (catalog sources and builds use them)."""
    global _FILTERS
    if settings is not None and settings != _FILTERS.settings:
        _FILTERS = DiscoveryFilters.from_settings(settings)
    return _FILTERS


def filters() -> DiscoveryFilters:
    return _FILTERS


def reset() -> None:
    """Back to the built-in discovery filters (tests)."""
    global _FILTERS
    _FILTERS = DiscoveryFilters.from_settings(DiscoverySettings())


@contextlib.contextmanager
def applied(f: DiscoveryFilters) -> Iterator[DiscoveryFilters]:
    """Use ``f`` while sources enumerate modules (a catalog built with its own filters)."""
    global _FILTERS
    previous = _FILTERS
    if f is previous:
        yield f
        return
    with _APPLY_LOCK:
        _FILTERS = f
        try:
            yield f
        finally:
            _FILTERS = previous


def maybe_task_file(path: Path) -> bool:
    """Cheap pre-filter: a class definition plus an operator/sensor/notifier mention."""
    f = _FILTERS
    try:
        if path.stat().st_size > f.settings.max_file_bytes:
            return False
        data = path.read_bytes()
    except OSError:
        return False
    if not _CLASS_RE.search(data):
        return False
    return not f.hint or any(h.search(data) for h in f.hint)


def category_of(module: str) -> OperatorCategory:
    """Best guess from the module path; sensors are re-detected from the class itself."""
    if ".sensors" in module or module.endswith("sensors") or "sensor" in module.rsplit(".", 1)[-1]:
        return "sensor"
    if ".transfers" in module:
        return "transfer"
    return "operator"


def module_name(root: Path, path: Path) -> str | None:
    rel = path.relative_to(root).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    if not parts or not all(p.isidentifier() for p in parts):
        return None
    return ".".join(parts)


def walk_python(root: Path) -> Iterator[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        skip = _FILTERS.skip_dirs
        dirnames[:] = sorted(d for d in dirnames if d not in skip and not d.startswith("."))
        for name in sorted(filenames):
            if name.endswith(".py"):
                yield Path(dirpath) / name


# --------------------------------------------------------------------------- search path

_REGISTERED: list[str] = []


def register_search_path(root: Path) -> None:
    """Make ``root`` an import root here and in every subprocess started afterwards."""
    path = str(root.resolve())
    if path in _REGISTERED:
        return
    _REGISTERED.append(path)
    if path not in sys.path:
        sys.path.append(path)  # appended: never shadows installed packages
    current = os.environ.get("PYTHONPATH", "")
    entries = [p for p in current.split(os.pathsep) if p]
    if path not in entries:
        os.environ["PYTHONPATH"] = os.pathsep.join([*entries, path])
    clear_locator_cache()
    # Warm DagBag workers started earlier lack the new root: restart them lazily.
    pool = sys.modules.get("autoair.validation.pool")
    if pool is not None:
        pool.shutdown_pool()


def registered_search_paths() -> list[str]:
    return list(_REGISTERED)


# --------------------------------------------------------------------------- sources


# PEP 508 requirement on Airflow itself (what makes a distribution an Airflow library).
_AIRFLOW_REQ = re.compile(r"^\s*apache-airflow(-core|-task-sdk)?\s*([\[<>=!~;(]|$)", re.I)


def depends_on_airflow(dist: Distribution) -> bool:
    """A hard (non-extra) requirement on Airflow: an operator library, provider or not."""
    for req in dist.requires or []:
        if "extra ==" in req:
            continue
        if _AIRFLOW_REQ.match(req):
            return True
    return False


def _provider_dists() -> Iterator[Distribution]:
    """Providers (``apache_airflow_provider`` entry point) and any other installed package that
    requires Airflow (e.g. airflow-dbt-python, airflow-clickhouse-plugin)."""
    for dist in distributions():
        name = _norm(dist.name or "")
        # Our own package and Airflow's distributions are never operator libraries.
        excluded = _FILTERS.excluded_distribution_pattern(name)
        if excluded is not None:
            note_skip("distribution", name, "discovery.exclude_distributions", excluded)
            continue
        if any(ep.group == PROVIDER_ENTRY_POINT_GROUP for ep in dist.entry_points) or (
            depends_on_airflow(dist)
        ):
            yield dist


def _dist_modules(dist: Distribution) -> list[str]:
    """Python modules shipped by a distribution (from its RECORD file)."""
    modules: set[str] = set()
    for f in dist.files or []:
        p = str(f)
        if not p.endswith(".py") or p.startswith(".."):
            continue
        mod = p[:-3].replace("/", ".").replace("\\", ".")
        if mod.endswith(".__init__"):
            mod = mod[: -len(".__init__")]
        if all(part.isidentifier() for part in mod.split(".")):
            modules.add(mod)
    return sorted(modules)


def provider_package_modules() -> dict[str, tuple[str, str, list[str]]]:
    """``package-name -> (distribution name, version, modules)`` for every provider dist."""
    out: dict[str, tuple[str, str, list[str]]] = {}
    for dist in _provider_dists():
        name = _norm(dist.name or "")
        out.setdefault(name, (name, dist.version, _dist_modules(dist)))
    return out


def _candidate_modules(modules: Iterable[str]) -> Iterator[str]:
    for mod in modules:
        if _FILTERS.skip_module_name(mod) or mod.endswith(".get_provider_info"):
            continue
        path = find_module_file(mod)
        if path is not None and maybe_task_file(path):
            yield mod


class PathsSource:
    """Custom operators in folders that are import roots (plugins/, dags/, include/…)."""

    def __init__(self, roots: Sequence[Path], provider: str = "custom") -> None:
        self.roots = [Path(r).expanduser() for r in roots if Path(r).expanduser().is_dir()]
        self.provider = provider
        for root in self.roots:
            register_search_path(root)

    def modules(self) -> Iterable[ModuleRef]:
        from autoair.catalog.service import ModuleRef

        seen: set[str] = set()
        for root in self.roots:
            label = f"{self.provider}:{root.name}"
            for path in walk_python(root):
                mod = module_name(root, path)
                if mod is None or mod in seen or _FILTERS.skip_module_name(mod):
                    continue
                if not maybe_task_file(path):
                    continue
                seen.add(mod)
                yield ModuleRef(mod, label, category_of(mod), None, "Custom operators")


class PackagesSource:
    """Every module of the named installed packages (e.g. ``acme_airflow``)."""

    def __init__(self, packages: Sequence[str], provider: str = "custom") -> None:
        self.packages = list(packages)
        self.provider = provider

    def modules(self) -> Iterable[ModuleRef]:
        from autoair.catalog.service import ModuleRef

        for package in self.packages:
            init = find_module_file(package)
            if init is None:
                log.warning("custom operator package %r not found", package)
                continue
            if init.name != "__init__.py":
                if maybe_task_file(init):
                    yield ModuleRef(package, f"{self.provider}:{package}", category_of(package))
                continue
            root = init.parent.parent
            for path in walk_python(init.parent):
                mod = module_name(root, path)
                if mod and not _FILTERS.skip_module_name(mod) and maybe_task_file(path):
                    yield ModuleRef(mod, f"{self.provider}:{package}", category_of(mod))


def distribution_packages(name: str) -> list[str]:
    """Top-level import packages of an installed pip distribution (``acme-airflow-ops`` ->
    ``["acme_ops"]``): ``top_level.txt`` when present, else derived from its file list."""
    try:
        dist = distribution(name)
    except PackageNotFoundError:
        return []
    return _top_level_packages(dist)


def _top_level_packages(dist: Distribution) -> list[str]:
    top = (dist.read_text("top_level.txt") or "").split()
    if top:
        return sorted({t.strip() for t in top if t.strip().isidentifier()})
    found = {m.split(".", 1)[0] for m in _dist_modules(dist)}
    return sorted(found or _editable_packages(dist))


def _editable_packages(dist: Distribution) -> set[str]:
    """Import names of an editable install, whose RECORD lists no ``.py`` module: the
    ``MAPPING`` of setuptools' ``__editable___*_finder.py`` or the packages in the folders
    named by its ``.pth`` file."""
    import ast

    names: set[str] = set()
    for f in dist.files or []:
        p = str(f)
        if p.startswith("..") or "/" in p or "\\" in p:
            continue
        try:
            text = Path(str(dist.locate_file(f))).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if p.startswith("__editable__") and p.endswith("_finder.py"):
            with contextlib.suppress(SyntaxError, ValueError):
                for node in ast.parse(text).body:
                    if isinstance(node, ast.Assign) and any(
                        isinstance(t, ast.Name) and t.id == "MAPPING" for t in node.targets
                    ):
                        mapping = ast.literal_eval(node.value)
                        if isinstance(mapping, dict):
                            names.update(k for k in mapping if str(k).isidentifier())
        elif p.endswith(".pth"):
            for line in text.splitlines():
                line = line.strip()
                if not line or line.startswith(("#", "import ")):
                    continue
                folder = Path(line)
                if not folder.is_dir():
                    continue
                for child in folder.iterdir():
                    if child.name.isidentifier() and (child / "__init__.py").is_file():
                        names.add(child.name)
                    elif child.suffix == ".py" and child.stem.isidentifier():
                        names.add(child.stem)
    return names


def _package_modules(package: str) -> list[str]:
    """Every module of an importable top-level package (found without importing it)."""
    init = find_module_file(package)
    if init is None:
        return []
    if init.name != "__init__.py":
        return [package]
    root = init.parent.parent
    if str(root.resolve()) not in {str(Path(p or ".").resolve()) for p in sys.path}:
        # Found by an import hook (setuptools editable finder): make its submodules (and
        # the probes' imports) resolvable by path.
        register_search_path(root)
    return [m for path in walk_python(init.parent) if (m := module_name(root, path))]


class DistributionsSource:
    """Every module shipped by the named pip distributions (``AUTOAIR_EXTRA_OPERATOR_
    DISTRIBUTIONS``), whether or not they declare an Airflow requirement or entry point."""

    def __init__(self, names: Sequence[str]) -> None:
        self.names = list(names)

    def modules(self) -> Iterable[ModuleRef]:
        from autoair.catalog.service import ModuleRef

        for name in self.names:
            try:
                dist = distribution(name)
            except PackageNotFoundError:
                log.warning("operator distribution %r is not installed", name)
                continue
            label = _norm(dist.name or name)
            modules = _dist_modules(dist)
            if not modules:
                # Editable install: RECORD lists a .pth file / finder, not the modules.
                packages = _top_level_packages(dist)
                log.info(
                    "operator distribution %r lists no Python modules (editable install?); "
                    "scanning its import packages %s",
                    name,
                    ", ".join(packages) or "(none found)",
                )
                modules = sorted({m for p in packages for m in _package_modules(p)})
            for module in _candidate_modules(modules):
                yield ModuleRef(module, label, category_of(module), dist.version)


def _import_root(path: Path) -> Path:
    """The folder that must be on ``sys.path`` to import ``path`` (above its packages)."""
    current = path if path.is_dir() else path.parent
    while (current / "__init__.py").is_file() and current.parent != current:
        current = current.parent
    return current


class FilesSource:
    """Single operator files and package folders (``AUTOAIR_EXTRA_OPERATOR_PATHS``).

    ``/opt/acme/acme_ops/jira.py`` (``acme_ops`` has an ``__init__.py``) is imported as
    ``acme_ops.jira`` with ``/opt/acme`` registered as import root; a folder without
    ``__init__.py`` is itself an import root (like ``AUTOAIR_CUSTOM_OPERATOR_PATHS``).
    """

    def __init__(self, paths: Sequence[Path], provider: str = "custom") -> None:
        self.paths = [Path(p).expanduser().resolve() for p in paths]
        self.provider = provider
        for path in self.paths:
            if path.exists():
                register_search_path(_import_root(path))
            else:
                log.warning("custom operator path %s does not exist", path)

    def modules(self) -> Iterable[ModuleRef]:
        from autoair.catalog.service import ModuleRef

        seen: set[str] = set()
        for path in self.paths:
            if not path.exists():
                continue
            root = _import_root(path)
            files = [path] if path.is_file() else list(walk_python(path))
            label = f"{self.provider}:{path.stem if path.is_file() else path.name}"
            for file in files:
                if file.suffix != ".py":
                    continue
                mod = module_name(root, file)
                if mod is None or mod in seen or _FILTERS.skip_module_name(mod):
                    continue
                if not maybe_task_file(file):
                    continue
                seen.add(mod)
                yield ModuleRef(mod, label, category_of(mod), None, "Custom operators")


def airflow_folders() -> list[Path]:
    """Airflow's plugins and DAG folders (import roots inside Airflow), when they exist.

    Read without importing Airflow (the static catalog must not): environment overrides, then
    ``$AIRFLOW_HOME/airflow.cfg``, then Airflow's defaults. Inside Airflow (already imported)
    its configuration object is used directly.
    """
    values: dict[str, str | None] = {}
    conf_mod = sys.modules.get("airflow.configuration")
    conf = getattr(conf_mod, "conf", None)
    home = Path(os.environ.get("AIRFLOW_HOME", "~/airflow")).expanduser()
    if conf is not None:
        for key in ("plugins_folder", "dags_folder"):
            try:
                values[key] = conf.get("core", key, fallback=None)
            except Exception:
                values[key] = None
    else:
        import configparser

        cfg = configparser.ConfigParser(interpolation=None)
        with contextlib.suppress(configparser.Error):
            cfg.read(home / "airflow.cfg")
        for key, default in (("plugins_folder", "plugins"), ("dags_folder", "dags")):
            value = os.environ.get(f"AIRFLOW__CORE__{key.upper()}") or cfg.get(
                "core", key, fallback=None
            )
            values[key] = (value or str(home / default)).replace("{AIRFLOW_HOME}", str(home))
    out: list[Path] = []
    for value in values.values():
        if value and Path(value).expanduser().is_dir():
            out.append(Path(value).expanduser())
    return out


class CatalogSourceSettings(AutoAirBaseSettings):
    """The ``AUTOAIR_*`` settings that select catalog sources (no Airflow-dependent defaults,
    so building a catalog never imports Airflow). Reads the configuration files too."""

    model_config = SettingsConfigDict(
        env_prefix="AUTOAIR_", env_file=".env", extra="ignore", env_nested_delimiter="__"
    )

    extra_operator_modules: list[str] = Field(default_factory=list)
    extra_operator_packages: list[str] = Field(default_factory=list)
    extra_operator_distributions: list[str] = Field(default_factory=list)
    extra_operator_paths: list[Path] = Field(default_factory=list)
    custom_operator_paths: list[Path] = Field(default_factory=list)
    discover_airflow_folders: bool = True
    scan_provider_packages: bool = True
    discovery: DiscoverySettings = Field(default_factory=DiscoverySettings)
    catalog: CatalogSettings = Field(default_factory=CatalogSettings)
    operator_allow: list[str] = Field(default_factory=lambda: ["*"])
    operator_deny: list[str] = Field(default_factory=list)


__all__ = [
    "CatalogSourceSettings",
    "DiscoveryFilters",
    "DistributionsSource",
    "FilesSource",
    "PackagesSource",
    "PathsSource",
    "airflow_folders",
    "applied",
    "category_of",
    "configure",
    "distribution_packages",
    "filters",
    "maybe_task_file",
    "note_skip",
    "provider_package_modules",
    "recording_skips",
    "register_search_path",
    "reset",
]
