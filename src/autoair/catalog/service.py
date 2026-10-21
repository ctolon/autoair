"""Operator catalog: discovers operators from installed providers (and extensions).

Discovery is pluggable through :class:`CatalogSource` objects. Built-in sources:

* :class:`ProvidersSource` — every ``operators`` / ``sensors`` / ``transfers`` module
  declared by installed Airflow providers (read statically from provider metadata).
* :class:`ModulesSource` — explicitly configured modules (e.g. in-house operators).

Extra sources can be registered via the ``autoair.catalog_sources`` entry point group.
The catalog doubles as the **import allowlist** for code generation.

Strategies
----------
``strategy="static"`` (default) never imports provider code in this process: modules
are parsed with :mod:`ast` (:mod:`autoair.catalog.static`); classes that cannot be
resolved statically are introspected by importing them **in a subprocess**.
``strategy="import"`` keeps the historical in-process import + ``inspect`` approach.

Builds are cached on disk (JSON) keyed by the installed ``apache-airflow*``
distributions, the AutoAir version, a fingerprint of the catalog-building code
(:func:`logic_fingerprint`), the on-disk layout (:func:`layout_version`, a hash of the
:class:`CatalogData` JSON schema), the discovery filters, the strategy and custom sources, so
only the very first start pays the build cost and neither a logic nor a layout change can
ever serve a stale cache — there is no version number to bump by hand.
:func:`build_catalog_file` / :func:`load_catalog_file` precompute a catalog (e.g. at Docker
build time).
"""

from __future__ import annotations

import fnmatch
import functools
import hashlib
import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field, TypeAdapter, ValidationError

from autoair import __version__
from autoair._subprocess import user_code_env
from autoair.catalog.discovery import DiscoveryFilters
from autoair.catalog.discovery import applied as discovery_applied
from autoair.catalog.discovery import filters as discovery_filters
from autoair.catalog.introspection import ParamInfo
from autoair.catalog.metadata import (
    Presentation,
    apply_package_metadata,
    entry_point_layers,
    file_layers,
)
from autoair.catalog.models import (
    CatalogBuildDiagnostics,
    CatalogDiagnostics,
    CatalogError,
    CatalogFiltered,
    CatalogSourceInfo,
    CatalogStats,
    CatalogStrategy,
    ConnectionTypeInfo,
    NotifierDetail,
    NotifierSummary,
    OperatorCategory,
    OperatorDetail,
    OperatorSummary,
    QueueScheme,
    TaskDecoratorInfo,
)
from autoair.catalog.providers import ProviderIndex, airflow_distributions, load_provider_index
from autoair.catalog.schema_check import validate_value
from autoair.catalog.static import (
    PYTHON_REF,
    ClassInfo,
    ModuleInfo,
    StaticAnalyzer,
    Unresolvable,
    annotation_schema,
    callable_docstring,
    candidate_classes,
    clear_locator_cache,
    find_module_file,
    looks_like_task_class,
    parse_module,
)
from autoair.sections import CatalogSettings

if TYPE_CHECKING:
    from autoair.blueprints.catalog import BlueprintCatalog

log = logging.getLogger(__name__)

SOURCE_ENTRY_POINT_GROUP = "autoair.catalog_sources"
# Operators whose required callable the builder cannot express (yet), mapped to the TaskFlow
# node that does the same thing (the function body is written in the builder instead).
TASKFLOW_EQUIVALENTS: dict[str, str] = {
    "PythonOperator": "task",
    "BranchPythonOperator": "branch",
    "ShortCircuitOperator": "short_circuit",
    "PythonVirtualenvOperator": "virtualenv",
    "BranchPythonVirtualenvOperator": "branch_virtualenv",
    "ExternalPythonOperator": "external_python",
    "BranchExternalPythonOperator": "branch_external_python",
    "PythonSensor": "sensor",
}


def _first_sentence(doc: str | None) -> str | None:
    """Summary line of a docstring (first sentence of its first paragraph)."""
    if not doc:
        return None
    para = doc.strip().split("\n\n", 1)[0].replace("\n", " ").strip()
    end = re.search(r"(?<=[.!?])\s", para)
    return (para[: end.start()] if end else para)[:200] or None


@dataclass(frozen=True)
class ModuleRef:
    module: str
    provider: str
    category: OperatorCategory
    provider_version: str | None = None
    integration: str | None = None


@runtime_checkable
class CatalogSource(Protocol):
    def modules(self) -> Iterable[ModuleRef]: ...


class ProvidersSource:
    """Every installed provider's operators: the modules declared in its metadata
    (``provider.yaml``, with integration names) plus every other module of the distribution
    that defines a task class (``scan_packages``) — undeclared modules and third-party
    providers that declare nothing are covered too."""

    def __init__(self, index: ProviderIndex | None = None, *, scan_packages: bool = True) -> None:
        self._index = index
        self.scan_packages = scan_packages

    def modules(self) -> Iterable[ModuleRef]:
        index = self._index or load_provider_index()
        declared: set[str] = set()
        for ref in self._declared(index):
            declared.add(ref.module)
            yield ref
        if not self.scan_packages:
            return
        from autoair.catalog.discovery import _candidate_modules, category_of
        from autoair.catalog.discovery import provider_package_modules as package_modules

        versions = {package: info.version for package, info in index.items()}
        for package, (_dist, version, modules) in sorted(package_modules().items()):
            extra = [m for m in modules if m not in declared]
            for module in _candidate_modules(extra):
                version_ = versions.get(package, version)
                yield ModuleRef(module, package, category_of(module), version_)

    def _declared(self, index: ProviderIndex) -> Iterable[ModuleRef]:
        for package, info in index.items():
            data: dict[str, Any] = info.data
            pairs: tuple[tuple[str, OperatorCategory], ...] = (
                ("operators", "operator"),
                ("sensors", "sensor"),
            )
            for key, category in pairs:
                for entry in data.get(key, []) or []:
                    for module in entry.get("python-modules", []) or []:
                        yield ModuleRef(
                            module, package, category, info.version, entry.get("integration-name")
                        )
            for entry in data.get("transfers", []) or []:
                module = entry.get("python-module")
                if module:
                    integ = (
                        f"{entry.get('source-integration-name')} → "
                        f"{entry.get('target-integration-name')}"
                    )
                    yield ModuleRef(module, package, "transfer", info.version, integ)


class ModulesSource:
    def __init__(self, modules: Sequence[str], provider: str = "custom") -> None:
        self._modules = modules
        self._provider = provider

    def modules(self) -> Iterable[ModuleRef]:
        for m in self._modules:
            yield ModuleRef(m, self._provider, "operator")


