"""Read-only, sandboxed file access for the troubleshooting agent.

Only files below the allow-listed roots (``ai.troubleshoot.filesystem.roots``, optionally
the DAGs folder) can be listed or read:

* paths are resolved (symlinks followed, ``..`` collapsed) and must stay inside a root,
  so neither traversal nor a symlink pointing elsewhere escapes;
* the final open uses ``O_NOFOLLOW`` and re-checks the opened file (no swap between the
  check and the read);
* every path component is matched against ``deny_globs`` (keys, ``.env``, configs…) and the
  file name against ``allow_globs``;
* files larger than ``max_file_bytes`` are refused, reads return at most
  ``max_read_chars`` (head, tail, a line window or grep matches);
* contents are scrubbed of secrets before the model sees them.
"""

from __future__ import annotations

import fnmatch
import os
import re
import stat
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from autoair.ai.config import TroubleshootFilesystem


class SandboxError(PermissionError):
    pass


class FsListInput(BaseModel):
    path: str = Field(default="", description="Directory (absolute or relative to a root).")
    pattern: str = Field(default="*", max_length=200, description="fnmatch filter, e.g. *.log")


class FsReadInput(BaseModel):
    path: str = Field(description="File (absolute or relative to a root).")
    mode: Literal["tail", "head", "lines", "grep"] = "tail"
    start_line: int = Field(default=1, ge=1, description="mode=lines: first line.")
    line_count: int = Field(default=200, ge=1, le=5_000, description="mode=lines/head/tail.")
    pattern: str | None = Field(
        default=None, max_length=300, description="mode=grep: regular expression."
    )


class FsEntry(BaseModel):
    path: str
    kind: Literal["file", "dir"]
    size: int | None = None
    modified: float | None = None


