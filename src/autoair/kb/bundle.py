"""Portable knowledge-base bundles: build on a connected machine, import air-gapped.

A bundle is a zip file with two members:

* ``pages.jsonl`` - one parsed page per line (metadata + sections, no chunks or vectors:
  the importer chunks and embeds with its own settings and model);
* ``manifest.json`` - format, creation time, AutoAir version, the units it covers with
  their fingerprints, page counts and the SHA-256 and size of ``pages.jsonl``; optionally
  an Ed25519 **signature** over the canonical JSON of the manifest (the marketplace's
  signing scheme: :func:`autoair.marketplace.sign_index`).

Import always verifies the checksum and sizes; with ``kb.bundle.public_keys`` (or
``require_signature``) the signature must verify against one of the keys. Only the two
known members are read (never extracted to disk), and every size is capped.
"""

from __future__ import annotations

import datetime
import hashlib
import io
import json
import tempfile
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any

from pydantic import ValidationError

from autoair.kb.config import KbSourceConfig
from autoair.kb.ingest import decode_body
from autoair.kb.models import ImportReport, PageDoc, SourceRun, page_id

if TYPE_CHECKING:
    from autoair.kb.ingest import Ingestor
    from autoair.kb.service import KnowledgeBase

FORMAT = 1
KIND = "autoair-kb-bundle"
MANIFEST = "manifest.json"
PAGES = "pages.jsonl"
#: Largest manifest accepted (bytes): it lists units, not pages.
_MAX_MANIFEST = 16 * 1024 * 1024
#: Longest line of pages.jsonl (one page) accepted (bytes).
_MAX_LINE = 64 * 1024 * 1024


class BundleError(ValueError):
    """The bundle is malformed, too large, tampered with or not signed as required."""


def export_bundle(
    kb: KnowledgeBase,
    out: IO[bytes],
    *,
    sources: list[str] | None = None,
    private_key_pem: bytes | None = None,
    description: str = "",
) -> dict[str, Any]:
    """Write the stored pages (all, or of ``sources``) as a bundle to ``out``."""
    from autoair import __version__

    store = kb.store
    store.open()
    digest = hashlib.sha256()
    size = 0
    units: dict[str, dict[str, Any]] = {}
    count = 0
    with tempfile.TemporaryFile() as tmp:
        for record, body in store.iter_bodies():
            if sources and record.source not in sources:
                continue
            doc = PageDoc.model_validate(
                {**record.model_dump(exclude={"chunks"}), "sections": decode_body(body)}
            )
            line = doc.model_dump_json(exclude_none=True).encode() + b"\n"
            digest.update(line)
            size += len(line)
            tmp.write(line)
            count += 1
            unit = units.setdefault(
                record.unit,
                {
                    "key": record.unit,
                    "source": record.source,
                    "component": record.component,
                    "package": record.package,
                    "version": record.version,
                    "pages": 0,
                    "fingerprint": store.get_meta(f"unit:{record.unit}"),
                },
            )
            unit["pages"] += 1
        manifest: dict[str, Any] = {
            "format": FORMAT,
            "kind": KIND,
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(
                timespec="seconds"
            ),
            "generator": f"autoair {__version__}",
            "description": description,
            "pages": count,
            "units": sorted(units.values(), key=lambda u: str(u["key"])),
            "files": {PAGES: {"sha256": digest.hexdigest(), "size": size}},
        }
        if private_key_pem:
            from autoair.marketplace import sign_index

            manifest = sign_index(manifest, private_key_pem)
        tmp.seek(0)
        with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            zf.writestr(MANIFEST, json.dumps(manifest, indent=2, sort_keys=True))
            with zf.open(PAGES, "w", force_zip64=True) as member:
                while chunk := tmp.read(1024 * 1024):
                    member.write(chunk)
    return manifest


def _member(zf: zipfile.ZipFile, name: str, limit: int) -> zipfile.ZipInfo:
    try:
        info = zf.getinfo(name)
    except KeyError as exc:
        raise BundleError(f"not a knowledge-base bundle: {name} is missing") from exc
    if info.file_size > limit:
        raise BundleError(f"{name} is larger than {limit} bytes")
    return info