def configured_sources(settings: Any = None) -> list[CatalogSource]:
    """Catalog sources from settings: providers (whole packages), custom folders (Airflow's
    plugins/DAG folders + configured paths), custom files, packages, pip distributions and
    modules, entry points. Also applies the discovery filters (``settings.discovery``)."""
    from autoair.catalog.discovery import (
        CatalogSourceSettings,
        DistributionsSource,
        FilesSource,
        PackagesSource,
        PathsSource,
        airflow_folders,
        configure,
    )

    if settings is None:
        try:
            settings = CatalogSourceSettings()
        except Exception:  # invalid environment: providers only
            log.warning("cannot read catalog settings; scanning providers only", exc_info=True)
            return [ProvidersSource(), *entry_point_sources()]

    configure(getattr(settings, "discovery", None))
    sources: list[CatalogSource] = [ProvidersSource(scan_packages=settings.scan_provider_packages)]
    roots = list(settings.custom_operator_paths)
    if settings.discover_airflow_folders:
        roots += [p for p in airflow_folders() if p not in roots]
    if roots:
        sources.append(PathsSource(roots))
    paths = list(getattr(settings, "extra_operator_paths", []) or [])
    if paths:
        sources.append(FilesSource(paths))
    if settings.extra_operator_packages:
        sources.append(PackagesSource(settings.extra_operator_packages))
    distributions = list(getattr(settings, "extra_operator_distributions", []) or [])
    if distributions:
        sources.append(DistributionsSource(distributions))
    if settings.extra_operator_modules:
        sources.append(ModulesSource(settings.extra_operator_modules))
    return [*sources, *entry_point_sources()]


def entry_point_sources() -> list[CatalogSource]:
    sources: list[CatalogSource] = []
    for ep in entry_points(group=SOURCE_ENTRY_POINT_GROUP):
        try:
            obj = ep.load()
            sources.append(obj() if isinstance(obj, type) else obj)
        except Exception:
            log.exception("failed to load catalog source %r", ep.name)
    return sources


def _logic_files() -> list[tuple[str, Callable[[], bytes]]]:
    """Every source file whose content shapes what a catalog build produces."""
    from importlib import resources

    package_dir = Path(__file__).resolve().parent
    files: list[tuple[str, Callable[[], bytes]]] = [
        (f"autoair/catalog/{p.name}", p.read_bytes) for p in sorted(package_dir.glob("*.py"))
    ]
    icons = package_dir.parent / "icons.py"
    files.append(("autoair/icons.py", icons.read_bytes))
    manifest = resources.files("autoair.resources").joinpath("logos/manifest.json")
    files.append(("autoair/resources/logos/manifest.json", manifest.read_bytes))
    return files


@functools.cache
def logic_fingerprint() -> str:
    """Hash of the catalog-building code (``autoair/catalog/*.py``, ``icons.py``, the logo
    manifest): part of the cache key, so any logic change invalidates cached and
    precomputed catalogs without a manual version bump. Computed once per process."""
    digest = hashlib.sha256()
    for name, read in _logic_files():
        try:
            data = read()
        except OSError:  # e.g. a zip-imported install without sources
            data = b"<unavailable>"
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(data).digest())
    return digest.hexdigest()[:32]


