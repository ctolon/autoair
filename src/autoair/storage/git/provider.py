"""The small git hosting API a :class:`~autoair.storage.git.target.GitTarget` needs."""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel

from autoair.storage.base import StorageError

_BRANCH_RE = re.compile(r"^(?!/)(?!.*//)(?!.*\.\.)(?!.*@\{)[A-Za-z0-9._/-]{1,200}(?<![/.])$")
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")


class PullRequest(BaseModel):
    url: str
    number: int


@dataclass(frozen=True)
class PullRequestOptions:
    """How pull requests are opened (``AUTOAIR_STORAGE__GIT__PR_*``). Hosts apply what they
    support; labels/reviewers that cannot be set are logged, never fail a deploy."""

    draft: bool = False
    labels: tuple[str, ...] = ()
    reviewers: tuple[str, ...] = ()
    delete_branch: bool = True


class CommitInfo(BaseModel):
    sha: str
    timestamp: datetime.datetime | None = None
    author_name: str | None = None
    author_email: str | None = None
    message: str = ""

    @property
    def author(self) -> str | None:
        if self.author_name and self.author_email:
            return f"{self.author_name} <{self.author_email}>"
        return self.author_name or self.author_email


class GitProvider(Protocol):
    """Read files at a ref, create branches, commit with compare-and-swap, open PRs."""

    name: str
    location: str
    supports_pull_requests: bool

    def get_file(self, ref: str, path: str) -> tuple[str | None, str | None]:
        """``(content, blob_sha)`` of ``path`` at ``ref``; ``(None, None)`` if missing."""
        ...

    def list_files(self, ref: str, prefix: str) -> list[str]:
        """Paths of the files directly inside directory ``prefix`` ("" = repo root)."""
        ...

    def head(self, branch: str) -> str | None:
        """Commit sha the branch points to, ``None`` if it does not exist."""
        ...

    def create_branch(self, name: str, from_sha: str) -> None: ...

    def reset_branch(self, name: str, to_sha: str, *, expect: str | None = None) -> None:
        """Move an existing branch to ``to_sha`` (used to recycle merged PR branches), only
        while it still points at ``expect`` (the head the caller decided on; ``None``: the
        head last seen). Raises :class:`~autoair.storage.base.ConflictError` when it moved,
        so a commit pushed meanwhile is never thrown away."""
        ...

    def commit_files(
        self,
        branch: str,
        base_sha: str | None,
        changes: dict[str, str | None],
        message: str,
        author: str | None,
    ) -> str:
        """Commit ``changes`` (path -> content, ``None`` deletes) on top of ``base_sha``.

        Raises :class:`~autoair.storage.base.ConflictError` if ``branch`` no longer
        points to ``base_sha``. Returns the new commit sha.
        """
        ...

    def open_pr(
        self,
        head: str,
        base: str,
        title: str,
        body: str,
        options: PullRequestOptions | None = None,
    ) -> PullRequest | None: ...

    def find_open_pr(self, head: str) -> PullRequest | None: ...

    def log(self, path: str, ref: str, limit: int = 50) -> list[CommitInfo]: ...


def check_branch(name: str) -> str:
    if not _BRANCH_RE.fullmatch(name) or name.endswith(".lock"):
        raise StorageError(f"invalid branch name {name!r}")
    return name


def check_ref(ref: str) -> str:
    if _SHA_RE.fullmatch(ref):
        return ref
    return check_branch(ref)


def parse_author(author: str | None, default: tuple[str, str]) -> tuple[str, str]:
    """``"Jane <jane@x.io>"`` / ``"jane@x.io"`` / ``"jane"`` -> ``(name, email)``."""
    if not author:
        return default
    match = re.match(r"^\s*(.*?)\s*<([^<>\s]+@[^<>\s]+)>\s*$", author)
    if match:
        return (match.group(1) or match.group(2).split("@")[0], match.group(2))
    author = author.strip()
    if re.match(r"^[^@\s]+@[^@\s]+$", author):
        return (author.split("@")[0], author)
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", author).strip("-") or "user"
    return (author, f"{safe}@users.noreply.autoair")
