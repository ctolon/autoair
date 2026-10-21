"""Spec document migrations.

Every stored ``*.autoair.json`` carries ``spec_version``. Migrators upgrade raw documents
step by step (``1 -> 2 -> ...``) *before* Pydantic validation, so old specs keep loading
after schema changes. Add a function to ``MIGRATIONS`` for every version bump.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

Document = dict[str, Any]


def _v1_to_v2(doc: Document) -> Document:
    # v2 only added optional fields; v1 documents are structurally valid v2 documents.
    return {**doc, "spec_version": 2}


MIGRATIONS: dict[int, Callable[[Document], Document]] = {1: _v1_to_v2}
LATEST = max(MIGRATIONS) + 1


class SpecMigrationError(ValueError):
    pass


def migrate(doc: Document) -> Document:
    """Upgrade a raw spec document to :data:`LATEST`."""
    version = doc.get("spec_version", 1)
    if not isinstance(version, int):
        raise SpecMigrationError(f"invalid spec_version {version!r}")
    if version > LATEST:
        raise SpecMigrationError(
            f"spec_version {version} is newer than this AutoAir supports ({LATEST}); "
            "upgrade AutoAir"
        )
    while version < LATEST:
        doc = MIGRATIONS[version](doc)
        version = doc["spec_version"]
    return doc