@functools.cache
def layout_version() -> str:
    """Hash of the :class:`CatalogData` JSON schema: the on-disk layout version. Any change
    to the cached models changes it, so old cache / precomputed files are ignored."""
    schema = json.dumps(CatalogData.model_json_schema(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(schema.encode()).hexdigest()[:16]


def default_cache_dir() -> Path:
    env = os.environ.get("AUTOAIR_CACHE_DIR")
    if env:
        return Path(env).expanduser()
    xdg = os.environ.get("XDG_CACHE_HOME")
    return (Path(xdg).expanduser() if xdg else Path.home() / ".cache") / "autoair"


# --------------------------------------------------------------------------- data


class CatalogData(BaseModel):
    """Everything a catalog build produces (the on-disk cache format)."""

    format: str = Field(
        default_factory=lambda: layout_version(), description="layout_version() of the writer"
    )
    key: str
    key_parts: dict[str, Any]
    strategy: CatalogStrategy
    built_at: float
    build_seconds: float
    operators: list[OperatorDetail] = []
    notifiers: list[NotifierDetail] = []
    decorators: list[TaskDecoratorInfo] = []
    queue_schemes: list[QueueScheme] = []
    connection_types: list[ConnectionTypeInfo] = []
    errors: list[CatalogError] = []
    diagnostics: CatalogBuildDiagnostics = Field(default_factory=CatalogBuildDiagnostics)
    package_icons: dict[str, str] = Field(
        default_factory=dict, description="Sanitised SVG icons shipped by operator packages."
    )


@dataclass
class _Entry:
    detail: OperatorDetail
    params: dict[str, ParamInfo]


@dataclass
class _NotifierEntry:
    detail: NotifierDetail
    params: dict[str, ParamInfo]


@dataclass
class _State:
    data: CatalogData
    operators: dict[str, _Entry]
    notifiers: dict[str, _NotifierEntry]
    loaded_from_cache: bool = False
    filtered: list[CatalogFiltered] = field(default_factory=list)
    loaded_at: float = field(default_factory=time.time)


def _params_from_schema(
    schema: dict[str, Any], adapters: dict[str, TypeAdapter[Any]] | None = None
) -> dict[str, ParamInfo]:
    required = set(schema.get("required") or [])
    out: dict[str, ParamInfo] = {}
    for name, prop in (schema.get("properties") or {}).items():
        # "required" python-ref params can only be a Python reference (no JSON value).
        supported = prop.get(PYTHON_REF) != "required" and "x-autoair-unsupported" not in prop
        out[name] = ParamInfo(
            name=name,
            schema=prop,
            required=name in required,  # the schema lists every param without a default
            adapter=(adapters or {}).get(name),
            supported=supported,
        )
    return out


# --------------------------------------------------------------------------- build


@dataclass
class _ConnContext:
    hook_types: dict[str, str] = field(default_factory=dict)  # hook fqn -> conn_type
    default_names: dict[str, str] = field(default_factory=dict)  # default conn id -> type
    known: set[str] = field(default_factory=set)


KNOWN_QUEUE_SCHEMES: tuple[tuple[str, str], ...] = (
    ("sqs", "apache-airflow-providers-amazon"),
    ("kinesis", "apache-airflow-providers-amazon"),
    ("kafka", "apache-airflow-providers-apache-kafka"),
    ("redis+pubsub", "apache-airflow-providers-redis"),
    ("azure+servicebus", "apache-airflow-providers-microsoft-azure"),
    ("google+pubsub", "apache-airflow-providers-google"),
    ("ibmmq", "apache-airflow-providers-ibm-mq"),
)
_TRIGGER_SKIP = {"self", "args", "kwargs", "scheme", "queue"}


def _parse_job(name: str) -> ModuleInfo:
    return parse_module(name)


class _Builder:
    def __init__(
        self,
        sources: Sequence[CatalogSource],
        index: ProviderIndex,
        strategy: CatalogStrategy,
        subprocess_fallback: bool,
        workers: int | None,
        fallback_timeout: float,
        filters: DiscoveryFilters | None = None,
        presentation: CatalogSettings | None = None,
    ) -> None:
        self.filters = filters if filters is not None else discovery_filters()
        self.presentation = presentation if presentation is not None else CatalogSettings()
        self.conn_patterns: list[tuple[re.Pattern[str], str | None]] = [
            (re.compile(p.pattern), p.conn_type) for p in self.presentation.connection_patterns()
        ]
        self.filtered: list[CatalogFiltered] = []
        self.source_infos: list[CatalogSourceInfo] = []
        self.module_source: dict[str, list[int]] = {}  # module -> sources yielding it
        self.sources = sources
        self.index = index
        self.strategy = strategy
        self.subprocess_fallback = subprocess_fallback
        self.workers = workers
        self.fallback_timeout = fallback_timeout
        self.analyzer = StaticAnalyzer()
        self.errors: list[CatalogError] = []
        self.adapters: dict[str, dict[str, TypeAdapter[Any]]] = {}
        self.conn = _ConnContext()

    def err(self, module: str, provider: str, error: str) -> None:
        self.errors.append(CatalogError(module=module, provider=provider, error=error))

    def requires(self, provider: str) -> str | None:
        info = self.index.providers.get(provider)
        return info.requires_airflow if info else None

    # ---------------------------------------------------------------- parse
    def preparse(self, modules: Sequence[str]) -> None:
        names = sorted(set(modules))
        options = self.filters.settings
        workers = self.workers if self.workers is not None else options.workers
        if workers is None:
            workers = min(4, os.cpu_count() or 1)
        if len(names) >= options.parallel_threshold and workers > 1:
            import multiprocessing

            ctx = multiprocessing.get_context("spawn")
            try:
                with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as pool:
                    for info in pool.map(_parse_job, names, chunksize=32):
                        self.analyzer.add(info)  # records parse errors
                return
            except Exception:  # pragma: no cover - fall back to serial parsing
                log.warning("parallel catalog parsing failed; parsing serially", exc_info=True)
        for name in names:
            self.analyzer.module(name)

    # ---------------------------------------------------------------- connections
    def connection_types(self) -> list[ConnectionTypeInfo]:
        out: list[ConnectionTypeInfo] = []
        for package, info in self.index.items():
            for entry in info.data.get("connection-types", []) or []:
                conn_type = entry.get("connection-type")
                hook = entry.get("hook-class-name")
                if not conn_type:
                    continue
                default_name: str | None = None
                if hook:
                    self.conn.hook_types[hook] = conn_type
                    target = self.analyzer.resolve_fqn(hook)
                    if isinstance(target, ClassInfo):
                        _, default_name = self.analyzer.hook_conn_type(target)
                if default_name:
                    self.conn.default_names.setdefault(default_name, conn_type)
                self.conn.known.add(conn_type)
                out.append(
                    ConnectionTypeInfo(
                        conn_type=conn_type,
                        hook_class=hook,
                        hook_name=entry.get("hook-name"),
                        provider=package,
                        default_conn_name=default_name,
                    )
                )
        return sorted(out, key=lambda c: (c.conn_type, c.provider))

    def hook_types(self, hooks: Iterable[ClassInfo]) -> list[str]:
        out: list[str] = []
        for h in hooks:
            ct = self.conn.hook_types.get(f"{h.module}.{h.name}")
            if ct is None:
                ct, _ = self.analyzer.hook_conn_type(h)
            if ct and ct not in out:
                out.append(ct)
        return out

    def connection_match(self, name: str) -> tuple[re.Match[str], str | None] | None:
        """The first ``catalog.connection_param_patterns`` match of a param name."""
        for pattern, conn_type in self.conn_patterns:
            m = pattern.search(name)
            if m:
                return m, conn_type
        return None

    def assign_conn_types(self, params: list[dict[str, Any]], hook_types: list[str]) -> list[str]:
        # Connection params follow catalog.connection_param_patterns (the introspection's
        # built-in *_conn_id tagging is replaced).
        conn_params: list[tuple[dict[str, Any], str, str | None]] = []
        for p in params:
            schema_: dict[str, Any] = p["schema"]
            found = self.connection_match(p["name"])
            if found is None:
                if schema_.get("x-autoair-widget") == "connection":
                    del schema_["x-autoair-widget"]
                continue
            m, explicit = found
            schema_["x-autoair-widget"] = "connection"
            conn_params.append((p, p["name"][: m.start()].rstrip("_"), explicit))
        all_types: list[str] = []
        for p, prefix, explicit in conn_params:
            schema: dict[str, Any] = p["schema"]
            types: list[str] = [explicit] if explicit else []
            default = schema.get("default")
            if not types and isinstance(default, str):
                # "spark_default" is the default of several hooks (spark, spark_jdbc…): a
                # connection type named like the default wins over the first hook declaring it.
                if default.endswith("_default") and default[:-8] in self.conn.known:
                    types = [default[:-8]]
                elif default in self.conn.default_names:
                    types = [self.conn.default_names[default]]
            if not types and prefix in self.conn.known:
                types = [prefix]
            if not types and hook_types:
                if len(conn_params) == 1:
                    types = list(hook_types)
                elif prefix:
                    types = [t for t in hook_types if t.startswith(prefix) or prefix in t]
            if types:
                schema["x-autoair-conn-type"] = types
                all_types += [t for t in types if t not in all_types]
        return all_types

    # ---------------------------------------------------------------- records
    def static_record(self, cls: ClassInfo) -> tuple[dict[str, Any], list[str]]:
        r = self.analyzer.analyze(cls)
        record = {
            "name": cls.name,
            "qualname": cls.name,
            "module": cls.module,
            "params": [
                {"name": n, "schema": p.schema, "required": p.required, "supported": p.supported}
                for n, p in r.params.items()
            ],
            "template_fields": r.template_fields,
            "docstring": r.docstring,
            "summary": r.summary,
            "ui_color": r.ui_color,
            "is_sensor": r.is_sensor,
            "deprecated": cls.deprecated or "deprecated" in (cls.doc or "").lower()[:400],
        }
        return record, self.hook_types(r.hook_refs)

    def own_hook_types(self, cls: ClassInfo | None) -> list[str]:
        if cls is None:
            return []
        hooks = [self.analyzer.resolve(cls.module, ref) for ref in cls.hook_refs]
        return self.hook_types(h for h in hooks if isinstance(h, ClassInfo))

    @staticmethod
    def schema_of(record: dict[str, Any]) -> tuple[dict[str, Any], list[str], bool]:
        """(JSON schema, Python-reference params, a required param needs a reference)."""
        props = {p["name"]: p["schema"] for p in record["params"]}
        required = [p["name"] for p in record["params"] if p["required"]]
        refs = [n for n, sch in props.items() if sch.get(PYTHON_REF)]
        needs_code = any(p["required"] and not p["supported"] for p in record["params"])
        schema: dict[str, Any] = {
            "type": "object",
            "title": record["name"],
            "properties": props,
            "additionalProperties": False,
        }
        if required:
            schema["required"] = required
        return schema, refs, needs_code

    def operator_detail(
        self, record: dict[str, Any], ref: ModuleRef, hook_types: list[str]
    ) -> OperatorDetail:
        conn_types = self.assign_conn_types(record["params"], hook_types)
        schema, refs, needs_code = self.schema_of(record)
        category: OperatorCategory = "sensor" if record["is_sensor"] else ref.category
        name: str = record["name"]
        return OperatorDetail(
            fqn=f"{record['module']}.{record['qualname']}",
            class_name=name,
            module=record["module"],
            provider=ref.provider,
            provider_version=ref.provider_version,
            integration=ref.integration,
            category=category,
            summary=record["summary"],
            ui_color=record["ui_color"],
            deprecated=record["deprecated"],
            python_ref_params=refs,
            requires_python_code=needs_code,
            equivalent_decorator=(
                TASKFLOW_EQUIVALENTS.get(name)
                if record["module"].startswith("airflow.providers.standard.")
                else None
            ),
            params_schema=schema,
            template_fields=record["template_fields"],
            docstring=record["docstring"],
            connection_types=conn_types,
            is_sensor=record["is_sensor"],
            deferrable_param="deferrable" in schema["properties"],
            hitl=name.startswith("HITL")
            or name == "ApprovalOperator"
            or ".hitl" in record["module"],
            required_airflow=self.requires(ref.provider),
        )

    def notifier_detail(
        self, record: dict[str, Any], provider: str, version: str | None, hook_types: list[str]
    ) -> NotifierDetail:
        conn_types = self.assign_conn_types(record["params"], hook_types)
        schema, refs, needs_code = self.schema_of(record)
        return NotifierDetail(
            fqn=f"{record['module']}.{record['qualname']}",
            class_name=record["name"],
            module=record["module"],
            provider=provider,
            provider_version=version,
            summary=record["summary"],
            deprecated=record["deprecated"],
            python_ref_params=refs,
            requires_python_code=needs_code,
            params_schema=schema,
            template_fields=record["template_fields"],
            docstring=record["docstring"],
            connection_types=conn_types,
        )

    # ---------------------------------------------------------------- operators
    def refs(self) -> list[ModuleRef]:
        from autoair.catalog.discovery import recording_skips

        refs: list[ModuleRef] = []
        for i, source in enumerate(self.sources):
            info = CatalogSourceInfo(source=type(source).__name__, detail=_source_detail(source))
            self.source_infos.append(info)
            with recording_skips() as skipped:
                try:
                    found = list(source.modules())
                except Exception as exc:
                    self.err(type(source).__name__, "?", repr(exc))
                    info.error = repr(exc)
                    found = []
            for kind, name, reason, pattern in skipped:
                self.filtered.append(
                    CatalogFiltered(name=name, kind=kind, reason=reason, detail=pattern)
                )
            info.modules = len(found)
            info.providers = sorted({r.provider for r in found})
            for ref in found:
                indexes = self.module_source.setdefault(ref.module, [])
                if i not in indexes:
                    indexes.append(i)
            refs += found
        return refs

    def operators(self, refs: list[ModuleRef]) -> list[OperatorDetail]:
        if self.strategy == "import":
            return self.operators_import(refs)
        details: dict[str, OperatorDetail] = {}
        fallback: dict[str, tuple[ModuleRef, list[str] | None]] = {}
        static_classes: dict[str, ClassInfo] = {}
        for ref in refs:
            mi = self.analyzer.module(ref.module)
            if mi is None:
                fallback.setdefault(ref.module, (ref, None))
                continue
            for cls in candidate_classes(mi):
                fqn = f"{cls.module}.{cls.name}"
                if fqn in details:
                    continue
                if self.filters.skip_class_name(cls.name):
                    pattern = next(
                        (p.pattern for p in self.filters.skip_class if p.search(cls.name)), None
                    )
                    self.filtered.append(
                        CatalogFiltered(
                            name=fqn,
                            kind="operator",
                            reason="discovery.skip_class_pattern",
                            detail=pattern,
                        )
                    )
                    continue
                try:
                    kind = self.analyzer.classify(cls)
                    if kind != "operator":
                        continue
                    record, hooks = self.static_record(cls)
                except Unresolvable:
                    if looks_like_task_class(cls):
                        _ref, only = fallback.setdefault(ref.module, (ref, []))
                        if only is not None:
                            only.append(cls.name)
                        static_classes[fqn] = cls
                    continue
                except Exception as exc:
                    self.err(fqn, ref.provider, f"static analysis failed: {exc!r}")
                    continue
                details[fqn] = self.operator_detail(record, ref, hooks)
        for detail in self.run_fallback(fallback, "operator", static_classes, details):
            details.setdefault(detail.fqn, detail)
        # Static analysis cannot see module-level guards (e.g. "requires Airflow 3.1"):
        # drop operators whose module does not import in this environment.
        broken = probe_imports(
            sorted({ref.module for ref in refs}),
            timeout=self.filters.settings.probe_timeout,
        )
        for module, error in broken.items():
            provider = next((r.provider for r in refs if r.module == module), "?")
            self.err(module, provider, error)
        return [d for d in details.values() if d.module not in broken]

    def run_fallback(
        self,
        fallback: dict[str, tuple[ModuleRef, list[str] | None]],
        kind: str,
        static_classes: dict[str, ClassInfo],
        existing: dict[str, OperatorDetail],
    ) -> list[OperatorDetail]:
        if not fallback:
            return []
        if not self.subprocess_fallback:
            for module, (ref, only) in fallback.items():
                what = ", ".join(only) if only else "module"
                self.err(module, ref.provider, f"static analysis could not resolve {what}")
            return []
        from autoair.catalog.importer import introspect_in_subprocess

        requests = [
            {"module": m, "kind": kind, "only": only} for m, (_ref, only) in fallback.items()
        ]
        results, errors = introspect_in_subprocess(requests, timeout=self.fallback_timeout)
        out: list[OperatorDetail] = []
        for module, (ref, _only) in fallback.items():
            if module in errors:
                self.err(module, ref.provider, errors[module])
            for record in results.get(module, []):
                fqn = f"{record['module']}.{record['qualname']}"
                if fqn in existing:
                    continue
                hooks = self.own_hook_types(static_classes.get(fqn))
                out.append(self.operator_detail(record, ref, hooks))
        return out

    def operators_import(self, refs: list[ModuleRef]) -> list[OperatorDetail]:
        from autoair.catalog.importer import introspect_module

        details: dict[str, OperatorDetail] = {}
        for ref in refs:
            try:
                recs = introspect_module(ref.module, "operator")
            except Exception as exc:  # missing optional deps are common
                self.err(ref.module, ref.provider, f"{type(exc).__name__}: {exc}")
                continue
            mi = self.analyzer.module(ref.module)
            for record, intro in recs:
                fqn = f"{record['module']}.{record['qualname']}"
                if fqn in details:
                    continue
                cls = mi.classes.get(record["name"]) if mi else None
                details[fqn] = self.operator_detail(record, ref, self.own_hook_types(cls))
                self.adapters[fqn] = {
                    n: p.adapter for n, p in intro.params.items() if p.adapter is not None
                }
        return list(details.values())

    # ---------------------------------------------------------------- notifiers
    def notifiers(self) -> list[NotifierDetail]:
        out: dict[str, NotifierDetail] = {}
        fallback: dict[str, tuple[str, str | None, list[str]]] = {}
        static_classes: dict[str, ClassInfo] = {}
        for package, info in self.index.items():
            for path in info.data.get("notifications", []) or []:
                module, _, name = str(path).rpartition(".")
                if self.strategy == "import":
                    fallback.setdefault(module, (package, info.version, []))[2].append(name)
                    continue
                target = self.analyzer.resolve_fqn(path)
                if not isinstance(target, ClassInfo):
                    fallback.setdefault(module, (package, info.version, []))[2].append(name)
                    continue
                try:
                    if self.analyzer.classify(target) != "notifier":
                        self.err(path, package, "not a BaseNotifier subclass")
                        continue
                    record, hooks = self.static_record(target)
                except Unresolvable:
                    static_classes[path] = target
                    fallback.setdefault(module, (package, info.version, []))[2].append(name)
                    continue
                except Exception as exc:
                    self.err(path, package, f"static analysis failed: {exc!r}")
                    continue
                out[path] = self.notifier_detail(record, package, info.version, hooks)
        if fallback:
            out.update(self.notifiers_fallback(fallback, static_classes))
        return sorted(out.values(), key=lambda n: n.fqn)

    def notifiers_fallback(
        self,
        fallback: dict[str, tuple[str, str | None, list[str]]],
        static_classes: dict[str, ClassInfo],
    ) -> dict[str, NotifierDetail]:
        out: dict[str, NotifierDetail] = {}
        results: dict[str, list[dict[str, Any]]] = {}
        errors: dict[str, str] = {}
        if self.strategy == "import":
            from autoair.catalog.importer import introspect_module

            for module, (_p, _v, only) in fallback.items():
                try:
                    recs = introspect_module(module, "notifier", only)
                except Exception as exc:
                    errors[module] = f"{type(exc).__name__}: {exc}"
                    continue
                results[module] = [r for r, _ in recs]
                for r, intro in recs:
                    self.adapters[f"{r['module']}.{r['qualname']}"] = {
                        n: p.adapter for n, p in intro.params.items() if p.adapter is not None
                    }
        elif self.subprocess_fallback:
            from autoair.catalog.importer import introspect_in_subprocess

            reqs = [
                {"module": m, "kind": "notifier", "only": only}
                for m, (_p, _v, only) in fallback.items()
            ]
            results, errors = introspect_in_subprocess(reqs, timeout=self.fallback_timeout)
        else:
            errors = {m: "static analysis could not resolve notifier" for m in fallback}
        for module, (package, version, _only) in fallback.items():
            if module in errors:
                self.err(module, package, errors[module])
            for record in results.get(module, []):
                fqn = f"{record['module']}.{record['qualname']}"
                cls = static_classes.get(fqn)
                if cls is None:
                    mi = self.analyzer.module(module)
                    cls = mi.classes.get(record["name"]) if mi else None
                hooks = self.own_hook_types(cls)
                out[fqn] = self.notifier_detail(record, package, version, hooks)
        return out

    # ---------------------------------------------------------------- misc
    def decorators(self) -> list[TaskDecoratorInfo]:
        out: list[TaskDecoratorInfo] = []
        for package, info in self.index.items():
            for entry in info.data.get("task-decorators", []) or []:
                if entry.get("name") and entry.get("class-name"):
                    doc = callable_docstring(str(entry["class-name"]))
                    out.append(
                        TaskDecoratorInfo(
                            name=entry["name"],
                            class_name=entry["class-name"],
                            provider=package,
                            provider_version=info.version,
                            docstring=doc,
                            summary=_first_sentence(doc),
                        )
                    )
        return sorted(out, key=lambda d: (d.name, d.provider))

    def queue_schemes(self) -> list[QueueScheme]:
        found: dict[str, QueueScheme] = {}
        messaging = "apache-airflow-providers-common-messaging" in self.index.providers
        for package, info in self.index.items():
            for path in info.data.get("queues", []) or []:
                target = self.analyzer.resolve_fqn(str(path))
                if not isinstance(target, ClassInfo):
                    continue
                scheme = self.analyzer.class_attr(target, "scheme")
                if not isinstance(scheme, str):
                    continue
                trigger_path, kwargs_schema = self.trigger_kwargs(target)
                found[scheme] = QueueScheme(
                    scheme=scheme,
                    provider=package,
                    available=messaging,
                    provider_class=str(path),
                    trigger_class=trigger_path,
                    kwargs_schema=kwargs_schema,
                )
        for scheme, package in KNOWN_QUEUE_SCHEMES:
            if scheme not in found:
                found[scheme] = QueueScheme(
                    scheme=scheme,
                    provider=package,
                    available=False,
                )
        return sorted(found.values(), key=lambda q: q.scheme)

    def trigger_kwargs(self, provider_cls: ClassInfo) -> tuple[str | None, dict[str, Any]]:
        expr = provider_cls.method_returns.get("trigger_class")
        if not expr:
            return None, {}
        trig = self.analyzer.resolve(provider_cls.module, expr)
        if not isinstance(trig, ClassInfo):
            return None, {}
        props: dict[str, Any] = {}
        required: list[str] = []
        for p in trig.init or []:
            if p.name in _TRIGGER_SKIP or p.kind in ("var_positional", "var_keyword"):
                continue
            schema, supported = annotation_schema(p.annotation)
            if not supported:
                continue
            props[p.name] = {**schema, "title": p.name.replace("_", " ").capitalize()}
            if p.default is None:
                required.append(p.name)
        schema_out: dict[str, Any] = {"type": "object", "properties": props}
        if required:
            schema_out["required"] = required
        return f"{trig.module}.{trig.name}", schema_out

    def build(self, key: str, key_parts: dict[str, Any]) -> tuple[CatalogData, float]:
        start = time.perf_counter()
        refs = self.refs()
        if self.strategy == "static":
            self.preparse([r.module for r in refs])
        conn_types = self.connection_types()
        operators = self.operators(refs)
        notifiers = self.notifiers()
        package_icons, metadata = self.package_metadata(operators)
        for detail in operators:
            for index in self.module_source.get(detail.module, []):
                self.source_infos[index].operators.append(detail.fqn)
        for info in self.source_infos:
            info.operators.sort()
        data = CatalogData(
            key=key,
            key_parts=key_parts,
            strategy=self.strategy,
            built_at=time.time(),
            build_seconds=0.0,
            operators=sorted(operators, key=lambda d: d.fqn),
            notifiers=notifiers,
            decorators=self.decorators(),
            queue_schemes=self.queue_schemes(),
            connection_types=conn_types,
            errors=self.errors,
            diagnostics=CatalogBuildDiagnostics(
                sources=self.source_infos,
                filtered=self.filtered,
                parse_errors=[
                    CatalogError(module=m, provider=self.provider_of(m), error=e)
                    for m, e in sorted(self.analyzer.parse_errors.items())
                ],
                metadata=metadata,
            ),
            package_icons=package_icons,
        )
        data.build_seconds = round(time.perf_counter() - start, 3)
        return data, data.build_seconds

    def provider_of(self, module: str) -> str:
        indexes = self.module_source.get(module)
        if not indexes:
            return "?"
        index = indexes[0]
        providers = self.source_infos[index].providers
        return providers[0] if len(providers) == 1 else self.source_infos[index].source

    def package_metadata(self, operators: list[OperatorDetail]) -> tuple[dict[str, str], list[Any]]:
        """Merge the metadata operator packages ship (autoair.yaml, entry points)."""
        if not self.presentation.package_metadata or not operators:
            return {}, []
        module_files: list[tuple[str, Path]] = []
        for module in sorted({d.module for d in operators}):
            path = find_module_file(module)
            if path is not None:
                module_files.append((module, path))
        layers = entry_point_layers(self.strategy, find_module_file=find_module_file)
        layers += file_layers(module_files, self.presentation)
        if not layers:
            return {}, []
        result = apply_package_metadata(operators, layers, self.presentation)
        return result.icons, result.sources


# --------------------------------------------------------------------------- catalog


def _source_detail(src: CatalogSource) -> list[str]:
    """What a source scans, for the diagnostics (folders, packages, distributions…)."""
    if isinstance(src, ProvidersSource):
        return ["installed Airflow providers" + (" (+ package scan)" if src.scan_packages else "")]
    for attr in ("roots", "paths", "packages", "names", "_modules"):
        value = getattr(src, attr, None)
        if isinstance(value, (list, tuple)) and value:
            return [str(v) for v in value]
    return [repr(src)[:200]]


def _source_key(src: CatalogSource) -> Any:
    if isinstance(src, ProvidersSource):
        # Installed providers are covered by the distributions part of the key.
        return ["providers", {"scan_packages": bool(src.scan_packages)}]
    entry: list[Any] = [type(src).__qualname__]
    try:
        for ref in src.modules():
            path = find_module_file(ref.module)
            stat = path.stat() if path else None
            entry.append(
                [
                    ref.module,
                    ref.provider,
                    ref.category,
                    ref.provider_version,
                    ref.integration,
                    [stat.st_mtime_ns, stat.st_size] if stat else None,
                ]
            )
    except Exception as exc:
        # The type only: an exception's repr may hold addresses or timing (unstable key).
        log.debug("catalog source %r failed while computing the cache key", src, exc_info=True)
        entry.append(["error", type(exc).__name__])
    return entry


class OperatorCatalog:
    """Thread-safe, lazily built (and disk-cached) operator catalog."""

    def __init__(
        self,
        sources: Sequence[CatalogSource] | None = None,
        *,
        allow: Sequence[str] = ("*",),
        deny: Sequence[str] = (),
        strategy: CatalogStrategy = "static",
        cache_dir: Path | None = None,
        use_cache: bool = True,
        catalog_file: Path | None = None,
        subprocess_fallback: bool = True,
        workers: int | None = None,
        fallback_timeout: float = 300,
        discovery: DiscoveryFilters | None = None,
        presentation: CatalogSettings | None = None,
    ) -> None:
        self._sources = list(sources) if sources is not None else None
        # catalog.* presentation settings (connection params, groups, param overrides).
        self._presentation = presentation
        self._compiled: tuple[CatalogSettings, Presentation] | None = None
        # The discovery filters of this catalog (cache key and build): the process-wide ones
        # when it was created (configured_sources(settings) applies settings.discovery).
        self.discovery = discovery if discovery is not None else discovery_filters()
        self._allow = list(allow)
        self._deny = list(deny)
        self.strategy: CatalogStrategy = strategy
        self.cache_dir = cache_dir if cache_dir is not None else default_cache_dir()
        self.use_cache = use_cache
        env_file = os.environ.get("AUTOAIR_CATALOG_FILE")
        self.catalog_file = catalog_file or (Path(env_file) if env_file else None)
        self.subprocess_fallback = subprocess_fallback
        self.workers = workers
        self.fallback_timeout = fallback_timeout
        self._lock = threading.Lock()
        self._state: _State | None = None
        # The blueprint catalog travels with the operator catalog: both are import allowlists
        # of code generation, and every validation caller already passes this object.
        self.blueprints: BlueprintCatalog | None = None

    @property
    def presentation(self) -> CatalogSettings:
        """The ``catalog`` settings section in use (explicit, else the process' active one)."""
        if self._presentation is None:
            self._presentation = default_presentation()
        return self._presentation

    def _compiled_presentation(self) -> Presentation:
        current = self.presentation
        if self._compiled is None or self._compiled[0] is not current:
            self._compiled = (current, Presentation.from_settings(current))
        return self._compiled[1]

    # ----------------------------------------------------------------- keys
    def _default_sources(self) -> list[CatalogSource]:
        sources = configured_sources()  # applies the configured discovery filters
        self.discovery = discovery_filters()
        return sources

    def _resolved_sources(self) -> list[CatalogSource]:
        return self._sources if self._sources is not None else self._default_sources()

    def cache_key_parts(self, sources: Sequence[CatalogSource] | None = None) -> dict[str, Any]:
        """Inputs of the cache key: distributions, versions, strategy, custom sources."""
        resolved = sources if sources is not None else self._resolved_sources()
        custom: list[Any] = []
        with discovery_applied(self.discovery):
            for src in resolved:
                custom.append(_source_key(src))
        return {
            "format": layout_version(),
            "logic": logic_fingerprint(),
            "discovery": self.discovery.settings.fingerprint(),
            "presentation": self.presentation.build_fingerprint(),
            "autoair": __version__,
            "strategy": self.strategy,
            "distributions": airflow_distributions(),
            "sources": custom,
        }

    @staticmethod
    def _key(parts: dict[str, Any]) -> str:
        raw = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(raw.encode()).hexdigest()

    def cache_path(self, key: str) -> Path:
        return self.cache_dir / f"catalog-{self.strategy}-{key[:24]}.json"

    # ----------------------------------------------------------------- build
    def _allowed(self, fqn: str) -> bool:
        return self._refusal(fqn) is None

    def _refusal(self, fqn: str) -> CatalogFiltered | None:
        """Why ``operator_allow`` / ``operator_deny`` hide ``fqn`` (None: shown)."""
        if not any(fnmatch.fnmatchcase(fqn, p) for p in self._allow):
            return CatalogFiltered(
                name=fqn,
                kind="operator",
                reason="operator_allow",
                detail="matches none of " + ", ".join(self._allow),
            )
        denied = next((p for p in self._deny if fnmatch.fnmatchcase(fqn, p)), None)
        if denied is not None:
            return CatalogFiltered(name=fqn, kind="operator", reason="operator_deny", detail=denied)
        return None

    def _materialize(
        self,
        data: CatalogData,
        adapters: dict[str, dict[str, TypeAdapter[Any]]] | None = None,
        *,
        from_cache: bool,
    ) -> _State:
        adapters = adapters or {}
        presentation = self._compiled_presentation()
        filtered: list[CatalogFiltered] = []
        ops: dict[str, _Entry] = {}
        for d in data.operators:
            refused = self._refusal(d.fqn)
            if refused is not None:
                filtered.append(refused)
                continue
            # catalog.groups / catalog.param_overrides win over the package metadata.
            shown = presentation.apply(d)
            ops[d.fqn] = _Entry(
                shown, _params_from_schema(shown.params_schema, adapters.get(d.fqn))
            )
        notifiers = {
            d.fqn: _NotifierEntry(d, _params_from_schema(d.params_schema, adapters.get(d.fqn)))
            for d in data.notifiers
            if self._allowed(d.fqn)
        }
        return _State(data, ops, notifiers, from_cache, filtered)

    def build_data(self) -> tuple[CatalogData, dict[str, dict[str, TypeAdapter[Any]]]]:
        """Build from scratch (no cache involved)."""
        sources = self._resolved_sources()
        parts = self.cache_key_parts(sources)
        index = load_provider_index()
        for src in sources:
            if isinstance(src, ProvidersSource) and src._index is None:
                src._index = index
        builder = _Builder(
            sources,
            index,
            self.strategy,
            self.subprocess_fallback,
            self.workers,
            self.fallback_timeout,
            self.discovery,
            self.presentation,
        )
        with discovery_applied(self.discovery):
            data, _ = builder.build(self._key(parts), parts)
        for path, err in index.errors:
            data.errors.append(CatalogError(module=path, provider="?", error=err))
        return data, builder.adapters

    def _load(self, *, force: bool = False) -> _State:
        parts = self.cache_key_parts()
        key = self._key(parts)
        if not force:
            if self.catalog_file is not None:
                data = _read_data(self.catalog_file)
                if data is not None and _compatible(data.key_parts, parts):
                    return self._materialize(data, from_cache=True)
                log.warning("catalog file %s is missing or stale; ignoring it", self.catalog_file)
            if self.use_cache:
                data = _read_data(self.cache_path(key))
                if data is not None and data.key == key:
                    return self._materialize(data, from_cache=True)
        data, adapters = self.build_data()
        if self.use_cache:
            try:
                _write_data(self.cache_path(key), data)
                _prune(
                    self.cache_dir,
                    keep=self.cache_path(key),
                    max_files=self.discovery.settings.cache_max_files,
                )
            except OSError:
                log.warning("cannot write catalog cache to %s", self.cache_dir, exc_info=True)
        return self._materialize(data, adapters, from_cache=False)

    def refresh(self) -> CatalogStats:
        """Rebuild from scratch: also forget which modules failed to import (a missing
        optional dependency may have been installed since) and re-locate module files."""
        with self._lock:
            _PROBE_CACHE.clear()
            clear_locator_cache()
            self._state = self._load(force=True)
        return self.stats()

    def _ensure(self) -> _State:
        if self._state is None:
            with self._lock:
                if self._state is None:
                    self._state = self._load()
        return self._state

    def warm(self) -> CatalogStats:
        """Build (or load) now; useful at application start-up in a background thread."""
        return self.stats()

    @property
    def data(self) -> CatalogData:
        return self._ensure().data

    # ----------------------------------------------------------------- queries
    def summaries(self) -> list[OperatorSummary]:
        fields = set(OperatorSummary.model_fields)
        return [
            OperatorSummary.model_validate(e.detail.model_dump(include=fields))
            for e in self._ensure().operators.values()
        ]

    def get(self, fqn: str) -> OperatorDetail | None:
        entry = self._ensure().operators.get(fqn)
        return entry.detail if entry else None

    def __contains__(self, fqn: object) -> bool:
        return fqn in self._ensure().operators

    def stats(self) -> CatalogStats:
        state = self._ensure()
        return CatalogStats(
            operators=len(state.operators),
            providers=len({e.detail.provider for e in state.operators.values()}),
            errors=list(state.data.errors),
            notifiers=len(state.notifiers),
            strategy=state.data.strategy,
            cache_key=state.data.key,
            loaded_from_cache=state.loaded_from_cache,
            build_seconds=state.data.build_seconds,
        )

    def diagnostics(self) -> CatalogDiagnostics:
        """What the last build scanned, found, filtered and failed on (admin diagnostics)."""
        state = self._ensure()
        data = state.data
        diag = data.diagnostics
        return CatalogDiagnostics(
            strategy=data.strategy,
            cache_key=data.key,
            loaded_from_cache=state.loaded_from_cache,
            built_at=data.built_at,
            build_seconds=data.build_seconds,
            loaded_at=state.loaded_at,
            operators=len(state.operators),
            notifiers=len(state.notifiers),
            providers=len({e.detail.provider for e in state.operators.values()}),
            sources=list(diag.sources),
            filtered=[*diag.filtered, *state.filtered],
            parse_errors=list(diag.parse_errors),
            metadata=list(diag.metadata),
            errors=list(data.errors),
        )

    def package_icon(self, icon_id: str) -> str | None:
        """A sanitised SVG icon shipped by an operator package (``package:<name>``)."""
        return self._ensure().data.package_icons.get(icon_id)

    def package_icon_ids(self) -> list[str]:
        return sorted(self._ensure().data.package_icons)

    def notifiers(self) -> list[NotifierSummary]:
        fields = set(NotifierSummary.model_fields)
        return [
            NotifierSummary.model_validate(e.detail.model_dump(include=fields))
            for e in self._ensure().notifiers.values()
        ]

    def get_notifier(self, fqn: str) -> NotifierDetail | None:
        entry = self._ensure().notifiers.get(fqn)
        return entry.detail if entry else None

    def decorators(self) -> list[TaskDecoratorInfo]:
        return list(self._ensure().data.decorators)

    def queue_schemes(self) -> list[QueueScheme]:
        return list(self._ensure().data.queue_schemes)

    def connection_types(self) -> list[ConnectionTypeInfo]:
        return list(self._ensure().data.connection_types)

    def validate_params(
        self, fqn: str, params: dict[str, Any], mapped: Iterable[str] = ()
    ) -> list[tuple[str | None, str]]:
        """Validate operator params; returns ``(field, message)`` problems.

        Templated strings (``{{ ... }}``) and XCom references are accepted for any
        field since they are only resolved at runtime.
        """
        entry = self._ensure().operators.get(fqn)
        if entry is None:
            return [(None, f"operator {fqn!r} is not in the catalog / allowlist")]
        problems: list[tuple[str | None, str]] = []
        if not entry.detail.builder_supported:
            hint = (
                f"; use a Python task node with @task.{entry.detail.equivalent_decorator} instead"
                if entry.detail.equivalent_decorator
                else ""
            )
            problems.append(
                (
                    None,
                    f"{entry.detail.class_name} is not supported by the builder yet "
                    f"({entry.detail.unsupported_reason}){hint}",
                )
            )
        return problems + _check_params(entry.params, fqn, params, set(mapped))

    def validate_notifier_params(
        self, fqn: str, params: dict[str, Any]
    ) -> list[tuple[str | None, str]]:
        entry = self._ensure().notifiers.get(fqn)
        if entry is None:
            return [(None, f"notifier {fqn!r} is not in the catalog / allowlist")]
        return _check_params(entry.params, fqn, params, set())


def _check_params(
    known: dict[str, ParamInfo], fqn: str, params: dict[str, Any], mapped: set[str]
) -> list[tuple[str | None, str]]:
    problems: list[tuple[str | None, str]] = []
    for name, info in known.items():
        if info.required and name not in mapped:
            if name not in params:
                hint = (
                    "" if info.supported else " (a Python reference, e.g. my_pkg.module.function)"
                )
                problems.append((name, f"required parameter {name!r} is missing{hint}"))
            elif isinstance(params[name], str) and not params[name].strip():
                # An empty string would render `bash_command=""`: a task that fails at runtime.
                problems.append((name, f"required parameter {name!r} is empty"))
    for name, value in params.items():
        pinfo = known.get(name)
        if pinfo is None:
            problems.append((name, f"unknown parameter {name!r} for {fqn.rsplit('.', 1)[-1]}"))
            continue
        if not pinfo.supported and not _is_python_ref(value) and name not in mapped:
            problems.append(
                (
                    name,
                    f"parameter {name!r} takes a Python object: write a function or give a "
                    "Python reference",
                )
            )
            continue
        if name in mapped or _is_dynamic(value):
            continue
        if pinfo.adapter is not None:
            try:
                pinfo.adapter.validate_python(value)
            except ValidationError as exc:
                msg = "; ".join(e["msg"] for e in exc.errors()[:3])
                problems.append((name, f"invalid value for {name!r}: {msg}"))
            continue
        schema = {k: v for k, v in pinfo.schema.items() if k != "default"}
        errs = validate_value(schema, value)
        if errs:
            problems.append((name, f"invalid value for {name!r}: {'; '.join(errs)}"))
    return problems


def _is_python_ref(value: Any) -> bool:
    """A Python object: an import reference or a function written in the builder."""
    return isinstance(value, dict) and value.get("$type") in ("import", "function")


def _is_dynamic(value: Any) -> bool:
    if isinstance(value, str):
        return "{{" in value or "{%" in value
    if isinstance(value, dict):
        return "$type" in value or any(_is_dynamic(v) for v in value.values())
    if isinstance(value, list):
        return any(_is_dynamic(v) for v in value)
    return False


# --------------------------------------------------------------------------- files


def _read_data(path: Path) -> CatalogData | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = CatalogData.model_validate_json(raw)
    except ValidationError:
        log.warning("ignoring unreadable catalog cache %s", path)
        return None
    return data if data.format == layout_version() else None


def _write_data(path: Path, data: CatalogData) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(data.model_dump_json(), encoding="utf-8")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


# Temporary files of interrupted cache writes (killed process) older than this are removed.
_TMP_MAX_AGE = 3600.0


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _prune(cache_dir: Path, keep: Path, max_files: int = 8) -> None:
    files = sorted(
        (p for p in cache_dir.glob("catalog-*.json") if p != keep),
        key=_mtime,
        reverse=True,
    )
    for old in files[max_files - 1 :]:
        old.unlink(missing_ok=True)
    # Leftovers of writes that died before os.replace (another process may be writing a
    # fresh one: only old files are removed).
    cutoff = time.time() - _TMP_MAX_AGE
    for tmp in cache_dir.glob(".catalog-*.tmp"):
        if _mtime(tmp) < cutoff:
            tmp.unlink(missing_ok=True)


def _compatible(stored: dict[str, Any], current: dict[str, Any]) -> bool:
    """A precomputed file is usable when everything but custom-source file stats match."""

    def strip(parts: dict[str, Any]) -> dict[str, Any]:
        sources = [
            [s[0], *[r[:5] if isinstance(r, list) else r for r in s[1:]]]
            if isinstance(s, list)
            else s
            for s in parts.get("sources", [])
        ]
        return {**parts, "sources": json.loads(json.dumps(sources, default=str))}

    return strip(json.loads(json.dumps(stored, default=str))) == strip(
        json.loads(json.dumps(current, default=str))
    )


def default_presentation() -> CatalogSettings:
    """The ``catalog`` section of the running process: the activated settings (API services,
    CLI) or, when none were activated, the one of the ``AUTOAIR_*`` environment/files."""
    from autoair import sections

    found = sections._ACTIVE.get(CatalogSettings)
    if isinstance(found, CatalogSettings):
        return found
    from autoair.catalog.discovery import CatalogSourceSettings

    try:
        return CatalogSourceSettings().catalog
    except Exception:
        log.warning("cannot read the catalog settings; using the defaults", exc_info=True)
        return CatalogSettings()


def build_catalog_file(
    path: Path,
    sources: Sequence[CatalogSource] | None = None,
    *,
    strategy: CatalogStrategy = "static",
    subprocess_fallback: bool = True,
    presentation: CatalogSettings | None = None,
) -> CatalogStats:
    """Build the catalog from scratch and write it to ``path`` (e.g. at image build time)."""
    catalog = OperatorCatalog(
        sources,
        strategy=strategy,
        use_cache=False,
        subprocess_fallback=subprocess_fallback,
        presentation=presentation,
    )
    data, _ = catalog.build_data()
    _write_data(Path(path), data)
    catalog._state = catalog._materialize(data, from_cache=False)
    return catalog.stats()


def load_catalog_file(
    path: Path,
    sources: Sequence[CatalogSource] | None = None,
    *,
    allow: Sequence[str] = ("*",),
    deny: Sequence[str] = (),
    check_versions: bool = True,
) -> OperatorCatalog:
    """Catalog backed by a precomputed file.

    With ``check_versions`` (default) a stale file (different Airflow distributions,
    AutoAir version or sources) is ignored and the catalog is rebuilt lazily.
    """
    data = _read_data(Path(path))
    if data is None:
        raise FileNotFoundError(f"no usable catalog file at {path}")
    catalog = OperatorCatalog(
        sources, allow=allow, deny=deny, strategy=data.strategy, catalog_file=Path(path)
    )
    if not check_versions:
        catalog._state = catalog._materialize(data, from_cache=True)
    return catalog


__all__ = [
    "CatalogData",
    "CatalogSource",
    "ModuleRef",
    "ModulesSource",
    "OperatorCatalog",
    "ProvidersSource",
    "build_catalog_file",
    "default_cache_dir",
    "entry_point_sources",
    "layout_version",
    "load_catalog_file",
    "logic_fingerprint",
]


def probe_imports(modules: list[str], timeout: float = 120) -> dict[str, str]:
    """Import ``modules`` in one subprocess (never in this process); return failures.

    Results are memoised per process; the catalog itself is cached on disk.
    """
    pending = [m for m in modules if m not in _PROBE_CACHE]
    if pending:
        import json as _json
        import subprocess
        import sys

        code = (
            "import importlib, json, sys\n"
            "out = {}\n"
            "for m in json.loads(sys.argv[1]):\n"
            "    try:\n"
            "        importlib.import_module(m)\n"
            "    except BaseException as exc:\n"
            "        out[m] = f'{type(exc).__name__}: {exc}'[:500]\n"
            "sys.stdout.write('@@PROBE@@' + json.dumps(out))\n"
        )
        failures: dict[str, str] | None = None
        try:
            proc = subprocess.run(
                [sys.executable, "-c", code, _json.dumps(pending)],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env=user_code_env(),  # user code: never AutoAir's own secrets
            )
            marker = proc.stdout.rfind("@@PROBE@@")
            if marker >= 0:
                failures = _json.loads(proc.stdout[marker + 9 :])
            else:
                tail = (proc.stderr or "").strip().splitlines()[-3:]
                log.warning(
                    "import probe crashed (rc=%s): %s", proc.returncode, " | ".join(tail) or "-"
                )
        except subprocess.TimeoutExpired:
            log.warning("import probe of %d module(s) timed out after %ss", len(pending), timeout)
        except (OSError, ValueError):
            log.warning("import probe failed", exc_info=True)
        # Only a completed probe is remembered: after a crash or a timeout the modules are
        # kept for now (unknown) and probed again next time.
        if failures is not None:
            for m in pending:
                _PROBE_CACHE[m] = failures.get(m)
    return {m: err for m in modules if (err := _PROBE_CACHE.get(m))}


_PROBE_CACHE: dict[str, str | None] = {}
