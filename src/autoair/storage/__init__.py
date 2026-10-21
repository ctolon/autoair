"""Pluggable persistence of specs and generated DAG files ("deploy targets").

* :class:`LocalFolderTarget` — a local folder (``LocalDagBundle`` / ``dags_folder``);
* :class:`ObjectStorageTarget` — S3 / GCS / Azure / ... via fsspec;
* :class:`~autoair.storage.git.GitTarget` — a git repository (push or pull requests).

:class:`FileSystemRepository` is a minimal ``save(spec, python_source)`` facade over a folder.
"""

from __future__ import annotations

from autoair.storage.base import (
    GENERATED_MARKER,
    STUB_MARKER,
    ConflictError,
    DagRepository,
    DeployTarget,
    EjectedError,
    NotFoundError,
    SaveResult,
    SpecSummary,
    StorageError,
    StoredSpec,
    TargetStatus,
    VersionInfo,
    spec_etag,
)
from autoair.storage.config import GitStorageSettings, StorageSettings, build_target
from autoair.storage.local import FileSystemRepository, LocalFolderTarget
from autoair.storage.object import ObjectStorageTarget
from autoair.storage.render import TargetRepository, render_dag_files

__all__ = [
    "GENERATED_MARKER",
    "STUB_MARKER",
    "ConflictError",
    "DagRepository",
    "DeployTarget",
    "EjectedError",
    "FileSystemRepository",
    "GitStorageSettings",
    "LocalFolderTarget",
    "NotFoundError",
    "ObjectStorageTarget",
    "SaveResult",
    "SpecSummary",
    "StorageError",
    "StorageSettings",
    "StoredSpec",
    "TargetRepository",
    "TargetStatus",
    "VersionInfo",
    "build_target",
    "render_dag_files",
    "spec_etag",
]
