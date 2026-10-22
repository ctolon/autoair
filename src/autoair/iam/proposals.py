"""Deploy proposals and spec reviews (four-eyes deploys, ``iam.require_approval``).

When approvals are required and a caller deploys a version nobody else has approved yet, the
deploy route stores the submitted spec as a *proposal* instead of writing it: a reviewer
approves that exact version (its etag) and anyone holding ``deploy`` then deploys it. On git
targets in pull-request mode the pull request is the proposal and nothing is stored here.

Layout under the store's root (any fsspec URL, default ``<state>/proposals``)::

    <dag_id>/proposals/<etag>.json   one proposal (spec + who/when/why + deploy options)
    <dag_id>/reviews.json            the DAG's review verdicts (small, bounded index)

A proposal either deploys its spec (``action="deploy"``, keyed by the spec's etag) or deletes
the DAG (``action="delete"``, keyed by :func:`delete_etag` of the stored spec), so a delete
that goes live passes the same review as a deploy.

Approvals are looked up in that index, never by scanning the audit trail (reviews are
still recorded in the audit as well). Proposals and reviews expire after ``ttl`` seconds,
and a proposal is removed once deployed.

Who saved each stored spec version is recorded as well (:class:`VersionAuthors`, a small
bounded document per DAG in the shared document store, so every replica sees it): a version's
saver cannot approve it, and approvals by its saver never count towards putting it live. A
version saved before AutoAir recorded savers has no record (unknown saver): the deploying user
then counts as its author, as before.

The four-eyes rule itself lives here and nowhere else: :func:`write_mode` (does a write go
live or open a pull request), :func:`needs_approval` (does it need a review) and
:func:`approvers_for` (which reviews count). The deploy and delete routes, the proposal
routes, the admin overview and the MCP middleware all call these.
"""

from __future__ import annotations

import datetime
import json
import logging
import re
import threading
from typing import TYPE_CHECKING, Any, Literal, Protocol

import fsspec
from pydantic import BaseModel, Field

from autoair.spec import DagSpec
from autoair.storage.base import check_dag_id

if TYPE_CHECKING:
    from autoair.iam.config import IamSettings
    from autoair.persistence.documents import DocumentStore

log = logging.getLogger(__name__)

ProposalAction = Literal["deploy", "delete"]
GitWriteMode = Literal["push", "pr"]
DELETE_ETAG_PREFIX = "delete-"
Verdict = Literal["approve", "request_changes", "comment"]
ETAG_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
MAX_REVIEWS_PER_DAG = 500
#: Saved versions remembered per DAG (oldest dropped first: an old version that is
#: deployed again then counts as having an unknown saver).
MAX_VERSION_AUTHORS_PER_DAG = 200


class ProposalError(ValueError):
    pass


class ReviewRecord(BaseModel):
    timestamp: str
    user: str | None
    etag: str
    verdict: str
    comment: str | None = None


class Proposal(BaseModel):
    dag_id: str
    etag: str = Field(description="Etag of the proposed spec (what reviewers approve).")
    spec: DagSpec
    author: str | None = None
    timestamp: str
    message: str | None = None
    base_etag: str | None = Field(
        default=None, description="Version the author edited (deploy conflicts are checked)."
    )
    environment: str | None = None
    force: bool = False
    direct_push: bool | None = None
    action: ProposalAction = Field(
        default="deploy",
        description="'deploy': deploy the proposed spec; 'delete': delete the DAG (the spec "
        "is the stored one being deleted, base_etag its stored version).",
    )


def check_etag(etag: str) -> str:
    if not ETAG_RE.fullmatch(etag):
        raise ProposalError(f"invalid etag {etag!r}")
    return etag


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def now_iso() -> str:
    return _now().isoformat()


def _age(timestamp: str, now: datetime.datetime) -> float:
    try:
        then = datetime.datetime.fromisoformat(timestamp)
    except ValueError:
        return float("inf")
    if then.tzinfo is None:
        then = then.replace(tzinfo=datetime.timezone.utc)
    return (now - then).total_seconds()


