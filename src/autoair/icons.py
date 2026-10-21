"""Canvas icons: bundled integration logos + user-uploaded custom icons.

* **Bundled** (``builtin:<name>``) — provider integration logos (S3, Jira, BigQuery, dbt…)
  downloaded by ``scripts/fetch_logos.py`` into ``resources/logos`` (64 px PNG or SVG).
* **Custom** (``custom:<name>``) — uploaded by users: the browser crops/resizes the image to a
  square PNG, the server re-encodes it with Pillow (≤ 128 px, metadata stripped), so only
  small, inert PNGs are ever stored or served. Stored under ``AUTOAIR_ICONS_URL`` (fsspec;
  default ``<output>/.autoair/icons``) next to ``assignments.json``, or in the SQL database
  (``database.backend``).
* **Package** (``package:<name>``) — SVG icons shipped by operator packages in their
  metadata (``autoair.yaml``), sanitised (:func:`sanitize_svg`) at catalog build time and
  served from the catalog.
* **Assignments** — default icon per operator class (e.g. your own ``GitlabCreateIssueOperator``)
  or per provider; a node's own ``icon`` field (canvas only) wins over both.

Resolution for an operator: node icon → operator assignment → provider assignment →
``icons.rules_extra`` (configuration) → the icon its package metadata declares → its
integration's logo → a logo whose integration name appears in the class/module name →
the provider's first logo → a concept glyph (``GLYPH_RULES``).
"""

from __future__ import annotations

import io
import json
import logging
import re
import threading
import time
from collections.abc import Callable
from functools import cache, lru_cache
from importlib import resources
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from autoair.persistence.documents import DocumentStore, FileDocumentStore

log = logging.getLogger(__name__)

ICON_ID_PATTERN = r"^(builtin|custom|package):[a-z0-9][a-z0-9-]{0,80}$"
_ID_RE = re.compile(ICON_ID_PATTERN)
_WORD = re.compile(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+")
# Integration names too generic to infer an icon from a class or module name.
_GENERIC = {"sql", "http", "ftp", "smtp", "ssh", "sftp", "jdbc", "odbc", "imap", "grpc", "common"}
# Packages/integrations without a provider logo of their own.
ALIASES = {
    "dbt": "dbt Cloud",
    "python": "@python",
    "bash": "@bash",
    "pyspark": "Apache Spark",
    "gitlab": "@gitlab",
    "vespa": "@vespa",
    "pgvector": "PostgreSQL",
}
# Concept glyphs for operators with no vendor/provider logo (first match wins; matched against
# the spaced lower-case words of the class name, then of the module).
GLYPH_RULES: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pattern), glyph)
    for pattern, glyph in (
        (r" agent ", "agent"),
        (r" (hitl|approval|human) ", "human"),
        (r" llm sql | sql query ", "sql-ai"),
        (r" schema compare ", "schema-compare"),
        (r" rerank ", "rerank"),
        (r" embedding ", "embedding"),
        (r" retrieval | retriever ", "retrieval"),
        (r" (document|file) (loader|analysis) ", "document"),
        (r" llm | llama | gen ai | genai | openai ", "llm"),
        (r" branch ", "branch"),
        (r" trigger dag ", "trigger"),
        (r" external task ", "link"),
        (r" latest only ", "latest"),
        (r" empty ", "empty"),
        (r" smooth ", "smooth"),
        (r" (day of week|weekday) ", "calendar"),
        (r" (time delta|wait) ", "hourglass"),
        (r" (date time|datetime|time) ", "clock"),
        (r" (psrp|power shell|powershell|winrm|shell) ", "terminal"),
        (r" (file transfer|transfer) ", "transfer"),
        (r" fivetran | sync ", "sync"),
        (r" (gx|great expectations|checkpoint|validate|quality) ", "quality"),
    )
)


class IconInfo(BaseModel):
    id: str
    name: str
    kind: Literal["builtin", "custom", "package"]
    url: str = Field(description="Relative to the API base.")


class IconAssignments(BaseModel):
    operators: dict[str, str] = Field(default_factory=dict, description="operator FQN -> icon id")
    providers: dict[str, str] = Field(default_factory=dict, description="provider -> icon id")


class IconError(Exception):
    pass


class PackageIcons(Protocol):
    """Source of the sanitised SVG icons operator packages ship (the operator catalog)."""

    def package_icon(self, icon_id: str) -> str | None: ...

    def package_icon_ids(self) -> list[str]: ...


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:80] or "icon"


