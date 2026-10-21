"""The AI knowledge base: built-in Markdown, site folders and admin overrides.

Precedence for a document id (the file stem, e.g. ``airflow3-best-practices``):

1. an **override** uploaded by an admin (stored under ``ai.knowledge.store_url``,
   default ``<state>/ai-knowledge``, or in the SQL database when one is configured),
2. a file of the same name in one of ``ai.knowledge.extra_dirs`` (paths or fsspec URLs),
3. the **built-in** file shipped with AutoAir (and AutoAir's ``docs/`` when running from
   a source checkout).

A document is disabled when it is listed in ``ai.knowledge.disabled`` or was switched
off in the UI; switching it on again in the UI wins over the setting. **Reset** deletes
the override and the UI toggles, which restores the configured default.
"""

from __future__ import annotations

import datetime
import json
import logging
import re
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import fsspec
from pydantic import BaseModel, Field

from autoair.ai.config import KnowledgeSettings, ai_settings_of
from autoair.ai.rag import KNOWLEDGE_DIR, Document, markdown_documents
from autoair.persistence.documents import DocumentStore, FileDocumentStore, StoredDocument

log = logging.getLogger("autoair.ai.knowledge")

Origin = Literal["builtin", "extra", "overridden", "custom"]
DOC_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,80}$")
REPO_DOCS = Path(__file__).resolve().parents[3] / "docs"


class KnowledgeError(ValueError):
    pass


class KnowledgeDoc(BaseModel):
    id: str
    title: str
    origin: Origin = Field(
        description="builtin (shipped), extra (a configured folder), overridden (an "
        "uploaded replacement of a builtin/extra doc) or custom (uploaded, no default)."
    )
    enabled: bool
    disabled_by: Literal["settings", "ui"] | None = None
    has_default: bool = Field(description="Reset restores a shipped / configured version.")
    size: int = 0
    updated_at: str | None = None


def _title(doc_id: str, text: str) -> str:
    first = text.lstrip().splitlines()[0] if text.strip() else ""
    return first[2:].strip() if first.startswith("# ") else doc_id.replace("-", " ").title()


def check_id(doc_id: str) -> str:
    doc_id = doc_id.strip().lower().removesuffix(".md")
    if not DOC_ID.match(doc_id):
        raise KnowledgeError(
            "document id must be lower-case letters, digits, '.', '-' or '_' (max 81)"
        )
    return doc_id


