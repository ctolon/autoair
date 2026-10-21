"""Deploy proposals and spec reviews (four-eyes deploys, ``iam.require_approval``).

When approvals are required and a caller deploys a version nobody else has approved yet, the
deploy route stores the submitted spec as a *proposal* instead of writing it: a reviewer
approves that exact version (its etag) and anyone holding ``deploy`` then deploys it. On git
targets in pull-request mode the pull request is the proposal and nothing is stored here.

Layout under the store's root (any fsspec URL, default ``<state>/proposals``)::

    <dag_id>/proposals/<etag>.json   one proposal (spec + who/when/why + deploy options)
    <dag_id>/reviews.json            the DAG's review verdicts (small, bounded index)

Approvals are looked up in that index, never by scanning the audit trail (reviews are
still recorded in the audit as well). Proposals and reviews expire after ``ttl`` seconds,
and a proposal is removed once deployed.
"""

from __future__ import annotations

import datetime
import json
import logging
import re
import threading
from typing import Any, Literal

import fsspec
from pydantic import BaseModel, Field

from autoair.spec import DagSpec
from autoair.storage.base import check_dag_id

log = logging.getLogger(__name__)

Verdict = Literal["approve", "request_changes", "comment"]
ETAG_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
MAX_REVIEWS_PER_DAG = 500


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
    )