# --------------------------------------------------------------------------- bundled


@cache
def _manifest() -> dict[str, dict[str, str]]:
    try:
        raw = resources.files("autoair.resources").joinpath("logos/manifest.json").read_text()
    except (FileNotFoundError, ModuleNotFoundError):
        return {"integrations": {}, "providers": {}, "extra": {}, "glyphs": {}}
    data: dict[str, dict[str, str]] = json.loads(raw)
    data.setdefault("glyphs", {})
    return data


def _builtin_id(file: str) -> str:
    return f"builtin:{slug(file.rsplit('.', 1)[0])}"


@cache
def builtin_files() -> dict[str, str]:
    """``builtin:<name>`` -> file name in resources/logos."""
    m = _manifest()
    files = {
        *m["integrations"].values(),
        *m["providers"].values(),
        *m["extra"].values(),
        *m["glyphs"].values(),
    }
    return {_builtin_id(f): f for f in sorted(files)}


def builtin_bytes(icon_id: str) -> tuple[bytes, str] | None:
    file = builtin_files().get(icon_id)
    if file is None:
        return None
    data = resources.files("autoair.resources").joinpath(f"logos/{file}").read_bytes()
    return data, "image/svg+xml" if file.endswith(".svg") else "image/png"


@cache
def _integration_index() -> list[tuple[str, str]]:
    """(lower-cased integration words, icon id), most specific (longest) first."""
    m = _manifest()
    out: list[tuple[str, str]] = []
    for name, file in m["integrations"].items():
        key = re.sub(
            r"\(.*?\)|^(amazon|aws|google( cloud)?|microsoft( azure)?|apache|atlassian)\s+",
            "",
            name,
            flags=re.I,
        )
        key = key.strip().lower()
        if key and key not in _GENERIC and len(key) >= 3:
            out.append((key, _builtin_id(file)))
    for alias, target in ALIASES.items():
        found = (
            m["extra"].get(target[1:]) if target.startswith("@") else m["integrations"].get(target)
        )
        if found:
            out.append((alias, _builtin_id(found)))
    return sorted(out, key=lambda kv: -len(kv[0]))


def _words(text: str) -> str:
    """``BigQueryInsertJobOperator`` / ``airflow_dbt_python.operators`` -> spaced lower words."""
    spaced = " ".join(_WORD.findall(text.replace("_", " ").replace(".", " ")))
    return f" {spaced.lower()} "


@lru_cache(maxsize=16384)
def infer_icon(
    *, class_name: str, module: str, provider: str, integration: str | None
) -> str | None:
    """The bundled logo that best identifies an operator (None when nothing fits)."""
    m = _manifest()
    if integration:
        for part in [p.strip() for p in integration.split("→")][::-1]:  # transfers: target first
            if part in m["integrations"]:
                return _builtin_id(m["integrations"][part])
    # The class name is the strongest hint (airflow_dbt_python.DbtRunOperator is dbt, not Python).
    for text in (_words(class_name), _words(module)):
        compact = text.replace(" ", "")
        for key, icon in _integration_index():
            if f" {key} " in text or (len(key) >= 5 and key.replace(" ", "") in compact):
                return icon
    file = m["providers"].get(provider)
    if file:
        return _builtin_id(file)
    return glyph_icon(class_name, module)


def normalize_icon_ref(value: str) -> str:
    """Validate an icon reference of the configuration: ``builtin:<logo>`` (must be bundled),
    a bare glyph name (``agent`` -> its ``builtin:`` id), ``custom:<name>`` (an uploaded icon,
    checked when resolved) or ``package:<name>``."""
    text = value.strip()
    if ":" not in text:
        glyphs = _manifest()["glyphs"]
        if text in glyphs:
            return _builtin_id(glyphs[text])
        raise ValueError(
            f"unknown icon {value!r}: use builtin:<logo>, custom:<name> or a glyph name "
            f"({', '.join(sorted(glyphs))})"
        )
    if not _ID_RE.fullmatch(text):
        raise ValueError(f"invalid icon id {value!r} (expected builtin:<name> or custom:<name>)")
    if text.startswith("builtin:") and text not in builtin_files():
        raise ValueError(f"unknown bundled icon {value!r} (GET /api/icons lists them)")
    return text