class KnowledgeBase:
    def __init__(
        self,
        settings: KnowledgeSettings,
        store_url: str,
        *,
        docs: DocumentStore | None = None,
        state: DocumentStore | None = None,
    ) -> None:
        self.settings = settings
        self.store_url = store_url.rstrip("/")
        # Overrides (<store>/docs/<id>.md) and UI toggles (<store>/state.json): files by
        # default, rows of the SQL database when one is configured.
        self.docs: DocumentStore = docs or FileDocumentStore(f"{self.store_url}/docs", suffix=".md")
        self.state_store: DocumentStore = state or FileDocumentStore(self.store_url)

    # -- sources ---------------------------------------------------------------------------

    def builtin_files(self) -> dict[str, Path]:
        files = {p.stem: p for p in sorted(KNOWLEDGE_DIR.glob("*.md"))}
        if self.settings.include_repo_docs and REPO_DOCS.is_dir():
            for p in sorted(REPO_DOCS.glob("*.md")):
                if p.stem.lower() != "roadmap":
                    files.setdefault(p.stem.lower(), p)
        return files

    def _extra(self) -> dict[str, tuple[Any, str]]:
        out: dict[str, tuple[Any, str]] = {}
        for url in self.settings.extra_dirs:
            try:
                fs, root = fsspec.core.url_to_fs(url)
                for path in sorted(fs.glob(f"{str(root).rstrip('/')}/*.md")):
                    stem = Path(str(path)).stem.lower()
                    if DOC_ID.match(stem):
                        out[stem] = (fs, str(path))
            except Exception:
                log.warning("knowledge folder %s unavailable", url, exc_info=True)
        return out

    def _overrides(self) -> dict[str, str]:
        """Override ids (each maps to itself: read through :attr:`docs`)."""
        try:
            keys = self.docs.keys()
        except (FileNotFoundError, OSError):
            return {}
        return {k: k for k in keys if DOC_ID.match(k)}

    def _override(self, doc_id: str) -> StoredDocument | None:
        try:
            return self.docs.get(doc_id)
        except (OSError, ValueError):
            return None

    @staticmethod
    def _parse_state(raw: bytes | None) -> dict[str, list[str]]:
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        return {
            "disabled": [str(x) for x in data.get("disabled") or []],
            "enabled": [str(x) for x in data.get("enabled") or []],
        }

    def _state(self) -> dict[str, list[str]]:
        try:
            found = self.state_store.get("state")
        except (OSError, ValueError):
            found = None
        return self._parse_state(found.data if found else None)

    def _change_state(self, change: Callable[[dict[str, list[str]]], None]) -> None:
        """Atomic read-modify-write of the UI toggles (no lost update between replicas)."""

        def apply(raw: bytes | None) -> bytes:
            state = self._parse_state(raw)
            change(state)
            return json.dumps({k: sorted(set(v)) for k, v in state.items()}, indent=2).encode()

        self.state_store.update("state", apply)

    def _read(self, fs: Any, path: str) -> str:
        with fs.open(path, "r", encoding="utf-8") as fh:
            text: str = fh.read()
        return text

    def _mtime(self, fs: Any, path: str) -> str | None:
        try:
            info = fs.info(path)
        except (FileNotFoundError, OSError):
            return None
        value = info.get("mtime") or info.get("LastModified") or info.get("created")
        if isinstance(value, (int, float)):
            return datetime.datetime.fromtimestamp(value, datetime.timezone.utc).isoformat()
        return str(value) if value else None

    # -- queries ---------------------------------------------------------------------------

    def _disabled(self, doc_id: str, state: dict[str, list[str]]) -> str | None:
        if doc_id in state["enabled"]:
            return None
        if doc_id in state["disabled"]:
            return "ui"
        if doc_id in self.settings.disabled:
            return "settings"
        return None

    def entries(self) -> list[KnowledgeDoc]:
        builtin = self.builtin_files()
        extra = self._extra()
        overrides = self._overrides()
        state = self._state()
        out: list[KnowledgeDoc] = []
        for doc_id in sorted({*builtin, *extra, *overrides}):
            has_default = doc_id in builtin or doc_id in extra
            updated_at: str | None
            if doc_id in overrides:
                origin: Origin = "overridden" if has_default else "custom"
                stored = self._override(doc_id)
                if stored is None:
                    continue
                text, updated_at = stored.text(), stored.updated_at
            else:
                if doc_id in extra:
                    origin = "extra"
                    fs, path = extra[doc_id]
                else:
                    origin = "builtin"
                    fs, path = fsspec.filesystem("file"), str(builtin[doc_id])
                try:
                    text = self._read(fs, path)
                except OSError:
                    continue
                updated_at = self._mtime(fs, path)
            disabled_by = self._disabled(doc_id, state)
            out.append(
                KnowledgeDoc(
                    id=doc_id,
                    title=_title(doc_id, text),
                    origin=origin,
                    enabled=disabled_by is None,
                    disabled_by=disabled_by,
                    has_default=has_default,
                    size=len(text.encode()),
                    updated_at=updated_at,
                )
            )
        return out

    def content(self, doc_id: str, *, default: bool = False) -> str:
        """Effective text (``default=True``: what a reset would restore)."""
        doc_id = check_id(doc_id)
        if not default:
            stored = self._override(doc_id)
            if stored is not None:
                return stored.text()
        extra = self._extra()
        if doc_id in extra:
            fs, path = extra[doc_id]
            return self._read(fs, path)
        builtin = self.builtin_files().get(doc_id)
        if builtin is not None:
            return builtin.read_text(encoding="utf-8")
        raise KnowledgeError(f"no knowledge document {doc_id!r}")

    # -- changes ---------------------------------------------------------------------------

    def put(self, doc_id: str, text: str) -> KnowledgeDoc:
        doc_id = check_id(doc_id)
        if len(text.encode()) > self.settings.max_upload_bytes:
            raise KnowledgeError(f"document is larger than {self.settings.max_upload_bytes} bytes")
        if not text.strip():
            raise KnowledgeError("document is empty")
        self.docs.put(doc_id, text.encode("utf-8"))
        return self._get(doc_id)

    def set_enabled(self, doc_id: str, enabled: bool) -> KnowledgeDoc:
        doc_id = check_id(doc_id)
        self._get(doc_id)  # must exist
        configured_off = doc_id in self.settings.disabled

        def change(state: dict[str, list[str]]) -> None:
            state["disabled"] = [d for d in state["disabled"] if d != doc_id]
            state["enabled"] = [d for d in state["enabled"] if d != doc_id]
            if enabled and configured_off:
                state["enabled"].append(doc_id)
            elif not enabled:
                state["disabled"].append(doc_id)

        self._change_state(change)
        return self._get(doc_id)

    def reset(self, doc_id: str) -> KnowledgeDoc | None:
        """Delete the override and UI toggles; ``None`` when nothing remains (custom)."""
        doc_id = check_id(doc_id)
        self.docs.delete(doc_id)

        def change(state: dict[str, list[str]]) -> None:
            state["disabled"] = [d for d in state["disabled"] if d != doc_id]
            state["enabled"] = [d for d in state["enabled"] if d != doc_id]

        self._change_state(change)
        return next((d for d in self.entries() if d.id == doc_id), None)

    def _get(self, doc_id: str) -> KnowledgeDoc:
        found = next((d for d in self.entries() if d.id == doc_id), None)
        if found is None:
            raise KnowledgeError(f"no knowledge document {doc_id!r}")
        return found

    # -- retrieval -------------------------------------------------------------------------

    def documents(self, *, chunk_size: int = 1200, chunk_overlap: int = 150) -> list[Document]:
        docs: list[Document] = []
        for item in self.entries():
            if not item.enabled:
                continue
            try:
                text = self.content(item.id)
            except (OSError, KnowledgeError):
                continue
            docs.extend(
                markdown_documents(
                    text=text,
                    doc_id=item.id,
                    chunk_size=chunk_size,
                    chunk_overlap=chunk_overlap,
                    meta={"origin": item.origin},
                )
            )
        return docs


_LOCK = threading.Lock()
_BASES: dict[tuple[str, str], KnowledgeBase] = {}


def knowledge_for(services: Any) -> KnowledgeBase:
    settings = services.settings
    ai = ai_settings_of(settings)
    url = ai.knowledge.store_url or str(Path(settings.state_dir) / "ai-knowledge")
    key = (url, ai.knowledge.model_dump_json())
    from autoair.persistence import document_store, is_sql

    if is_sql(settings):
        key = ("sql:" + url, key[1])
    with _LOCK:
        base = _BASES.get(key)
        if base is None:
            base = _BASES[key] = KnowledgeBase(
                ai.knowledge,
                url,
                docs=document_store(
                    settings, "ai_knowledge_docs", file_url=f"{url.rstrip('/')}/docs", suffix=".md"
                ),
                state=document_store(settings, "ai_knowledge_state", file_url=url),
            )
        return base


__all__ = [
    "DOC_ID",
    "KnowledgeBase",
    "KnowledgeDoc",
    "KnowledgeError",
    "check_id",
    "knowledge_for",
]
