"""Persisted chat threads of the assistants (developer / admin).

One document per thread in the ``ai_threads`` collection of the persistence layer (files
``<state>/ai-threads/<user hash>.<thread id>.json`` or rows of the SQL database). A thread
holds its messages - with tool calls, citations, follow-ups and proposals - plus the
rolling summary that context compaction produced and the summed token usage.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import threading
import uuid
from typing import Any, Literal

from pydantic import (
    BaseModel,
    Field,
    PrivateAttr,
    SerializationInfo,
    ValidationError,
    field_serializer,
)

from autoair.ai.findings import Finding
from autoair.ai.problems import SpecProblem
from autoair.ai.usage import Usage
from autoair.persistence.documents import DocumentStore
from autoair.spec import DagSpec

Mode = Literal["developer", "admin"]
CitationKind = Literal["doc", "source", "audit", "spec", "web", "log", "config", "tool", "file"]
ProposalStatus = Literal["proposed", "approved", "rejected", "applied"]


_CLOCK_LOCK = threading.Lock()
_last_now: datetime.datetime | None = None


def now_iso() -> str:
    """UTC timestamp, strictly increasing within the process (threads sort and prune by it,
    so two saves in the same microsecond or a wall-clock step back must not reorder them)."""
    global _last_now
    with _CLOCK_LOCK:
        now = datetime.datetime.now(datetime.timezone.utc)
        if _last_now is not None and now <= _last_now:
            now = _last_now + datetime.timedelta(microseconds=1)
        _last_now = now
    return now.isoformat(timespec="microseconds")


def new_id() -> str:
    return uuid.uuid4().hex[:16]


class Citation(BaseModel):
    n: int = Field(description="Number the answer cites as [n].")
    kind: CitationKind
    title: str
    ref: str = Field(description="Path, URL, audit id, log reference or document id.")
    snippet: str = ""


class ToolCallRecord(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    ok: bool = True
    preview: str = ""
    duration_ms: int = 0


class Proposal(BaseModel):
    """A change the assistant proposes; nothing happens until the user approves it."""

    id: str = Field(default_factory=new_id)
    kind: Literal["spec_patch", "config_snippet"]
    title: str
    summary: str = ""
    # spec_patch
    findings: list[Finding] = Field(default_factory=list)
    base_spec: DagSpec | None = Field(default=None, description="The spec the change is on.")
    proposed_spec: DagSpec | None = None
    proposed_ok: bool | None = None
    problems: list[SpecProblem] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)
    # config_snippet
    format: Literal["yaml", "env", "json"] | None = None
    snippet: str | None = None
    status: ProposalStatus = "proposed"
    decided_by: str | None = None
    decided_at: str | None = None


class ChatMessage(BaseModel):
    id: str = Field(default_factory=new_id)
    role: Literal["user", "assistant"]
    content: str
    created_at: str = Field(default_factory=now_iso)
    model: str | None = None
    usage: Usage | None = None
    tools: list[ToolCallRecord] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    follow_ups: list[str] = Field(default_factory=list)
    proposals: list[Proposal] = Field(default_factory=list)
    guardrails: list[str] = Field(default_factory=list)
    stopped: str | None = None
    error: str | None = None
    history: list[dict[str, Any]] | None = Field(
        default=None,
        description="The turn's pydantic-ai messages (stored with the thread, never returned).",
    )

    @field_serializer("history")
    def _history(self, value: list[dict[str, Any]] | None, info: SerializationInfo) -> Any:
        return value if (info.context or {}).get("store") else None


class ThreadSummary(BaseModel):
    id: str
    mode: Mode
    title: str
    created_at: str
    updated_at: str
    message_count: int = 0
    usage: Usage = Field(default_factory=Usage)


class ChatThread(BaseModel):
    _version: str | None = PrivateAttr(default=None)
    id: str = Field(default_factory=new_id)
    user: str
    mode: Mode
    title: str = "New conversation"
    created_at: str = Field(default_factory=now_iso)
    updated_at: str = Field(default_factory=now_iso)
    messages: list[ChatMessage] = Field(default_factory=list)
    summary: str = Field(default="", description="Compaction summary of older messages.")
    summarized: int = Field(
        default=0, description="Leading messages folded into the summary (not sent verbatim)."
    )
    usage: Usage = Field(default_factory=Usage)

    def as_summary(self) -> ThreadSummary:
        return ThreadSummary(
            id=self.id,
            mode=self.mode,
            title=self.title,
            created_at=self.created_at,
            updated_at=self.updated_at,
            message_count=len(self.messages),
            usage=self.usage,
        )

    def proposal(self, proposal_id: str) -> Proposal | None:
        for message in self.messages:
            for p in message.proposals:
                if p.id == proposal_id:
                    return p
        return None


class ThreadNotFound(LookupError):
    pass


def _user_key(user: str) -> str:
    return hashlib.sha256(user.encode()).hexdigest()[:24]


class ThreadStore:
    """Threads per user on a :class:`DocumentStore` (key ``<user hash>.<thread id>``)."""

    def __init__(
        self, documents: DocumentStore, *, max_threads: int = 200, max_messages: int = 400
    ) -> None:
        self.documents = documents
        self.max_threads = max_threads
        self.max_messages = max_messages
        self._lock = threading.Lock()

    def _key(self, user: str, thread_id: str) -> str:
        if not thread_id.isalnum() or len(thread_id) > 40:
            raise ThreadNotFound(thread_id)
        return f"{_user_key(user)}.{thread_id}"

    def _load(self, key: str) -> ChatThread | None:
        doc = self.documents.get(key)
        if doc is None:
            return None
        try:
            thread = ChatThread.model_validate_json(doc.data)
            thread._version = doc.version
            return thread
        except (ValidationError, ValueError):
            return None

    def get(self, user: str, thread_id: str) -> ChatThread:
        thread = self._load(self._key(user, thread_id))
        if thread is None or thread.user != user:
            raise ThreadNotFound(thread_id)
        return thread

    def threads(self, user: str, mode: Mode | None = None) -> list[ChatThread]:
        prefix = f"{_user_key(user)}."
        out: list[ChatThread] = []
        keys = self.documents.keys()
        for key in keys:
            if not key.startswith(prefix):
                continue
            thread = self._load(key)
            if thread is None or thread.user != user:
                continue
            if mode is None or thread.mode == mode:
                out.append(thread)
        out.sort(key=lambda t: t.updated_at, reverse=True)
        return out

    def list(self, user: str, mode: Mode | None = None) -> list[ThreadSummary]:
        return [t.as_summary() for t in self.threads(user, mode)]

    def save(self, thread: ChatThread) -> ChatThread:
        thread.updated_at = now_iso()
        if len(thread.messages) > self.max_messages:
            drop = len(thread.messages) - self.max_messages
            thread.messages = thread.messages[drop:]
            thread.summarized = max(0, thread.summarized - drop)
        data = json.dumps(
            thread.model_dump(mode="json", exclude_none=True, context={"store": True})
        ).encode()
        with self._lock:
            saved = self.documents.put(
                self._key(thread.user, thread.id), data, expected_version=thread._version
            )
            thread._version = saved.version
            self._prune(thread.user, thread.mode, keep=thread.id)
        return thread

    def _prune(self, user: str, mode: Mode, keep: str) -> None:
        threads = self.threads(user, mode)
        for old in threads[self.max_threads :]:
            if old.id != keep:
                self.documents.delete(self._key(user, old.id))

    def rename(self, user: str, thread_id: str, title: str) -> ChatThread:
        thread = self.get(user, thread_id)
        thread.title = title.strip()[:120] or thread.title
        return self.save(thread)

    def delete(self, user: str, thread_id: str) -> bool:
        self.get(user, thread_id)
        return self.documents.delete(self._key(user, thread_id))


_STORES: dict[str, ThreadStore] = {}
_STORES_LOCK = threading.Lock()


def thread_store_for(services: Any) -> ThreadStore:
    from autoair.ai.config import ai_settings_of
    from autoair.persistence import document_store, is_sql

    settings = services.settings
    cfg = ai_settings_of(settings).assistants
    url = cfg.store_url or str(settings.state_dir / "ai-threads")
    backend = settings.database.model_dump_json() if is_sql(settings) else "file"
    identity = f"{backend}:{url}:{cfg.max_threads_per_user}:{cfg.max_messages_per_thread}"
    key = hashlib.sha256(identity.encode()).hexdigest()
    with _STORES_LOCK:
        store = _STORES.get(key)
        if store is None:
            store = _STORES[key] = ThreadStore(
                document_store(settings, "ai_threads", file_url=url),
                max_threads=cfg.max_threads_per_user,
                max_messages=cfg.max_messages_per_thread,
            )
        return store


def title_from(text: str, limit: int = 60) -> str:
    line = " ".join(text.strip().split())
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


__all__ = [
    "ChatMessage",
    "ChatThread",
    "Citation",
    "Mode",
    "Proposal",
    "ThreadNotFound",
    "ThreadStore",
    "ThreadSummary",
    "ToolCallRecord",
    "new_id",
    "now_iso",
    "thread_store_for",
    "title_from",
]