@lru_cache(maxsize=32)
def _compiled_rules(rules: tuple[tuple[str, str], ...]) -> tuple[tuple[re.Pattern[str], str], ...]:
    return tuple((re.compile(match), icon) for match, icon in rules)


def rule_icon(rules: tuple[tuple[str, str], ...], *, fqn: str, provider: str) -> str | None:
    """The icon of the first ``icons.rules_extra`` rule matching the class path or the
    distribution/provider name."""
    for pattern, icon in _compiled_rules(rules):
        if pattern.search(fqn) or pattern.search(provider):
            return icon
    return None


def glyph_icon(class_name: str, module: str) -> str | None:
    """A concept glyph (robot for agents, clock for time sensors…) when no logo identifies it."""
    glyphs = _manifest()["glyphs"]
    for text in (_words(class_name), _words(module)):
        for pattern, glyph in GLYPH_RULES:
            if pattern.search(text) and glyph in glyphs:
                return _builtin_id(glyphs[glyph])
    return None


# --------------------------------------------------------------------------- custom


def normalize_png(data: bytes) -> bytes:
    """Validate an uploaded image and re-encode it as a small RGBA PNG (no metadata)."""
    from autoair.sections import IconSettings, active

    cfg = active(IconSettings)
    if len(data) > cfg.max_upload_bytes:
        raise IconError(f"icon is larger than {cfg.max_upload_bytes // 1024} KB")
    from PIL import Image, UnidentifiedImageError

    try:
        img = Image.open(io.BytesIO(data))
        if img.format not in {"PNG", "JPEG", "WEBP", "GIF"}:
            raise IconError(
                "upload a PNG, JPEG, WebP or GIF image (SVGs are rasterised in the browser)"
            )
        if img.width * img.height > cfg.max_pixels:
            raise IconError("image is too large")
        img.load()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise IconError(f"not a valid image: {exc}") from exc
    rgba = img.convert("RGBA")
    rgba.thumbnail((cfg.max_side, cfg.max_side))
    out = io.BytesIO()
    rgba.save(out, format="PNG", optimize=True)  # re-encoded: no metadata, no other formats
    return out.getvalue()