def read_manifest(zf: zipfile.ZipFile) -> dict[str, Any]:
    info = _member(zf, MANIFEST, _MAX_MANIFEST)
    try:
        manifest = json.loads(zf.read(info))
    except (ValueError, zipfile.BadZipFile) as exc:
        raise BundleError(f"unreadable manifest: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("kind") != KIND:
        raise BundleError("not a knowledge-base bundle (manifest kind)")
    if manifest.get("format") != FORMAT:
        raise BundleError(f"unsupported bundle format {manifest.get('format')!r}")
    return manifest


def verify(manifest: dict[str, Any], public_keys: list[str], required: bool) -> tuple[bool, bool]:
    """(signed, verified); raises when a signature is required and does not verify."""
    signed = isinstance(manifest.get("signature"), str)
    if not public_keys and not required:
        return signed, False
    if not public_keys:
        raise BundleError(
            "kb.bundle.require_signature is set but no kb.bundle.public_keys are configured"
        )
    if not signed:
        raise BundleError("the bundle is not signed (kb.bundle.public_keys is set)")
    from autoair.marketplace import verify_index

    if not any(verify_index(manifest, key) for key in public_keys):
        raise BundleError("the bundle signature does not match kb.bundle.public_keys")
    return True, True


def iter_pages(
    zf: zipfile.ZipFile, manifest: dict[str, Any], *, max_bytes: int, max_pages: int
) -> Iterator[PageDoc]:
    """Pages of a bundle. The checksum is verified in a first pass, before any page is
    returned: a tampered bundle imports nothing."""
    expected = (manifest.get("files") or {}).get(PAGES) or {}
    info = _member(zf, PAGES, max_bytes)
    if int(expected.get("size", -1)) != info.file_size:
        raise BundleError("pages.jsonl size does not match the manifest")
    digest = hashlib.sha256()
    with zf.open(info) as fh:
        while chunk := fh.read(1024 * 1024):
            digest.update(chunk)
    if digest.hexdigest() != expected.get("sha256"):
        raise BundleError("pages.jsonl checksum does not match the manifest (tampered?)")
    count = 0
    with zf.open(info) as fh:
        while True:
            line = fh.readline(_MAX_LINE + 1)
            if not line:
                return
            if len(line) > _MAX_LINE:
                raise BundleError("a page of the bundle is too large")
            if not line.strip():
                continue
            count += 1
            if count > max_pages:
                raise BundleError(f"the bundle has more than {max_pages} pages")
            try:
                yield PageDoc.model_validate_json(line)
            except ValidationError as exc:
                raise BundleError(f"invalid page in the bundle: {exc.errors()[0]['msg']}") from exc


def _import(
    ing: Ingestor,
    zf: zipfile.ZipFile,
    source_id: str,
    result: SourceRun,
    report: ImportReport,
    fingerprint_key: str | None,
) -> None:
    kb = ing.kb
    cfg = kb.kb.bundle
    manifest = read_manifest(zf)
    report.signed, report.signature_verified = verify(
        manifest, cfg.public_keys, cfg.require_signature
    )
    store = ing.store
    fingerprint = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    if fingerprint_key and store.get_meta(fingerprint_key) == fingerprint:
        existing = store.pages(source_id)
        result.pages_seen = result.pages_unchanged = report.pages = report.unchanged = len(existing)
        return
    existing = store.pages(source_id)
    seen: set[str] = set()
    sources: set[str] = set()
    total = int(manifest.get("pages") or 0)
    ing.progress(phase="importing", unit=None, done=0, total=total, force=True)
    for doc in iter_pages(zf, manifest, max_bytes=cfg.max_bytes, max_pages=cfg.max_pages):
        sources.add(doc.source)
        pid = page_id(source_id, doc.url)
        doc = doc.model_copy(
            update={"id": pid, "source": source_id, "unit": f"{source_id}:{doc.unit}"}
        )
        seen.add(pid)
        report.pages += 1
        result.pages_seen += 1
        prior = existing.get(pid)
        if prior is not None and prior.content_hash == doc.content_hash and doc.content_hash:
            report.unchanged += 1
            result.pages_unchanged += 1
        else:
            written = ing.write_page(doc)
            report.chunks += written
            report.changed += 1
            result.pages_changed += 1
            result.chunks_written += written
        ing.progress(done=report.pages)
    removed = [pid for pid in existing if pid not in seen]
    report.removed = result.pages_removed = store.delete_pages(removed) if removed else 0
    report.sources = sorted(sources)
    if fingerprint_key:
        store.set_meta(fingerprint_key, fingerprint)


def _open_zip(fileobj: IO[bytes]) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(fileobj)
    except zipfile.BadZipFile as exc:
        raise BundleError(f"not a zip file: {exc}") from exc


def import_bundle(
    kb: KnowledgeBase, path: Path | IO[bytes], *, source_id: str = "bundle"
) -> tuple[ImportReport, Any]:
    """Import a bundle file under the ingestion lease (CLI / admin panel)."""
    from autoair.kb.ingest import Ingestor

    report = ImportReport()
    if isinstance(path, Path) and path.stat().st_size > kb.kb.bundle.max_bytes:
        raise BundleError(f"the bundle is larger than {kb.kb.bundle.max_bytes} bytes")

    def work(ing: Ingestor) -> None:
        assert ing._run is not None
        result = SourceRun(source=source_id)
        ing._run.status.sources.append(result)
        handle: IO[bytes] = path.open("rb") if isinstance(path, Path) else path
        try:
            with _open_zip(handle) as zf:
                _import(ing, zf, source_id, result, report, None)
        finally:
            if isinstance(path, Path):
                handle.close()

    status = Ingestor(kb).run("import", work=work)
    if status.outcome != "ok":
        detail = next((e.message for e in reversed(status.errors)), status.outcome)
        raise BundleError(f"import failed: {detail}")
    return report, status


def import_bundle_source(ing: Ingestor, source: KbSourceConfig, result: SourceRun) -> None:
    """A ``bundle`` source (file path or object storage, read with its conn_id)."""
    from autoair.kb.fetch import open_fs

    assert source.url is not None
    parent, _, name = source.url.rstrip("/").rpartition("/")
    fs, root = open_fs(parent or ".", source.conn_id, source.storage_options)
    path = f"{root}/{name}" if root else name
    size = fs.size(path)
    if size is not None and size > ing.kb.kb.bundle.max_bytes:
        raise BundleError(f"the bundle is larger than {ing.kb.kb.bundle.max_bytes} bytes")
    report = ImportReport()
    with tempfile.TemporaryFile() as tmp:
        with fs.open(path, "rb") as fh:
            while chunk := fh.read(1024 * 1024):
                tmp.write(chunk)
        tmp.seek(0)
        with _open_zip(tmp) as zf:
            _import(ing, zf, source.id, result, report, f"bundle:{source.id}")


def bundle_bytes(kb: KnowledgeBase, **kwargs: Any) -> bytes:
    buf = io.BytesIO()
    export_bundle(kb, buf, **kwargs)
    return buf.getvalue()


__all__ = [
    "BundleError",
    "bundle_bytes",
    "export_bundle",
    "import_bundle",
    "import_bundle_source",
    "iter_pages",
    "read_manifest",
    "verify",
]
