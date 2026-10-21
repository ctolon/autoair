"""Repository protocols of AutoAir's persistent state.

Each protocol has a file implementation (JSON and JSON-lines files on fsspec URLs, next to
the domain code) and a SQL implementation (:mod:`autoair.persistence.sql`). The domain classes
(:class:`~autoair.audit.AuditLog`, :class:`~autoair.ai.memory.MemoryStore`…) keep their
logic and API and only call these methods for storage.

==========================  ==============================================  =====================
State                       File implementation                             SQL implementation
==========================  ==============================================  =====================
templates, icons, icon      :class:`~autoair.persistence.documents.         ``SqlDocumentStore``
assignments, knowledge      FileDocumentStore`
overrides/state, user LLM
settings, deploy target
audit trail                 :class:`~autoair.audit.FileAuditRepository`     ``SqlAuditRepository``
AI audit log                :class:`~autoair.ai.audit_log.FileAiAudit…`     ``SqlAiAuditRepository``
AI memory                   :class:`~autoair.ai.memory.FileMemory…`         ``SqlMemoryRepository``
reviews + proposals         :class:`~autoair.iam.proposals.ProposalStore`   ``SqlProposalStore``
==========================  ==============================================  =====================
"""

from __future__ import annotations

import builtins
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from autoair.persistence.documents import DocumentStore

if TYPE_CHECKING:
    from autoair.ai.audit_log import AiAuditRecord
    from autoair.ai.memory import MemoryItem
    from autoair.audit import AuditRecord
    from autoair.iam.proposals import Proposal, ReviewRecord


@runtime_checkable
class AuditRepository(Protocol):
    def append(self, record: AuditRecord) -> None: ...

    def query(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        dag_id: str | None = None,
        user: str | None = None,
        action: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> builtins.list[AuditRecord]:
        """Matching records, newest first (``since``/``until``: ISO timestamps, inclusive /
        exclusive)."""
        ...


@runtime_checkable
class AiAuditRepository(Protocol):
    def append(self, record: AiAuditRecord, line: str) -> None:
        """Store ``record`` (``line`` is its JSON serialisation)."""
        ...

    def recent(
        self, limit: int = 100, user: str | None = None, since: str | None = None
    ) -> builtins.list[AiAuditRecord]: ...

    def prune(self, *, retention_days: int, max_total_mb: int, today: str) -> builtins.list[str]:
        """Apply retention; returns what was removed (day folders / days)."""
        ...


@runtime_checkable
class MemoryRepository(Protocol):
    def all(self) -> builtins.list[MemoryItem]: ...

    def add(self, item: MemoryItem, *, max_items: int) -> MemoryItem:
        """Store ``item`` unless the same text exists in its scope/owner (then that one is
        returned). Raises ``MemoryError`` when ``max_items`` are stored."""
        ...

    def delete(self, item_id: str) -> bool: ...


@runtime_checkable
class ProposalRepository(Protocol):
    ttl: float

    def save(self, proposal: Proposal) -> Proposal: ...
    def get(self, dag_id: str, etag: str) -> Proposal | None: ...
    def pending(self, dag_id: str) -> builtins.list[Proposal]: ...
    def delete(self, dag_id: str, etag: str) -> bool: ...
    def reviews(self, dag_id: str, etag: str | None = None) -> builtins.list[ReviewRecord]: ...
    def add_review(self, dag_id: str, review: ReviewRecord) -> ReviewRecord: ...
    def approvers(self, dag_id: str, etag: str, *, exclude: str | None) -> builtins.list[str]: ...


__all__ = [
    "AiAuditRepository",
    "AuditRepository",
    "DocumentStore",
    "MemoryRepository",
    "ProposalRepository",
]