class IconStore:
    """Custom icons and icon assignments (fsspec URL: local folder or object storage)."""

    def __init__(
        self,
        url: str | None = None,
        storage_options: dict[str, Any] | None = None,
        *,
        images: DocumentStore | None = None,
        assignments: DocumentStore | None = None,
    ) -> None:
        # Files <url>/<name>.png + <url>/assignments.json by default; rows of the SQL
        # database (PNG bytes in the row) when one is configured.
        if images is None or assignments is None:
            if url is None:
                raise ValueError("IconStore needs a url or document stores")
            images = images or FileDocumentStore(
                url, suffix=".png", storage_options=storage_options
            )
            assignments = assignments or FileDocumentStore(url, storage_options=storage_options)
        self.images: DocumentStore = images
        self.assignment_store: DocumentStore = assignments
        self._lock = threading.Lock()
        self._exists_cache: dict[str, tuple[bool, float]] = {}
        # Icons shipped by operator packages (the operator catalog serves them).
        self.package_icons: PackageIcons | None = None

    def _path(self, name: str) -> str:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,80}", name):
            raise IconError(f"invalid icon name {name!r}")
        return name

    def list(self) -> list[IconInfo]:
        out = [
            IconInfo(id=i, name=i.split(":", 1)[1], kind="builtin", url=f"/icons/{i}")
            for i in builtin_files()
        ]
        try:
            names = sorted(self.images.keys())
        except FileNotFoundError:
            names = []
        for name in names:
            if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,80}", name):
                continue
            out.append(
                IconInfo(id=f"custom:{name}", name=name, kind="custom", url=f"/icons/custom:{name}")
            )
        if self.package_icons is not None:
            try:
                ids = self.package_icons.package_icon_ids()
            except Exception:
                log.warning("cannot list package icons", exc_info=True)
                ids = []
            out += [
                IconInfo(id=i, name=i.split(":", 1)[1], kind="package", url=f"/icons/{i}")
                for i in ids
            ]
        return out

    def save(self, name: str, data: bytes) -> IconInfo:
        name = slug(name)
        png = normalize_png(data)
        self.images.put(self._path(name), png)
        return IconInfo(id=f"custom:{name}", name=name, kind="custom", url=f"/icons/custom:{name}")

    def read(self, icon_id: str) -> tuple[bytes, str] | None:
        if not _ID_RE.fullmatch(icon_id):
            return None
        kind, name = icon_id.split(":", 1)
        if kind == "builtin":
            return builtin_bytes(icon_id)
        if kind == "package":
            svg = self.package_icons.package_icon(icon_id) if self.package_icons else None
            return (svg.encode(), "image/svg+xml") if svg is not None else None
        found = self.images.get(self._path(name))
        if found is None:
            return None
        return found.data, "image/png"

    def delete(self, icon_id: str) -> bool:
        if not icon_id.startswith("custom:"):
            raise IconError("bundled and package icons cannot be deleted")
        if not self.images.delete(self._path(icon_id.split(":", 1)[1])):
            return False

        def drop(a: IconAssignments) -> None:
            a.operators = {k: v for k, v in a.operators.items() if v != icon_id}
            a.providers = {k: v for k, v in a.providers.items() if v != icon_id}

        self._change_assignments(drop)
        return True

    # ------------------------------------------------------------- assignments
    def assignments(self) -> IconAssignments:
        try:
            found = self.assignment_store.get("assignments")
        except FileNotFoundError:
            return IconAssignments()
        except Exception:
            log.warning("ignoring unreadable icon assignments", exc_info=True)
            return IconAssignments()
        return self._parse_assignments(found.data if found is not None else None)

    @staticmethod
    def _parse_assignments(raw: bytes | None) -> IconAssignments:
        if not raw:
            return IconAssignments()
        try:
            return IconAssignments.model_validate_json(raw)
        except ValueError:
            log.warning("ignoring unreadable icon assignments", exc_info=True)
            return IconAssignments()

    def _change_assignments(self, change: Callable[[IconAssignments], None]) -> IconAssignments:
        """Atomic read-modify-write (versioned on SQL: no lost update between replicas);
        readers (every palette request) never see a half-written document."""
        result: list[IconAssignments] = []

        def apply(raw: bytes | None) -> bytes:
            a = self._parse_assignments(raw)
            change(a)
            result[:] = [a]
            return a.model_dump_json(indent=1).encode()

        with self._lock:
            self.assignment_store.update("assignments", apply)
        return result[0]

    def assign(
        self, *, operator: str | None = None, provider: str | None = None, icon: str | None
    ) -> IconAssignments:
        """Set (or clear, ``icon=None``) the default icon of an operator class or provider."""
        if icon is not None and (not _ID_RE.fullmatch(icon) or self.read(icon) is None):
            raise IconError(f"unknown icon {icon!r}")
        if bool(operator) == bool(provider):
            raise IconError("give exactly one of operator or provider")
        key = operator or provider or ""

        def change(a: IconAssignments) -> None:
            target = a.operators if operator else a.providers
            if icon is None:
                target.pop(key, None)
            else:
                target[key] = icon

        return self._change_assignments(change)

    def resolve(
        self,
        *,
        fqn: str,
        class_name: str,
        module: str,
        provider: str,
        integration: str | None,
        assignments: IconAssignments | None = None,
        declared: str | None = None,
        rules: tuple[tuple[str, str], ...] | None = None,
    ) -> str | None:
        """Assignment (operator, then provider) → configured rule (``icons.rules_extra``) →
        the package metadata's ``declared`` icon → inference (logos, then glyphs)."""
        a = assignments or self.assignments()
        if rules is None:
            from autoair.sections import IconSettings, active

            rules = tuple((r.match, r.icon) for r in active(IconSettings).rules_extra)
        configured = rule_icon(rules, fqn=fqn, provider=provider) if rules else None
        if (
            configured is not None
            and configured.startswith("custom:")
            and not self._exists(configured)
        ):
            log.warning("icons.rules_extra names unknown icon %r; ignoring it", configured)
            configured = None
        return (
            a.operators.get(fqn)
            or a.providers.get(provider)
            or configured
            or declared
            or infer_icon(
                class_name=class_name, module=module, provider=provider, integration=integration
            )
        )

    def _exists(self, icon_id: str) -> bool:
        """An uploaded icon exists (memoised briefly: resolved for every palette entry)."""
        now = time.monotonic()
        hit = self._exists_cache.get(icon_id)
        if hit is not None and now - hit[1] < 30:
            return hit[0]
        try:
            found = self.read(icon_id) is not None
        except Exception:
            found = False
        self._exists_cache[icon_id] = (found, now)
        return found