def latest_verdicts(reviews: list[ReviewRecord], etag: str) -> dict[str, str]:
    """Each reviewer's latest approve/request_changes verdict on ``etag``."""
    latest: dict[str, str] = {}
    for review in sorted(reviews, key=lambda r: r.timestamp):
        if review.etag == etag and review.user and review.verdict != "comment":
            latest[review.user] = review.verdict
    return latest


def approvers(reviews: list[ReviewRecord], etag: str, *, exclude: str | None) -> list[str]:
    """Users other than ``exclude`` whose latest verdict on ``etag`` is an approval."""
    return sorted(
        user
        for user, verdict in latest_verdicts(reviews, etag).items()
        if verdict == "approve" and user != exclude
    )


def delete_etag(spec: DagSpec) -> str:
    """Key of the proposal (and of its reviews) that deletes the DAG whose stored spec is
    ``spec``: distinct from every deploy etag, and new once the DAG changes."""
    from autoair.storage.base import spec_etag

    return check_etag(DELETE_ETAG_PREFIX + spec_etag(spec))


# --------------------------------------------------------------------------- four-eyes rule
def write_mode(
    target_mode: GitWriteMode | None,
    *,
    direct_push: bool | None,
    allowed: bool,
    iam: IamSettings,
) -> GitWriteMode | None:
    """How a write to a deploy target lands: ``"push"`` (live on the branch), ``"pr"`` (a
    pull request) or ``None`` (not a git target: always live).

    ``direct_push`` is the caller's request flag (``True`` push, ``False`` PR, ``None`` the
    target's mode); ``allowed`` whether the caller holds ``deploy:direct_push``. Callers
    refuse ``direct_push=True`` without ``allowed`` before asking. In push mode,
    ``iam.force_pr_without_direct_push`` turns the writes of everyone else into PRs."""
    if target_mode is None:
        return None
    if direct_push is not None:
        return "push" if direct_push else "pr"
    if target_mode == "push" and not allowed and iam.force_pr_without_direct_push:
        return "pr"
    return target_mode


def needs_approval(iam: IamSettings, mode: GitWriteMode | None) -> bool:
    """``iam.require_approval``: a write that goes live (anything but a pull request, which
    is its own review) needs another user's approval of that exact version."""
    return bool(iam.require_approval) and mode != "pr"


class _ReviewSource(Protocol):
    def get(self, dag_id: str, etag: str) -> Proposal | None: ...

    def approvers(self, dag_id: str, etag: str, *, exclude: str | None) -> list[str]: ...


class SavedBy(BaseModel):
    """Who saved one spec version (``user`` ``None``: an anonymous caller)."""

    etag: str
    user: str | None = None
    timestamp: str


class VersionAuthors:
    """Who saved each stored spec version, shared by every replica.

    One document per DAG (key: the dag_id) in a :class:`DocumentStore` (SQL rows or files)
    holding the newest :data:`MAX_VERSION_AUTHORS_PER_DAG` records, written with an atomic
    read-modify-write. Unreadable or missing records mean "unknown saver"."""

    def __init__(self, store: DocumentStore, *, limit: int = MAX_VERSION_AUTHORS_PER_DAG) -> None:
        self.store = store
        self.limit = limit

    @staticmethod
    def _parse(raw: bytes | None) -> list[SavedBy]:
        try:
            items = json.loads(raw) if raw else []
        except ValueError:
            return []
        out: list[SavedBy] = []
        for item in items if isinstance(items, list) else []:
            try:
                out.append(SavedBy.model_validate(item))
            except ValueError:
                continue
        return out

    def record(self, dag_id: str, etags: list[str], user: str | None) -> None:
        """``user`` saved the versions ``etags`` of ``dag_id`` (the submitted spec and, when
        the deploy stored another one, the stored spec). The first saver of a version is
        kept: saving the same content again later does not make someone else its author."""
        check_dag_id(dag_id)
        wanted = list(dict.fromkeys(check_etag(e) for e in etags if e))
        if not wanted:
            return
        stamp = now_iso()

        def add(raw: bytes | None) -> bytes:
            kept = self._parse(raw)
            known = {r.etag for r in kept}
            kept.extend(
                SavedBy(etag=e, user=user, timestamp=stamp) for e in wanted if e not in known
            )
            return json.dumps([r.model_dump() for r in kept[-self.limit :]]).encode()

        self.store.update(dag_id, add)

    def saved_by(self, dag_id: str, etag: str) -> SavedBy | None:
        """The record of version ``etag`` (``None``: unknown saver)."""
        found = self.store.get(check_dag_id(dag_id))
        for rec in reversed(self._parse(found.data if found is not None else None)):
            if rec.etag == etag:
                return rec
        return None