class FsSandbox:
    def __init__(
        self,
        settings: TroubleshootFilesystem,
        roots: Sequence[Path],
        scrub: Callable[[str], str] = lambda s: s,
    ) -> None:
        self.settings = settings
        resolved: list[Path] = []
        for root in roots:
            try:
                path = Path(root).expanduser().resolve(strict=True)
            except OSError:
                continue
            if path.is_dir() and path not in resolved:
                resolved.append(path)
        self.roots = resolved
        self.scrub = scrub

    @property
    def enabled(self) -> bool:
        return bool(self.roots)

    # ----------------------------------------------------------------- checks

    def _denied(self, path: Path, root: Path) -> str | None:
        rel = path.relative_to(root)
        for part in rel.parts:
            for pattern in self.settings.deny_globs:
                if fnmatch.fnmatch(part.lower(), pattern.lower()):
                    return part
        return None

    def resolve(self, raw: str) -> tuple[Path, Path]:
        """(resolved path, its root); raises :class:`SandboxError` outside the sandbox."""
        if not self.roots:
            raise SandboxError("filesystem access is not enabled")
        if "\x00" in raw:
            raise SandboxError("invalid path")
        candidates = []
        given = Path(raw.strip() or ".").expanduser()
        if given.is_absolute():
            candidates.append(given)
        else:
            candidates.extend(root / given for root in self.roots)
        for candidate in candidates:
            try:
                path = candidate.resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            for root in self.roots:
                if path == root or path.is_relative_to(root):
                    denied = self._denied(path, root) if path != root else None
                    if denied:
                        raise SandboxError(f"{denied!r} is on the deny list")
                    return path, root
        raise SandboxError(f"{raw!r} is not inside an allowed folder (or does not exist)")

    def _readable_name(self, path: Path) -> bool:
        return any(fnmatch.fnmatch(path.name, p) for p in self.settings.allow_globs)

    # ----------------------------------------------------------------- operations

    def list(self, raw: str = "", pattern: str = "*") -> dict[str, object]:
        if not raw.strip() and len(self.roots) > 1:
            return {"roots": [str(r) for r in self.roots]}
        path, root = self.resolve(raw or str(self.roots[0]))
        if not path.is_dir():
            raise SandboxError(f"{raw!r} is not a directory")
        entries: list[FsEntry] = []
        truncated = False
        for child in sorted(path.iterdir(), key=lambda p: p.name):
            if child.is_symlink():
                try:
                    target = child.resolve(strict=True)
                except OSError:
                    continue
                if not (target == root or target.is_relative_to(root)):
                    continue  # never list links that point outside
            if self._denied(child, root):
                continue
            is_dir = child.is_dir()
            if not is_dir and not fnmatch.fnmatch(child.name, pattern or "*"):
                continue
            if len(entries) >= self.settings.max_list_entries:
                truncated = True
                break
            try:
                st = child.stat()
            except OSError:
                continue
            entries.append(
                FsEntry(
                    path=str(child),
                    kind="dir" if is_dir else "file",
                    size=None if is_dir else st.st_size,
                    modified=st.st_mtime,
                )
            )
        return {
            "path": str(path),
            "entries": [e.model_dump(exclude_none=True) for e in entries],
            "truncated": truncated,
        }

    def _open_text(self, path: Path) -> str:
        if not stat.S_ISREG(os.stat(path, follow_symlinks=False).st_mode):
            raise SandboxError("not a regular file")
        # O_NONBLOCK: a FIFO swapped in after the check cannot block the reader.
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        fd = os.open(path, flags)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise SandboxError("not a regular file")
            if st.st_size > self.settings.max_file_bytes:
                raise SandboxError(
                    f"file is larger than {self.settings.max_file_bytes} bytes; "
                    "ask an operator for the relevant part"
                )
            with os.fdopen(fd, "rb", closefd=False) as fh:
                data = fh.read(self.settings.max_file_bytes)
        finally:
            os.close(fd)
        return data.decode("utf-8", errors="replace")

    def read(self, args: FsReadInput) -> dict[str, object]:
        path, _ = self.resolve(args.path)
        if path.is_dir():
            raise SandboxError(f"{args.path!r} is a directory (use fs_list)")
        if not self._readable_name(path):
            raise SandboxError(f"{path.name!r} is not an allowed file name")
        lines = self._open_text(path).splitlines()
        limit = self.settings.max_read_chars
        numbered: list[tuple[int, str]]
        if args.mode == "head":
            numbered = list(enumerate(lines[: args.line_count], 1))
        elif args.mode == "lines":
            start = args.start_line - 1
            numbered = list(enumerate(lines[start : start + args.line_count], args.start_line))
        elif args.mode == "grep":
            if not args.pattern:
                raise SandboxError("mode=grep needs a pattern")
            try:
                rx = re.compile(args.pattern, re.IGNORECASE)
            except re.error as exc:
                raise SandboxError(f"invalid pattern: {exc}") from exc
            numbered = [(i, line) for i, line in enumerate(lines, 1) if rx.search(line)]
            numbered = numbered[:500]
        else:
            first = max(0, len(lines) - args.line_count)
            numbered = list(enumerate(lines[first:], first + 1))
        out: list[str] = []
        used = 0
        truncated = False
        for n, line in numbered:
            row = f"{n:>6}: {line[:2_000]}"
            if used + len(row) > limit:
                truncated = True
                break
            out.append(row)
            used += len(row) + 1
        return {
            "path": str(path),
            "total_lines": len(lines),
            "text": self.scrub("\n".join(out)),
            "truncated": truncated,
        }


def sandbox_for(services: object, fs: TroubleshootFilesystem) -> FsSandbox:
    from autoair.ai.guardrails import pipeline_for

    roots = list(fs.roots)
    if fs.include_dags_folder:
        dags = getattr(getattr(services, "settings", None), "dags_folder", None)
        if dags:
            roots.append(Path(dags))
    from autoair.ai.rules import scrub_secrets

    pipeline = pipeline_for(services)
    return FsSandbox(fs, roots, scrub=lambda text: pipeline.sanitize(scrub_secrets(text)))


__all__ = ["FsListInput", "FsReadInput", "FsSandbox", "SandboxError", "sandbox_for"]
