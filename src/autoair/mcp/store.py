"""Small storage adapter used by the MCP server.

The MCP tools only need list / get / save / delete. :class:`RepositoryStore` adapts an
:class:`autoair.storage.DagRepository`; a richer deploy target can be plugged in
by implementing :class:`SpecStore` (or by exposing compatible methods - see
:func:`store_for`).
"""

from __future__ import annotations

import datetime
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from autoair.spec import DagSpec
from autoair.storage.render import TargetRepository


class StoredDag(BaseModel):
    dag_id: str
    output_mode: str | None = None
    display_name: str | None = None
    description: str | None = None
    tags: list[str] = []
    task_count: int | None = None
    updated_at: datetime.datetime | None = None
    location: str | None = None
    document_type: str | None = Field(
        default=None, description="spec (visual, generated code) or raw (raw code DAG)."
    )


@runtime_checkable
class SpecStore(Protocol):
    def list(self) -> list[StoredDag]: ...
    def get(self, dag_id: str) -> DagSpec | None: ...
    def save(self, spec: DagSpec, python_source: str | None) -> str:
        """Persist the spec (+ generated source); returns a human-readable location."""
        ...

    def delete(self, dag_id: str) -> bool: ...


def _to_stored(item: Any) -> StoredDag:
    if isinstance(item, StoredDag):
        return item
    data = item.model_dump() if isinstance(item, BaseModel) else dict(item)
    if "location" not in data:
        data["location"] = data.get("dag_file") or data.get("path")
    return StoredDag.model_validate(
        {k: v for k, v in data.items() if k in StoredDag.model_fields and v is not None}
    )


class RepositoryStore:
    """Adapter over any object with ``list/get/save/delete`` (e.g. ``FileSystemRepository``)."""

    def __init__(self, repository: Any) -> None:
        self._repo = repository

    def list(self) -> list[StoredDag]:
        return [_to_stored(item) for item in self._repo.list()]

    def get(self, dag_id: str) -> DagSpec | None:
        spec = self._repo.get(dag_id)
        return spec if spec is None or isinstance(spec, DagSpec) else DagSpec.model_validate(spec)

    def save(
        self,
        spec: DagSpec,
        python_source: str | None,
        *,
        base_etag: str | None = None,
        author: str | None = None,
        mode: str | None = None,
    ) -> str:
        """``base_etag``: written only while the stored spec still has this etag (checked by
        the deploy target under its lock; repositories without that check ignore it).
        ``author``/``mode``: the writing user and a git target's write mode (``"pr"``
        opens a pull request), for deploy targets."""
        if isinstance(self._repo, TargetRepository):
            result = self._repo.save(
                spec, python_source, base_etag=base_etag, author=author, mode=mode
            )
        else:
            result = self._repo.save(spec, python_source)
        return str(getattr(result, "location", None) or result)

    def delete(
        self,
        dag_id: str,
        *,
        base_etag: str | None = None,
        author: str | None = None,
        mode: str | None = None,
    ) -> bool:
        """``base_etag``: deleted only while the stored spec still has this etag (checked
        by the deploy target under its lock; repositories without that check ignore it)."""
        if isinstance(self._repo, TargetRepository):
            return bool(self._repo.delete(dag_id, base_etag=base_etag, author=author, mode=mode))
        return bool(self._repo.delete(dag_id))


def store_for(services: Any) -> SpecStore:
    """Pick the storage of a ``Services`` object (``deploy_target`` if present, else
    ``repository``)."""
    target = getattr(services, "deploy_target", None) or getattr(services, "repository", None)
    if target is None:
        raise RuntimeError("services have no repository / deploy target")
    if isinstance(target, RepositoryStore):
        return target
    return RepositoryStore(target)
