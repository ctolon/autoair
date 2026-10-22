"""Persistent AI memory: conventions and preferences per organisation, team or user.

Examples: "Owner is always the team alias data-eng@", "Use the snowflake_prod connection
for warehouse loads", "Prefer deferrable sensors". Memories are injected (bounded) into
the AI features' prompts and exposed to MCP clients (list / add / delete tools).

Stored as one JSON document on any fsspec URL (default ``<state>/ai-memory``; writes are
atomic per process) or, with a SQL ``database.backend``, as rows of ``<prefix>ai_memory``
(a unique constraint makes adding the same text idempotent across replicas). Text that
looks like a credential is rejected.
"""

from __future__ import annotations

import asyncio
import builtins
import datetime
import json
import threading
import uuid
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

from autoair.ai.config import active_ai
from autoair.ai.rag import tokenize
from autoair.ai.rules import SECRET_PATTERNS
from autoair.persistence.documents import FileDocumentStore

if TYPE_CHECKING:
    from autoair.persistence.protocols import MemoryRepository

Scope = Literal["global", "team", "user"]
MemoryKind = Literal["convention", "preference", "fact"]
MAX_ITEMS = 2000
MAX_TEXT = 2000


class MemoryItem(BaseModel):
    id: str
    scope: Scope = "global"
    owner: str | None = Field(default=None, description="Team or user name for scoped memories.")
    kind: MemoryKind = "convention"
    text: str = Field(max_length=MAX_TEXT)
    tags: list[str] = Field(default_factory=list)
    created_at: datetime.datetime
    created_by: str | None = None


class MemoryError(ValueError):
    pass


class FileMemoryRepository:
    """One JSON document ``<url>/memory.json``; changes are atomic read-modify-writes under
    a process lock plus, on local disks, a file lock (several workers on one host)."""

    KEY = "memory"

    def __init__(self, url: str, storage_options: dict[str, Any] | None = None) -> None:
        self.url = url.rstrip("/")
        self.documents = FileDocumentStore(self.url, storage_options=storage_options)

    @staticmethod
    def _parse(raw: bytes | None) -> builtins.list[MemoryItem]:
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:
            return []
        items = data.get("items", []) if isinstance(data, dict) else []
        out: builtins.list[MemoryItem] = []
        for item in items:
            try:
                out.append(MemoryItem.model_validate(item))
            except ValueError:
                continue
        return out

    @staticmethod
    def _dump(items: Sequence[MemoryItem]) -> bytes:
        return json.dumps(
            {"version": 1, "items": [i.model_dump(mode="json") for i in items]}, indent=1
        ).encode()

    def all(self) -> builtins.list[MemoryItem]:
        try:
            found = self.documents.get(self.KEY)
        except OSError:
            return []
        return self._parse(found.data if found is not None else None)

    def add(self, item: MemoryItem, *, max_items: int) -> MemoryItem:
        result: builtins.list[MemoryItem] = []

        def change(raw: bytes | None) -> bytes | None:
            items = self._parse(raw)
            for existing in items:
                if (existing.text, existing.scope, existing.owner) == (
                    item.text,
                    item.scope,
                    item.owner,
                ):
                    result.append(existing)
                    return raw
            if len(items) >= max_items:
                raise MemoryError(f"memory is full ({max_items} items); delete some first")
            result.append(item)
            return self._dump([*items, item])

        self.documents.update(self.KEY, change)
        return result[0]

    def delete(self, item_id: str) -> bool:
        removed: builtins.list[bool] = []

        def change(raw: bytes | None) -> bytes | None:
            items = self._parse(raw)
            kept = [i for i in items if i.id != item_id]
            removed.append(len(kept) != len(items))
            return self._dump(kept) if removed[-1] else raw

        self.documents.update(self.KEY, change)
        return bool(removed and removed[-1])


