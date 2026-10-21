"""Runtime feature-flag overrides (admin panel, ``admin:config``).

Admins switch :class:`~autoair.settings.FeatureFlags` on and off without a restart. The
overrides are one document (``feature-overrides``) of the persistence layer: a file in the
state folder (``database.backend=file``) or a row of the SQL database (sqlite/postgresql,
compare-and-swap writes), so every replica sharing the backend sees them.

Replicas converge by re-reading the document at most every ``features_override_ttl``
seconds (a cheap keyed read); the replica that made the change sees it at once.

Precedence of a flag (highest first; see docs/configuration.md, "Feature flags"):

1. **locked** — listed in ``features_locked`` (fnmatch, ``*`` locks every flag) or set
   through the environment (``AUTOAIR_FEATURES__<NAME>``, ``.env``, secrets directory):
   the configured value always wins and the admin panel cannot change it;
2. the **runtime override** stored here;
3. the configured value (configuration files, defaults).

Derived constraints still apply afterwards (``allow_python_code=false`` keeps Python code
off; a feature whose extra is missing stays off).
"""

from __future__ import annotations

import datetime
import logging
import threading
import time
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from autoair.persistence.documents import DocumentStore

log = logging.getLogger(__name__)

DOCUMENT_KEY = "feature-overrides"


class FeatureOverride(BaseModel):
    value: bool
    user: str | None = None
    reason: str | None = None
    updated_at: str | None = None


class FeatureOverrideDocument(BaseModel):
    overrides: dict[str, FeatureOverride] = Field(default_factory=dict)


class FeatureOverrideStore:
    """The runtime overrides of one deployment (shared by its replicas)."""

    def __init__(self, documents: DocumentStore, *, ttl: float = 5.0) -> None:
        self.documents = documents
        self.ttl = max(0.0, float(ttl))
        self._lock = threading.Lock()
        self._cached: FeatureOverrideDocument | None = None
        self._next_read = 0.0

    # ------------------------------------------------------------------ read
    @staticmethod
    def _parse(raw: bytes | None) -> FeatureOverrideDocument:
        if not raw:
            return FeatureOverrideDocument()
        try:
            return FeatureOverrideDocument.model_validate_json(raw)
        except (ValidationError, ValueError):
            log.warning("ignoring an unreadable feature-override document", exc_info=True)
            return FeatureOverrideDocument()

    def document(self, *, fresh: bool = False) -> FeatureOverrideDocument:
        """The overrides, re-read when the cached copy is older than ``ttl`` seconds. A
        backend error keeps the last known overrides (never fails a request)."""
        now = time.monotonic()
        with self._lock:
            if not fresh and self._cached is not None and now < self._next_read:
                return self._cached
        try:
            found = self.documents.get(DOCUMENT_KEY)
            doc = self._parse(found.data if found is not None else None)
        except FileNotFoundError:
            doc = FeatureOverrideDocument()
        except Exception:
            log.warning("cannot read the feature overrides", exc_info=True)
            with self._lock:
                return self._cached or FeatureOverrideDocument()
        with self._lock:
            self._cached = doc
            self._next_read = now + self.ttl
        return doc

    def values(self) -> dict[str, bool]:
        return {name: o.value for name, o in self.document().overrides.items()}

    # ------------------------------------------------------------------ write
    def set(
        self, name: str, value: bool | None, *, user: str | None, reason: str | None
    ) -> tuple[FeatureOverride | None, FeatureOverride | None]:
        """Set (``value``) or clear (``None``) the override of ``name`` atomically; returns
        (previous, new) override."""
        result: list[FeatureOverride | None] = [None, None]

        def change(raw: bytes | None) -> bytes:
            doc = self._parse(raw)
            result[0] = doc.overrides.get(name)
            if value is None:
                doc.overrides.pop(name, None)
                result[1] = None
            else:
                entry = FeatureOverride(
                    value=value,
                    user=user,
                    reason=reason,
                    updated_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                )
                doc.overrides[name] = entry
                result[1] = entry
            return doc.model_dump_json(indent=1).encode()

        self.documents.update(DOCUMENT_KEY, change)
        self.document(fresh=True)  # this replica sees its own change at once
        return result[0], result[1]


def override_store_for(settings: Any) -> FeatureOverrideStore:
    """The override store of ``settings``' persistence backend."""
    from autoair.persistence import document_store

    return FeatureOverrideStore(
        document_store(settings, "feature_overrides", file_url=str(settings.state_dir)),
        ttl=getattr(settings, "features_override_ttl", 5.0),
    )


__all__ = [
    "DOCUMENT_KEY",
    "FeatureOverride",
    "FeatureOverrideDocument",
    "FeatureOverrideStore",
    "override_store_for",
]
