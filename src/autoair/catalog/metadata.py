"""Operator metadata shipped by operator packages, and the presentation overrides of the
configuration (``catalog.*`` settings).

A package (say ``bifrost``) can make its operators look first-class in the palette without
any AutoAir configuration, through either

* a file ``autoair.yaml`` / ``autoair.yml`` / ``autoair.json`` in a package directory (it
  applies to that package and its subpackages; read statically — the package is never
  imported), or
* an entry point in the ``autoair.operator_metadata`` group pointing to a dict or to a
  function returning one (it applies to the modules of the distribution declaring it). With
  ``catalog_strategy=import`` it is loaded normally; with the default static strategy it is
  read from the module's source when the value is a literal (``METADATA = {...}`` or a
  function whose body is ``return {...}``).

Both have the :class:`PackageMetadata` layout: package-wide ``group`` / ``group_order`` /
``icon`` / ``docs_url`` plus per-operator entries (``operators``, keyed by class path, class
name or an fnmatch pattern) with a description, docs URL, icon, parameter overrides
(:class:`~autoair.sections.ParamOverride`) and examples. Icons may be bundled ids or SVG files
inside the package; SVGs are sanitised (:func:`autoair.icons.sanitize_svg`).

Precedence: AutoAir configuration (``catalog.groups``, ``catalog.param_overrides``,
``icons.rules_extra``, icon assignments) > package metadata > inference.
"""

from __future__ import annotations

import ast
import copy
import fnmatch
import json
import logging
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from importlib.metadata import EntryPoint, entry_points
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from autoair.catalog.models import CatalogMetadataSource, OperatorExample
from autoair.sections import CatalogSettings, ParamOverride

if TYPE_CHECKING:
    from autoair.catalog.models import OperatorDetail

log = logging.getLogger(__name__)

METADATA_ENTRY_POINT_GROUP = "autoair.operator_metadata"
METADATA_FILES = ("autoair.yaml", "autoair.yml", "autoair.json")
_URL = re.compile(r"^https?://[^\s]+$")
CODE_WIDGETS = frozenset({"sql", "bash", "python", "yaml", "markdown"})


def _check_url(value: str | None) -> str | None:
    if value is not None and not _URL.match(value):
        raise ValueError(f"docs_url must be an http(s) URL, got {value!r}")
    return value


class OperatorMetadata(BaseModel):
    """Presentation of one operator (or of every operator matching a pattern)."""

    model_config = ConfigDict(extra="forbid")

    description: str | None = Field(
        default=None, max_length=2000, description="Palette summary of the operator."
    )
    docs_url: str | None = Field(default=None, description="Documentation link (http/https).")
    icon: str | None = Field(
        default=None,
        description="builtin:<logo>, a glyph name, a name of `icons`, or an SVG file path "
        "relative to the metadata file.",
    )
    group: str | None = Field(default=None, max_length=80, description="Palette group.")
    group_order: int | None = Field(default=None, description="Palette group sort key.")
    params: dict[str, ParamOverride] = Field(
        default_factory=dict, description="Parameter widgets, labels, help…"
    )
    examples: list[OperatorExample] = Field(
        default_factory=list, description="Ready-made parameter sets shown in the inspector."
    )

    @field_validator("docs_url")
    @classmethod
    def _url(cls, value: str | None) -> str | None:
        return _check_url(value)