def _saver(authors: VersionAuthors | None, dag_id: str, etag: str) -> SavedBy | None:
    if authors is None:
        return None
    try:
        return authors.saved_by(dag_id, etag)
    except Exception:  # unreadable: treated as an unknown saver (the deployer is the author)
        log.warning("cannot read who saved %s@%s", dag_id, etag, exc_info=True)
        return None


def approvers_for(
    store: _ReviewSource,
    dag_id: str,
    etag: str,
    *,
    actor: str | None,
    authors: VersionAuthors | None = None,
) -> list[str]:
    """Approvals that let ``actor`` put version ``etag`` live: the latest approve verdicts
    of users other than its author, who is the author of the pending proposal of that
    version, else the user who saved it (``authors``), else ``actor`` (a direct deploy of a
    version whose saver is unknown). The saver's own approval never counts.

    Anonymous callers, proposals without an author and versions saved anonymously never
    pass: "another user" cannot be told apart from the author without a name."""
    if not actor:
        return []
    proposal = store.get(dag_id, etag)
    saved = _saver(authors, dag_id, etag)
    if saved is not None and not saved.user:
        return []
    fallback = saved.user if saved is not None else actor
    author = proposal.author if proposal is not None else fallback
    if not author:
        return []
    found = store.approvers(dag_id, etag, exclude=author)
    return [u for u in found if saved is None or u != saved.user]


def proposal_approvers(
    store: _ReviewSource, proposal: Proposal, authors: VersionAuthors | None = None
) -> list[str]:
    """Approvals of a pending proposal that let anyone holding the needed permission put it
    live (its author included): by users other than the author and than whoever saved that
    version (``authors``); none without an author."""
    if not proposal.author:
        return []
    found = store.approvers(proposal.dag_id, proposal.etag, exclude=proposal.author)
    saved = _saver(authors, proposal.dag_id, proposal.etag) if found else None
    if saved is not None and not saved.user:
        return []
    return [u for u in found if saved is None or u != saved.user]


def may_approve(
    reviewer: str | None, proposal: Proposal | None, saved_by: SavedBy | None = None
) -> bool:
    """Whether ``reviewer`` may approve: never their own proposal, never a version they
    saved themselves (``saved_by``), never anonymously, and never a version saved
    anonymously (its saver cannot be told apart from the reviewer)."""
    if saved_by is not None and (not reviewer or not saved_by.user or saved_by.user == reviewer):
        return False
    if proposal is None:
        return True
    return bool(reviewer) and proposal.author != reviewer