class MemoryStore:
    def __init__(
        self,
        url: str | None = None,
        storage_options: dict[str, Any] | None = None,
        *,
        repository: MemoryRepository | None = None,
    ) -> None:
        if repository is None:
            if url is None:
                raise ValueError("MemoryStore needs a url or a repository")
            repository = FileMemoryRepository(url, storage_options)
        self.repository = repository
        self.url = getattr(repository, "url", url or "")

    def _load(self) -> builtins.list[MemoryItem]:
        return self.repository.all()

    def list(
        self,
        *,
        scope: Scope | None = None,
        owner: str | None = None,
        query: str | None = None,
        visible_to: tuple[str | None, str | None] | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[builtins.list[MemoryItem], int]:
        """Filtered memories (newest first) and the total before paging.

        ``visible_to=(team, user)`` keeps global memories plus that team's / user's ones.
        """
        items = self._load()
        if scope is not None:
            items = [i for i in items if i.scope == scope]
        if owner is not None:
            items = [i for i in items if i.owner == owner]
        if visible_to is not None:
            team, user = visible_to
            items = [
                i
                for i in items
                if i.scope == "global"
                or (i.scope == "team" and team and i.owner == team)
                or (i.scope == "user" and user and i.owner == user)
            ]
        if query:
            terms = set(tokenize(query))
            items = [i for i in items if terms & set(tokenize(i.text + " " + " ".join(i.tags)))]
        items.sort(key=lambda i: i.created_at, reverse=True)
        return items[offset : offset + limit], len(items)

    def add(
        self,
        text: str,
        *,
        scope: Scope = "global",
        owner: str | None = None,
        kind: MemoryKind = "convention",
        tags: Sequence[str] = (),
        created_by: str | None = None,
    ) -> MemoryItem:
        text = text.strip()
        if not text:
            raise MemoryError("memory text is empty")
        if len(text) > MAX_TEXT:
            raise MemoryError(f"memory text is longer than {MAX_TEXT} characters")
        for _, label, pattern in SECRET_PATTERNS:
            if pattern.search(text):
                raise MemoryError(f"refusing to store what looks like a secret ({label})")
        if scope != "global" and not owner:
            raise MemoryError(f"a {scope} memory needs an owner ({scope} name)")
        limit = active_ai().assist.memory_max_items
        item = MemoryItem(
            id=uuid.uuid4().hex[:12],
            scope=scope,
            owner=owner if scope != "global" else None,
            kind=kind,
            text=text,
            tags=[t.strip() for t in tags if t.strip()][:10],
            created_at=datetime.datetime.now(datetime.timezone.utc),
            created_by=created_by,
        )
        return self.repository.add(item, max_items=limit)

    def get(self, item_id: str) -> MemoryItem | None:
        return next((i for i in self._load() if i.id == item_id), None)

    def delete(self, item_id: str, *, user: str | None = None, admin: bool = True) -> bool:
        """Delete a memory. With ``admin=False`` only ``user``'s own personal memory can go
        (:func:`may_manage`); another's or a shared one raises :class:`MemoryError`. Ids
        are unique and a memory's scope and owner never change, so checking before the
        delete is safe across replicas."""
        if not admin:
            item = self.get(item_id)
            if item is None:
                return False
            if not may_manage(item.scope, item.owner, user=user, admin=False):
                raise MemoryError(
                    "only an admin (admin:config) may delete global or team memories, and "
                    "only its owner a personal one"
                )
        return self.repository.delete(item_id)

    def conventions(
        self, *, team: str | None = None, user: str | None = None, max_chars: int = 3000
    ) -> builtins.list[str]:
        """Bounded list of memory texts for prompts (global, then team, then user)."""
        items, _ = self.list(visible_to=(team, user), limit=MAX_ITEMS)
        order = {"global": 0, "team": 1, "user": 2}
        items.sort(key=lambda i: (order[i.scope], i.created_at))
        out: builtins.list[str] = []
        used = 0
        for item in items:
            line = f"[{item.scope}{':' + item.owner if item.owner else ''}] {item.text}"
            if used + len(line) > max_chars:
                break
            out.append(line)
            used += len(line)
        return out


def may_manage(scope: Scope, owner: str | None, *, user: str | None, admin: bool) -> bool:
    """Whether a caller may add or delete a memory of ``scope`` / ``owner``. Global and team
    memories reach every user's prompts, so only admins (``admin:config``) manage them;
    everyone else only their own personal (``user``) memories."""
    if admin:
        return True
    return scope == "user" and bool(user) and owner == user


_STORES: dict[str, MemoryStore] = {}
_STORES_LOCK = threading.Lock()


def memory_for(services: Any) -> MemoryStore:
    settings = services.settings
    from autoair.ai.config import ai_settings_of
    from autoair.persistence import is_sql

    if is_sql(settings):
        from autoair.persistence.db import database_for
        from autoair.persistence.sql import SqlMemoryRepository

        db = database_for(settings)
        assert db is not None
        key = f"sql:{db.url}:{db.prefix}"
        with _STORES_LOCK:
            if key not in _STORES:
                _STORES[key] = MemoryStore(repository=SqlMemoryRepository(db))
            return _STORES[key]
    url = ai_settings_of(settings).memory_url or str(settings.state_dir / "ai-memory")
    with _STORES_LOCK:
        if url not in _STORES:
            _STORES[url] = MemoryStore(url)
        return _STORES[url]


def conventions_for(services: Any, *, user: str | None = None) -> list[str]:
    try:
        team = getattr(services.settings, "team", None)
        return memory_for(services).conventions(team=team, user=user)
    except Exception:
        return []


async def aconventions_for(services: Any, *, user: str | None = None) -> list[str]:
    """:func:`conventions_for` off the event loop (the memory store does file I/O)."""
    return await asyncio.to_thread(conventions_for, services, user=user)