# --------------------------------------------------------------------------- package SVGs

# Allowlist of SVG elements/attributes kept by sanitize_svg (static vector art only: no
# script, foreignObject, external references, animation or event handlers).
_SVG_NS = "http://www.w3.org/2000/svg"
_SVG_ELEMENTS = frozenset(
    {
        "svg", "g", "path", "circle", "ellipse", "rect", "line", "polyline", "polygon",
        "defs", "lineargradient", "radialgradient", "stop", "clippath", "mask", "symbol",
        "use", "title", "desc", "text", "tspan",
    }
)  # fmt: skip
_SVG_ATTRS = frozenset(
    {
        "viewbox", "width", "height", "x", "y", "x1", "y1", "x2", "y2", "cx", "cy", "r", "rx",
        "ry", "d", "points", "fill", "fill-opacity", "fill-rule", "stroke", "stroke-width",
        "stroke-linecap", "stroke-linejoin", "stroke-opacity", "stroke-dasharray",
        "stroke-dashoffset", "stroke-miterlimit", "opacity", "transform", "id", "class",
        "offset", "stop-color", "stop-opacity", "gradientunits", "gradienttransform",
        "spreadmethod", "fx", "fy", "clip-path", "clip-rule", "mask", "clippathunits",
        "maskunits", "preserveaspectratio", "version", "font-size", "font-family",
        "font-weight", "text-anchor", "dominant-baseline", "style", "href",
        "vector-effect", "color", "display", "visibility",
    }
)  # fmt: skip
_URL_REF = re.compile(r"url\(\s*(['\"]?)(.*?)\1\s*\)", re.I)


def _local(tag: str) -> str:
    """Local name (namespace stripped, case kept: SVG names are case-sensitive)."""
    return tag.rsplit("}", 1)[-1]


def _safe_value(value: str) -> bool:
    lowered = value.lower()
    if any(bad in lowered for bad in ("javascript:", "data:", "expression(", "@import", "<")):
        return False
    return all(m.group(2).startswith("#") for m in _URL_REF.finditer(value))


def sanitize_svg(data: bytes | str, max_bytes: int = 64 * 1024) -> str:
    """Re-serialise an SVG keeping only an allowlist of static elements and attributes.

    Raises :class:`IconError` for anything that is not a small, well-formed SVG document
    (DTDs/entities are refused outright, so no entity expansion or external fetch happens).
    """
    import xml.etree.ElementTree as ET

    raw = data.encode() if isinstance(data, str) else data
    if len(raw) > max_bytes:
        raise IconError(f"SVG icon is larger than {max_bytes // 1024} KB")
    text = raw.decode("utf-8", errors="strict") if raw else ""
    if re.search(r"<!(DOCTYPE|ENTITY)", text, re.I):
        raise IconError("SVG icons must not contain a DOCTYPE or entities")
    try:
        root = ET.fromstring(text)  # noqa: S314 - DTDs/entities refused above; no external fetch
    except ET.ParseError as exc:
        raise IconError(f"not a valid SVG document: {exc}") from exc
    if _local(root.tag) != "svg":
        raise IconError("the icon's root element is not <svg>")

    def clean(el: ET.Element) -> ET.Element:
        tag = _local(el.tag)
        out = ET.Element(tag)
        for key, value in el.attrib.items():
            name = _local(key)
            lowered = name.lower()
            if lowered not in _SVG_ATTRS or lowered.startswith("on") or not _safe_value(value):
                continue
            if lowered == "href" and not value.startswith("#"):
                continue  # <use href> to the document itself only
            out.set(name, value)
        out.text = el.text if tag in {"title", "desc", "text", "tspan"} else None
        for child in el:
            if _local(child.tag).lower() in _SVG_ELEMENTS:
                kept = clean(child)
                kept.tail = None
                out.append(kept)
        return out

    cleaned = clean(root)
    cleaned.set("xmlns", _SVG_NS)
    return ET.tostring(cleaned, encoding="unicode")


__all__ = [
    "ICON_ID_PATTERN",
    "IconAssignments",
    "IconError",
    "IconInfo",
    "IconStore",
    "builtin_files",
    "infer_icon",
    "normalize_icon_ref",
    "normalize_png",
    "rule_icon",
    "sanitize_svg",
]
