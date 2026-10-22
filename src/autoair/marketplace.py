"""Template marketplace: signed indexes of DAG templates, fragments and extensions.

A *source* is an ``index.json`` reachable over HTTP(S) or any fsspec URL (``file://``,
``s3://``…). The index lists entries with the SHA-256 of their payload and is signed with
Ed25519 over its canonical JSON (keys sorted, no whitespace, without ``signature``).
Organisations publish their own index (``autoair marketplace build``) and pin its public
key in ``AUTOAIR_MARKETPLACE_SOURCES``; installs are refused when the signature or a
payload hash does not match.

* ``dag`` / ``fragment`` entries install into the template gallery.
* ``extension`` entries describe pip packages (operators, node kinds, catalog sources):
  they are listed with their install command — AutoAir never installs Python code at
  runtime.
"""

from __future__ import annotations

import base64
import hashlib
import json
import posixpath
import re
import threading
import time
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urljoin, urlparse

import fsspec
import httpx2
from pydantic import BaseModel, Field, ValidationError

from autoair.net import tls
from autoair.templates import TemplateInput, TemplateStore

EntryKind = Literal["dag", "fragment", "extension"]
_ID_RE = r"^[a-z0-9][a-z0-9_-]{0,80}$"
DEFAULT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_TTL = 300.0
DEFAULT_TIMEOUT = 15.0


class MarketplaceError(Exception):
    pass


class MarketplaceSource(BaseModel):
    name: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,64}$")
    url: str = Field(description="URL of index.json (https://, file://, s3://, …).")
    public_key: str | None = Field(
        default=None, description="Base64 raw Ed25519 public key the index must be signed with."
    )
    require_signature: bool = True


class MarketplaceEntry(BaseModel):
    id: str = Field(pattern=_ID_RE)
    kind: EntryKind
    title: str
    description: str = ""
    category: str = "general"
    tags: list[str] = []
    version: str = "1.0.0"
    author: str | None = None
    min_airflow: str | None = None
    requires: list[str] = Field(default_factory=list, description="Provider packages needed.")
    path: str | None = Field(default=None, description="Payload, relative to the index.")
    sha256: str | None = None
    package: str | None = Field(default=None, description="extension: pip requirement.")
    homepage: str | None = None


class MarketplaceIndex(BaseModel):
    format: Literal[1] = 1
    name: str
    description: str = ""
    entries: list[MarketplaceEntry] = []
    signature: str | None = None


class ListedEntry(MarketplaceEntry):
    source: str
    verified: bool
    installed: bool = False


class SourceStatus(BaseModel):
    name: str
    url: str
    ok: bool
    verified: bool
    error: str | None = None
    entries: int = 0


class MarketplaceListing(BaseModel):
    sources: list[SourceStatus]
    entries: list[ListedEntry]


# --------------------------------------------------------------------------- signing


