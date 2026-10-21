"""Small filesystem helpers shared by the state stores (templates, icons, settings)."""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write ``path`` so readers see the old or the new content, never a torn file
    (temp file in the same folder + ``os.replace``)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def _is_local(fs: Any) -> bool:
    protocol = fs.protocol if isinstance(fs.protocol, (tuple, list)) else (fs.protocol,)
    return "file" in protocol or "local" in protocol


def fs_write_bytes(fs: Any, path: str, data: bytes) -> None:
    """Write through an fsspec filesystem: atomically on local disks (object stores
    replace objects atomically on their own)."""
    if _is_local(fs):
        atomic_write_bytes(Path(path), data)
        return
    fs.makedirs(path.rsplit("/", 1)[0], exist_ok=True)
    fs.pipe_file(path, data)


__all__ = ["atomic_write_bytes", "atomic_write_text", "fs_write_bytes"]