class PackageMetadata(BaseModel):
    """Layout of ``autoair.yaml`` / the ``autoair.operator_metadata`` entry point value."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = Field(default=1, description="Metadata format version.")
    group: str | None = Field(
        default=None, max_length=80, description="Palette group of every operator."
    )
    group_order: int | None = Field(default=None, description="Palette group sort key.")
    icon: str | None = Field(default=None, description="Default icon of every operator.")
    docs_url: str | None = Field(default=None, description="Default documentation link.")
    icons: dict[str, str] = Field(
        default_factory=dict,
        description="Named icons: an SVG file path relative to the metadata file (or the "
        "entry point's package), or inline SVG markup.",
    )
    operators: dict[str, OperatorMetadata] = Field(
        default_factory=dict,
        description="Per operator: key = class path, class name or fnmatch pattern.",
    )

    @field_validator("docs_url")
    @classmethod
    def _url(cls, value: str | None) -> str | None:
        return _check_url(value)


# --------------------------------------------------------------------------- layers


@dataclass
class MetadataLayer:
    """One metadata document and what it applies to."""

    kind: Literal["file", "entry_point"]
    origin: str
    metadata: PackageMetadata | None
    base_dir: Path | None
    package: str | None = None  # applies to this package and its subpackages
    modules: frozenset[str] = frozenset()  # ... or to exactly these modules
    distribution: str | None = None
    error: str | None = None
    applied: set[str] = field(default_factory=set)
    icons: set[str] = field(default_factory=set)
    depth: int = 0

    def covers(self, module: str) -> bool:
        if self.modules:
            return module in self.modules
        return bool(self.package) and (
            module == self.package or module.startswith(f"{self.package}.")
        )

    def info(self) -> CatalogMetadataSource:
        return CatalogMetadataSource(
            kind=self.kind,
            origin=self.origin,
            package=self.package,
            distribution=self.distribution,
            operators=len(self.applied),
            icons=len(self.icons),
            error=self.error,
        )


def _parse_document(text: str, suffix: str) -> Any:
    if suffix == ".json":
        return json.loads(text)
    import yaml

    return yaml.safe_load(text)


def _validate(raw: Any) -> PackageMetadata:
    if isinstance(raw, PackageMetadata):
        return raw
    if not isinstance(raw, dict):
        raise ValueError(f"metadata must be a mapping, got {type(raw).__name__}")
    return PackageMetadata.model_validate(raw)


def _error_text(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        parts = [
            f"{'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}"
            for e in exc.errors()[:5]
        ]
        return "invalid metadata: " + "; ".join(parts)
    return f"{type(exc).__name__}: {exc}"


def file_layers(
    module_files: Iterable[tuple[str, Path]], settings: CatalogSettings
) -> list[MetadataLayer]:
    """Metadata files of the packages containing the given ``(module, file)`` pairs: every
    package directory from the module up to its top-level package is checked once."""
    seen: dict[Path, MetadataLayer | None] = {}
    for module, path in module_files:
        parts = module.split(".")
        directory = path.parent
        names = parts if path.name == "__init__.py" else parts[:-1]
        while names:
            if directory not in seen:
                seen[directory] = _read_file_layer(directory, ".".join(names), settings)
            directory = directory.parent
            names = names[:-1]
    return [layer for layer in seen.values() if layer is not None]


def _read_file_layer(
    directory: Path, package: str, settings: CatalogSettings
) -> MetadataLayer | None:
    for name in METADATA_FILES:
        candidate = directory / name
        try:
            if not candidate.is_file():
                continue
            layer = MetadataLayer(
                kind="file",
                origin=str(candidate),
                metadata=None,
                base_dir=directory,
                package=package,
                depth=package.count(".") + 1,
            )
            size = candidate.stat().st_size
            if size > settings.metadata_max_bytes:
                layer.error = f"file is larger than {settings.metadata_max_bytes} bytes"
                return layer
            text = candidate.read_text(encoding="utf-8")
        except OSError as exc:
            return MetadataLayer(
                kind="file",
                origin=str(candidate),
                metadata=None,
                base_dir=directory,
                package=package,
                error=f"{type(exc).__name__}: {exc}",
            )
        try:
            layer.metadata = _validate(_parse_document(text, candidate.suffix))
        except Exception as exc:
            layer.error = _error_text(exc)
        return layer
    return None


def entry_point_layers(
    strategy: str,
    *,
    find_module_file: Callable[[str], Path | None],
    eps: Sequence[EntryPoint] | None = None,
) -> list[MetadataLayer]:
    """Layers of the ``autoair.operator_metadata`` entry points of installed distributions."""
    from autoair.catalog.discovery import _dist_modules, _norm, _top_level_packages

    layers: list[MetadataLayer] = []
    found = list(eps) if eps is not None else list(entry_points(group=METADATA_ENTRY_POINT_GROUP))
    for ep in found:
        dist = getattr(ep, "dist", None)
        dist_name = _norm(dist.name) if dist is not None and dist.name else None
        module_name = ep.value.split(":", 1)[0].strip()
        module_file = find_module_file(module_name)
        layer = MetadataLayer(
            kind="entry_point",
            origin=f"{ep.name} = {ep.value}",
            metadata=None,
            base_dir=module_file.parent if module_file else None,
            distribution=dist_name,
        )
        if dist is not None:
            modules = _dist_modules(dist)
            if modules:
                layer.modules = frozenset(modules)
            else:  # editable install: its import packages
                tops = _top_level_packages(dist)
                layer.package = tops[0] if len(tops) == 1 else None
                if len(tops) > 1:
                    layer.modules = frozenset(tops)
        if not layer.modules and not layer.package:
            layer.package = module_name.split(".", 1)[0]
        try:
            if strategy == "import":
                obj = ep.load()
                raw = obj() if callable(obj) and not isinstance(obj, dict) else obj
            else:
                raw = static_entry_point_value(ep.value, find_module_file)
            layer.metadata = _validate(raw)
        except Exception as exc:
            layer.error = _error_text(exc)
        layers.append(layer)
    return layers


def static_entry_point_value(value: str, find_module_file: Callable[[str], Path | None]) -> Any:
    """Read ``module:attr`` without importing it: ``attr = {...}`` (a literal) or
    ``def attr(): return {...}``. Anything else needs ``catalog_strategy=import``."""
    module, _, attr = value.partition(":")
    attr = attr.strip().split(".", 1)[0].split("[", 1)[0].strip()
    module = module.strip()
    if not attr:
        raise ValueError("the entry point must name an attribute (module:attr)")
    path = find_module_file(module)
    if path is None:
        raise ValueError(f"module {module!r} not found")
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == attr for t in node.targets
        ):
            return ast.literal_eval(node.value)
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == attr
            and node.value is not None
        ):
            return ast.literal_eval(node.value)
        if isinstance(node, ast.FunctionDef) and node.name == attr:
            body = [n for n in node.body if not _is_docstring(n)]
            if len(body) == 1 and isinstance(body[0], ast.Return) and body[0].value is not None:
                return ast.literal_eval(body[0].value)
            raise ValueError(
                f"{value}: the function is not a plain `return {{...}}`; it cannot be read "
                "statically (use catalog_strategy=import or ship an autoair.yaml file)"
            )
    raise ValueError(f"{value}: no literal {attr!r} found in {path.name}")


def _is_docstring(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


# --------------------------------------------------------------------------- param overrides


def apply_param_overrides(
    schema: dict[str, Any], overrides: dict[str, ParamOverride]
) -> tuple[dict[str, Any], list[str]]:
    """``schema`` (an operator's params JSON Schema) with the overrides applied (a copy), plus
    the connection types the overrides name."""
    out = copy.deepcopy(schema)
    props: dict[str, Any] = out.setdefault("properties", {})
    required: list[str] = list(out.get("required") or [])
    conn_types: list[str] = []
    for name, ov in overrides.items():
        prop = props.get(name)
        if not isinstance(prop, dict):
            continue
        widget = ov.widget or ("enum" if ov.enum else None)
        if widget is not None:
            prop["x-autoair-widget"] = widget
            if widget in CODE_WIDGETS:
                prop["x-autoair-renderer"] = widget
            elif widget in {"text", "secret"}:
                prop.pop("x-autoair-renderer", None)
        if ov.enum is not None:
            prop["enum"] = list(ov.enum)
        if ov.conn_type is not None:
            types = [ov.conn_type] if isinstance(ov.conn_type, str) else list(ov.conn_type)
            prop["x-autoair-conn-type"] = types
            prop.setdefault("x-autoair-widget", "connection")
            conn_types += [t for t in types if t not in conn_types]
        if ov.label is not None:
            prop["title"] = ov.label
            prop["x-autoair-label"] = ov.label  # shown as given (even when it is the name)
        if ov.help is not None:
            prop["description"] = ov.help
        if ov.hidden is not None:
            if ov.hidden:
                prop["x-autoair-hidden"] = True
            else:
                prop.pop("x-autoair-hidden", None)
        if "default" in ov.model_fields_set:
            prop["default"] = ov.default
        if ov.required is True and name not in required:
            required.append(name)
        elif ov.required is False and name in required:
            required.remove(name)
    if required:
        out["required"] = required
    else:
        out.pop("required", None)
    return out, conn_types


def matching_overrides(
    table: dict[str, dict[str, ParamOverride]], fqn: str, class_name: str
) -> dict[str, ParamOverride]:
    """Overrides of every key matching the operator (class path, class name or fnmatch
    pattern), less specific (shorter) keys first so specific ones win param by param."""
    merged: dict[str, ParamOverride] = {}
    for key in sorted(table, key=len):
        if key in (fqn, class_name) or fnmatch.fnmatchcase(fqn, key):
            for param, ov in table[key].items():
                previous = merged.get(param)
                merged[param] = _merge_override(previous, ov) if previous else ov
    return merged


def _merge_override(base: ParamOverride, top: ParamOverride) -> ParamOverride:
    data = base.model_dump(exclude_unset=True)
    data.update(top.model_dump(exclude_unset=True))
    return ParamOverride.model_validate(data)


# --------------------------------------------------------------------------- package metadata


@dataclass
class PackageMetadataResult:
    icons: dict[str, str]  # package icon id -> sanitised SVG
    sources: list[CatalogMetadataSource]


def _icon_id(owner: str, name: str) -> str:
    from autoair.icons import slug

    stem = name.rsplit("/", 1)[-1]
    stem = stem[:-4] if stem.lower().endswith(".svg") else stem
    return f"package:{slug(f'{owner}-{stem}')[:80]}"


def _resolve_icon(
    layer: MetadataLayer, ref: str, icons: dict[str, str], settings: CatalogSettings
) -> str | None:
    import hashlib

    from autoair.icons import IconError, normalize_icon_ref, sanitize_svg

    meta = layer.metadata
    assert meta is not None
    owner = layer.package or layer.distribution or "pkg"
    if ref in meta.icons:
        source, name = meta.icons[ref], ref
    elif ref.lstrip().startswith("<"):
        source, name = ref, "inline-" + hashlib.sha256(ref.encode()).hexdigest()[:8]
    elif ref.lower().endswith(".svg"):
        source, name = ref, ref
    else:
        try:
            return normalize_icon_ref(ref)
        except ValueError as exc:
            layer.error = layer.error or str(exc)
            return None
    icon_id = _icon_id(owner, name)
    if icon_id in icons:
        return icon_id
    try:
        if source.lstrip().startswith("<"):
            svg = sanitize_svg(source, settings.package_icon_max_bytes)
        else:
            if layer.base_dir is None:
                raise IconError("no package folder to read the icon from")
            base = layer.base_dir.resolve()
            path = (base / source).resolve()
            if not path.is_relative_to(base):
                raise IconError(f"icon path {source!r} leaves the package")
            if path.stat().st_size > settings.package_icon_max_bytes:
                raise IconError(f"icon {source!r} is too large")
            svg = sanitize_svg(path.read_bytes(), settings.package_icon_max_bytes)
    except (IconError, OSError, UnicodeDecodeError) as exc:
        layer.error = layer.error or f"icon {ref!r}: {exc}"
        return None
    icons[icon_id] = svg
    layer.icons.add(icon_id)
    return icon_id


def _operator_entries(meta: PackageMetadata, fqn: str, class_name: str) -> list[OperatorMetadata]:
    return [
        meta.operators[key]
        for key in sorted(meta.operators, key=len)
        if key in (fqn, class_name) or fnmatch.fnmatchcase(fqn, key)
    ]


def apply_package_metadata(
    details: list[OperatorDetail], layers: Sequence[MetadataLayer], settings: CatalogSettings
) -> PackageMetadataResult:
    """Merge package metadata into ``details`` in place (it wins over inference)."""
    icons: dict[str, str] = {}
    usable = [layer for layer in layers if layer.metadata is not None]
    # Entry points first, then files from the outermost package inwards (inner ones win).
    usable.sort(key=lambda layer: (layer.kind == "file", layer.depth))
    for detail in details:
        for layer in usable:
            if not layer.covers(detail.module):
                continue
            meta = layer.metadata
            assert meta is not None
            entries = _operator_entries(meta, detail.fqn, detail.class_name)
            if not entries and not (meta.group or meta.icon or meta.docs_url):
                continue
            layer.applied.add(detail.fqn)
            update: dict[str, Any] = {}
            if meta.group:
                update["group"] = meta.group
            if meta.group_order is not None:
                update["group_order"] = meta.group_order
            if meta.docs_url:
                update["docs_url"] = meta.docs_url
            icon_ref: str | None = meta.icon
            overrides: dict[str, ParamOverride] = {}
            for entry in entries:
                if entry.description:
                    update["summary"] = entry.description
                if entry.docs_url:
                    update["docs_url"] = entry.docs_url
                if entry.group:
                    update["group"] = entry.group
                if entry.group_order is not None:
                    update["group_order"] = entry.group_order
                if entry.icon:
                    icon_ref = entry.icon
                if entry.examples:
                    update["examples"] = list(entry.examples)
                for param, ov in entry.params.items():
                    previous = overrides.get(param)
                    overrides[param] = _merge_override(previous, ov) if previous else ov
            if icon_ref:
                resolved = _resolve_icon(layer, icon_ref, icons, settings)
                if resolved:
                    update["icon"] = resolved
            for key, value in update.items():
                setattr(detail, key, value)
            if overrides:
                schema, conn_types = apply_param_overrides(detail.params_schema, overrides)
                detail.params_schema = schema
                detail.connection_types += [
                    t for t in conn_types if t not in detail.connection_types
                ]
    return PackageMetadataResult(icons=icons, sources=[layer.info() for layer in layers])


# --------------------------------------------------------------------------- configuration


@dataclass(frozen=True)
class Presentation:
    """Compiled ``catalog.*`` presentation settings applied when the catalog is loaded."""

    groups: tuple[tuple[re.Pattern[str], str, int], ...]
    overrides: dict[str, dict[str, ParamOverride]]
    default_group_order: int

    @classmethod
    def from_settings(cls, s: CatalogSettings) -> Presentation:
        return cls(
            groups=tuple((re.compile(g.match), g.group, g.order) for g in s.groups),
            overrides=dict(s.param_overrides),
            default_group_order=s.default_group_order,
        )

    def group_of(self, fqn: str, module: str, provider: str) -> tuple[str, int] | None:
        for pattern, group, order in self.groups:
            if pattern.search(fqn) or pattern.search(module) or pattern.search(provider):
                return group, order
        return None

    def apply(self, detail: OperatorDetail) -> OperatorDetail:
        """``detail`` with the configured group and parameter overrides (a copy when changed)."""
        update: dict[str, Any] = {}
        found = self.group_of(detail.fqn, detail.module, detail.provider)
        if found is not None:  # configuration > package metadata
            update["group"], update["group_order"] = found
        elif detail.group:  # package metadata
            if detail.group_order is None:
                update["group_order"] = 0
        elif detail.group_order != self.default_group_order:  # the provider group
            update["group_order"] = self.default_group_order
        overrides = (
            matching_overrides(self.overrides, detail.fqn, detail.class_name)
            if self.overrides
            else {}
        )
        if overrides:
            schema, conn_types = apply_param_overrides(detail.params_schema, overrides)
            update["params_schema"] = schema
            update["connection_types"] = [
                *detail.connection_types,
                *(t for t in conn_types if t not in detail.connection_types),
            ]
        return detail.model_copy(update=update) if update else detail


__all__ = [
    "METADATA_ENTRY_POINT_GROUP",
    "METADATA_FILES",
    "MetadataLayer",
    "OperatorMetadata",
    "PackageMetadata",
    "Presentation",
    "apply_package_metadata",
    "apply_param_overrides",
    "entry_point_layers",
    "file_layers",
    "matching_overrides",
    "static_entry_point_value",
]