class ProposalStore:
    def __init__(
        self, url: str, *, ttl: float, storage_options: dict[str, Any] | None = None
    ) -> None:
        self.fs, root = fsspec.core.url_to_fs(url, **(storage_options or {}))
        self.root = root.rstrip("/")
        self.ttl = ttl
        self._lock = threading.Lock()

    # ----------------------------------------------------------------- paths
    def _dir(self, dag_id: str) -> str:
        return f"{self.root}/{check_dag_id(dag_id)}"

    def _proposal_path(self, dag_id: str, etag: str) -> str:
        return f"{self._dir(dag_id)}/proposals/{check_etag(etag)}.json"

    def _reviews_path(self, dag_id: str) -> str:
        return f"{self._dir(dag_id)}/reviews.json"

    def _write(self, path: str, data: str) -> None:
        self.fs.makedirs(path.rsplit("/", 1)[0], exist_ok=True)
        with self.fs.open(path, "w", encoding="utf-8") as fh:
            fh.write(data)

    def _expired(self, timestamp: str, now: datetime.datetime | None = None) -> bool:
        return _age(timestamp, now or _now()) > self.ttl

    # ----------------------------------------------------------------- proposals
    def save(self, proposal: Proposal) -> Proposal:
        with self._lock:
            self._write(
                self._proposal_path(proposal.dag_id, proposal.etag),
                proposal.model_dump_json(by_alias=True, exclude_none=True),
            )
        return proposal

    def get(self, dag_id: str, etag: str) -> Proposal | None:
        path = self._proposal_path(dag_id, etag)
        try:
            with self.fs.open(path, "r", encoding="utf-8") as fh:
                proposal = Proposal.model_validate_json(fh.read())
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            log.warning("ignoring unreadable proposal %s", path, exc_info=True)
            return None
        if self._expired(proposal.timestamp):
            self.delete(dag_id, etag)
            return None
        return proposal

    def pending(self, dag_id: str) -> list[Proposal]:
        try:
            paths = self.fs.glob(f"{self._dir(dag_id)}/proposals/*.json")
        except FileNotFoundError:
            return []
        out: list[Proposal] = []
        for path in paths:
            etag = path.rsplit("/", 1)[-1].removesuffix(".json")
            if not ETAG_RE.fullmatch(etag):
                continue
            proposal = self.get(dag_id, etag)
            if proposal is not None:
                out.append(proposal)
        return sorted(out, key=lambda p: p.timestamp, reverse=True)

    def delete(self, dag_id: str, etag: str) -> bool:
        path = self._proposal_path(dag_id, etag)
        with self._lock:
            try:
                self.fs.rm(path)
            except FileNotFoundError:
                return False
            except OSError:
                log.warning("cannot delete proposal %s", path, exc_info=True)
                return False
        return True

    # ----------------------------------------------------------------- reviews
    def _read_reviews(self, dag_id: str) -> list[ReviewRecord]:
        path = self._reviews_path(dag_id)
        try:
            with self.fs.open(path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except FileNotFoundError:
            return []
        except (OSError, ValueError):
            log.warning("ignoring unreadable review index %s", path, exc_info=True)
            return []
        out: list[ReviewRecord] = []
        for item in raw if isinstance(raw, list) else []:
            try:
                out.append(ReviewRecord.model_validate(item))
            except ValueError:
                continue
        now = _now()
        return [r for r in out if not self._expired(r.timestamp, now)]

    def reviews(self, dag_id: str, etag: str | None = None) -> list[ReviewRecord]:
        """Reviews of ``dag_id`` (of one version when ``etag`` is given), newest first."""
        found = self._read_reviews(dag_id)
        if etag is not None:
            found = [r for r in found if r.etag == etag]
        return sorted(found, key=lambda r: r.timestamp, reverse=True)

    def add_review(self, dag_id: str, review: ReviewRecord) -> ReviewRecord:
        check_etag(review.etag)
        with self._lock:
            found = self._read_reviews(dag_id)
            found.append(review)
            found = sorted(found, key=lambda r: r.timestamp)[-MAX_REVIEWS_PER_DAG:]
            self._write(
                self._reviews_path(dag_id),
                json.dumps([r.model_dump() for r in found]),
            )
        return review

    def approvers(self, dag_id: str, etag: str, *, exclude: str | None) -> list[str]:
        return approvers(self._read_reviews(dag_id), etag, exclude=exclude)


def new_proposal(
    spec: DagSpec,
    etag: str,
    *,
    author: str | None,
    message: str | None,
    base_etag: str | None,
    environment: str | None,
    force: bool,
    direct_push: bool | None,
    action: ProposalAction = "deploy",
) -> Proposal:
    return Proposal(
        dag_id=spec.dag_id,
        etag=check_etag(etag),
        spec=spec,
        author=author,
        timestamp=_now().isoformat(),
        message=message,
        base_etag=base_etag,
        environment=environment,
        force=force,
        direct_push=direct_push,
        action=action,
    )