def canonical(index: dict[str, Any]) -> bytes:
    body = {k: v for k, v in index.items() if k != "signature"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def generate_keypair() -> tuple[bytes, str]:
    """(private key PEM, base64 raw public key)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return pem, base64.b64encode(raw).decode()


def sign_index(index: dict[str, Any], private_pem: bytes) -> dict[str, Any]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = serialization.load_pem_private_key(private_pem, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise MarketplaceError("the signing key must be an Ed25519 private key")
    return {**index, "signature": base64.b64encode(key.sign(canonical(index))).decode()}


def verify_index(index: dict[str, Any], public_key_b64: str) -> bool:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    signature = index.get("signature")
    if not isinstance(signature, str):
        return False
    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64))
        key.verify(base64.b64decode(signature), canonical(index))
    except (InvalidSignature, ValueError):
        return False
    return True


def build_index(
    directory: Path, name: str, private_pem: bytes | None, description: str = ""
) -> dict[str, Any]:
    """Index every ``*.json`` template under ``directory/templates`` (+ ``extensions.json``)."""
    entries: list[dict[str, Any]] = []
    for path in sorted((directory / "templates").glob("*.json")):
        raw = path.read_bytes()
        tpl = TemplateInput.model_validate_json(raw)
        meta = json.loads(raw)
        entries.append(
            MarketplaceEntry(
                id=tpl.id,
                kind=tpl.kind,
                title=tpl.title,
                description=tpl.description,
                category=tpl.category,
                tags=tpl.tags,
                version=str(meta.get("version", "1.0.0")),
                author=meta.get("author"),
                min_airflow=meta.get("min_airflow"),
                requires=list(meta.get("requires", [])),
                path=f"templates/{path.name}",
                sha256=hashlib.sha256(raw).hexdigest(),
            ).model_dump(exclude_none=True)
        )
    extensions = directory / "extensions.json"
    if extensions.exists():
        for item in json.loads(extensions.read_text()):
            entries.append(
                MarketplaceEntry.model_validate({**item, "kind": "extension"}).model_dump(
                    exclude_none=True
                )
            )
    index = MarketplaceIndex(name=name, description=description).model_dump(exclude_none=True)
    index["entries"] = entries
    return sign_index(index, private_pem) if private_pem else index


# --------------------------------------------------------------------------- fetching


def _read(url: str, timeout: float = DEFAULT_TIMEOUT, max_bytes: int = DEFAULT_MAX_BYTES) -> bytes:
    scheme = urlparse(url).scheme
    if scheme in ("http", "https"):
        with (
            httpx2.Client(
                timeout=timeout, follow_redirects=True, **tls.httpx_kwargs("marketplace")
            ) as client,
            client.stream("GET", url) as resp,
        ):
            resp.raise_for_status()
            data = bytearray()
            for chunk in resp.iter_bytes():
                data += chunk
                if len(data) > max_bytes:
                    raise MarketplaceError(f"{url}: larger than {max_bytes} bytes")
            return bytes(data)
    with fsspec.open(url, "rb") as fh:
        raw = fh.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise MarketplaceError(f"{url}: larger than {max_bytes} bytes")
    return bytes(raw)


def _resolve(index_url: str, path: str) -> str:
    if re.match(r"^[a-z][a-z0-9+.-]*://", path) or path.startswith("/"):
        raise MarketplaceError(f"entry path {path!r} must be relative to the index")
    if ".." in path.split("/"):
        raise MarketplaceError(f"entry path {path!r} escapes the index location")
    if urlparse(index_url).scheme in ("http", "https"):
        return urljoin(index_url, path)
    return posixpath.join(posixpath.dirname(index_url), path)


class Marketplace:
    def __init__(
        self,
        sources: list[MarketplaceSource],
        templates: TemplateStore,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        cache_ttl: float = DEFAULT_TTL,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        self.sources = {s.name: s for s in sources}
        self.templates = templates
        self.timeout = timeout
        self.cache_ttl = cache_ttl
        self.max_bytes = max_bytes
        self._cache: dict[str, tuple[float, MarketplaceIndex, bool]] = {}
        self._lock = threading.Lock()

    def _index(
        self, source: MarketplaceSource, refresh: bool = False
    ) -> tuple[MarketplaceIndex, bool]:
        with self._lock:
            cached = self._cache.get(source.name)
        if cached and not refresh and time.monotonic() - cached[0] < self.cache_ttl:
            return cached[1], cached[2]
        try:
            raw = json.loads(_read(source.url, self.timeout, self.max_bytes))
        except (OSError, httpx2.HTTPError, ValueError) as exc:
            raise MarketplaceError(f"{source.name}: cannot read index: {exc}") from exc
        if not isinstance(raw, dict):
            raise MarketplaceError(f"{source.name}: index must be a JSON object")
        verified = bool(source.public_key) and verify_index(raw, source.public_key or "")
        if source.require_signature and not verified:
            raise MarketplaceError(
                f"{source.name}: index signature is missing or invalid"
                + ("" if source.public_key else " (no public_key configured)")
            )
        try:
            index = MarketplaceIndex.model_validate(raw)
        except ValidationError as exc:
            raise MarketplaceError(f"{source.name}: invalid index: {exc}") from exc
        with self._lock:
            self._cache[source.name] = (time.monotonic(), index, verified)
        return index, verified

    def listing(self, refresh: bool = False) -> MarketplaceListing:
        installed = {t.id for t in self.templates.list() if not t.builtin}
        statuses: list[SourceStatus] = []
        entries: list[ListedEntry] = []
        for source in self.sources.values():
            try:
                index, verified = self._index(source, refresh)
            except MarketplaceError as exc:
                statuses.append(
                    SourceStatus(
                        name=source.name, url=source.url, ok=False, verified=False, error=str(exc)
                    )
                )
                continue
            statuses.append(
                SourceStatus(
                    name=source.name,
                    url=source.url,
                    ok=True,
                    verified=verified,
                    entries=len(index.entries),
                )
            )
            entries.extend(
                ListedEntry(
                    **e.model_dump(),
                    source=source.name,
                    verified=verified,
                    installed=e.id in installed,
                )
                for e in index.entries
            )
        return MarketplaceListing(sources=statuses, entries=entries)

    def install(self, source_name: str, entry_id: str) -> TemplateInput:
        """:meth:`fetch` the entry and save it to the template store."""
        tpl = self.fetch(source_name, entry_id)
        self.templates.save(tpl)
        return tpl

    def fetch(self, source_name: str, entry_id: str) -> TemplateInput:
        """The entry's template, downloaded and checked against the signed index (not saved:
        callers check who may save it first)."""
        source = self.sources.get(source_name)
        if source is None:
            raise MarketplaceError(f"unknown marketplace source {source_name!r}")
        index, _verified = self._index(source)
        entry = next((e for e in index.entries if e.id == entry_id), None)
        if entry is None:
            raise MarketplaceError(f"{source_name}: no entry {entry_id!r}")
        if entry.kind == "extension":
            raise MarketplaceError(
                f"{entry_id!r} is a Python extension: install it with `pip install {entry.package}`"
            )
        if not entry.path or not entry.sha256:
            raise MarketplaceError(f"{entry_id!r}: entry has no payload path / sha256")
        payload = _read(_resolve(source.url, entry.path), self.timeout, self.max_bytes)
        digest = hashlib.sha256(payload).hexdigest()
        if digest != entry.sha256.lower():
            raise MarketplaceError(f"{entry_id!r}: payload hash mismatch ({digest})")
        try:
            tpl = TemplateInput.model_validate_json(payload)
        except ValidationError as exc:
            raise MarketplaceError(f"{entry_id!r}: invalid template: {exc}") from exc
        if tpl.id != entry.id or tpl.kind != entry.kind:
            raise MarketplaceError(f"{entry_id!r}: payload id/kind does not match the index")
        tags = [*tpl.tags, f"marketplace:{source_name}"]
        return tpl.model_copy(update={"tags": list(dict.fromkeys(tags))})
