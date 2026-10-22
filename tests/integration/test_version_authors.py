"""Four-eyes and who saved a version: nobody approves a spec version they saved themselves,
and the saver's own approval never counts towards putting it live (iam.require_approval).

The saver of every stored version is recorded in the shared document store (file or SQL), so
every replica applies the rule; versions saved before AutoAir recorded savers have an unknown
saver and keep the earlier rule (the deploying user counts as the author)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from autoair.iam.proposals import (
    ProposalStore,
    ReviewRecord,
    VersionAuthors,
    approvers_for,
    may_approve,
    new_proposal,
    now_iso,
    proposal_approvers,
)
from autoair.persistence import FileDocumentStore
from autoair.spec import DagSpec
from autoair.storage import spec_etag
from tests.db_backends import BACKENDS, database_settings, drop_tables
from tests.integration.test_iam_api import SIMPLE, as_role, deploy, make_client, make_services

pytestmark = pytest.mark.airflow

ETAG = spec_etag(DagSpec.model_validate(SIMPLE))


@pytest.fixture(params=BACKENDS)
def backend(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.fixture
def replicas(tmp_path: Path, backend: str) -> Iterator[tuple[TestClient, TestClient, Any]]:
    """Two replicas sharing the state (folder or database); require_approval toggled per
    test through the returned services' settings."""
    db = database_settings(backend, tmp_path)
    one = make_services(tmp_path, database=db)
    two = make_services(tmp_path, database=db)
    try:
        yield make_client(one), make_client(two), (one, two)
    finally:
        drop_tables(one.settings)


def _approve(c: TestClient, user: str, etag: str = ETAG) -> int:
    return c.post(
        "/api/specs/simple/reviews",
        json={"etag": etag, "verdict": "approve"},
        headers=as_role("Reviewer", user),
    ).status_code


def _strict(services: Any) -> None:
    for svc in services:
        svc.settings.iam.require_approval = True


def test_saver_cannot_approve_or_count_on_any_replica(
    replicas: tuple[TestClient, TestClient, Any],
) -> None:
    a, b, services = replicas
    # alice saves the version while approvals are not required yet (or in a pull request)
    saved = a.put("/api/specs/simple", json={"spec": SIMPLE}, headers=as_role("Deployer", "alice"))
    assert saved.status_code == 200, saved.text
    _strict(services)
    # on the other replica alice cannot approve the version she saved
    assert _approve(b, "alice") == 403
    # an approval of hers recorded anyway (e.g. while require_approval was off) never counts
    services[1].proposals.add_review(
        "simple", ReviewRecord(timestamp=now_iso(), user="alice", etag=ETAG, verdict="approve")
    )
    proposed = b.put(
        "/api/specs/simple",
        json={"spec": SIMPLE, "base_etag": ETAG},
        headers=as_role("Deployer", "carol"),
    )
    assert proposed.status_code == 202, proposed.text  # alice's approval did not count
    # carol proposed it, but alice saved it: alice still cannot approve; bob can
    assert _approve(a, "alice") == 403
    assert _approve(a, "bob") == 200
    # bob's approval lets anyone holding deploy put it live, its saver included
    live = a.put(
        "/api/specs/simple",
        json={"spec": SIMPLE, "base_etag": ETAG},
        headers=as_role("Deployer", "alice"),
    )
    assert live.status_code == 200, live.text
    assert live.json()["deployed"] is True


def test_unknown_saver_keeps_the_earlier_rule(tmp_path: Path) -> None:
    """A version saved before savers were recorded: the deployer counts as its author, so
    another user's approval lets them deploy it (and theirs alone never does)."""
    services = make_services(tmp_path)
    c = make_client(services)
    assert deploy(c, "Deployer").status_code == 200
    authors = services.version_authors
    assert authors is not None
    assert authors.saved_by("simple", ETAG) is not None
    authors.store.delete("simple")  # as stored by an older AutoAir
    services.settings.iam.require_approval = True
    assert _approve(c, "rita") == 200  # no saver known: approving is allowed
    redeploy = c.put(
        "/api/specs/simple",
        json={"spec": SIMPLE, "base_etag": ETAG},
        headers=as_role("Deployer", "rita"),
    )
    assert redeploy.status_code == 202, redeploy.text  # rita's own approval does not count
    assert _approve(c, "dave") == 200
    again = c.put(
        "/api/specs/simple",
        json={"spec": SIMPLE, "base_etag": ETAG},
        headers=as_role("Deployer", "rita"),
    )
    assert again.status_code == 200, again.text


def test_approved_proposals_record_their_author_as_the_saver(tmp_path: Path) -> None:
    services = make_services(tmp_path, iam={"require_approval": True})
    c = make_client(services)
    proposed = deploy(c, "Deployer")  # sub "deployer"
    assert proposed.status_code == 202
    assert _approve(c, "rita") == 200
    done = c.post(f"/api/specs/simple/proposals/{ETAG}/deploy", headers=as_role("Deployer", "erin"))
    assert done.status_code == 200, done.text
    assert services.version_authors is not None
    saved = services.version_authors.saved_by("simple", ETAG)
    assert saved is not None
    assert saved.user == "deployer"


def test_rules_of_the_helpers(tmp_path: Path) -> None:
    store = ProposalStore(str(tmp_path / "proposals"), ttl=3600)
    authors = VersionAuthors(FileDocumentStore(str(tmp_path / "authors")))
    spec = DagSpec.model_validate(SIMPLE)

    def approve(user: str) -> None:
        store.add_review(
            "simple", ReviewRecord(timestamp=now_iso(), user=user, etag=ETAG, verdict="approve")
        )

    approve("alice")
    approve("bob")
    # unknown saver: the actor is the author
    assert approvers_for(store, "simple", ETAG, actor="alice", authors=authors) == ["bob"]
    authors.record("simple", [ETAG], "alice")
    authors.record("simple", [ETAG], "mallory")  # the first saver is kept
    saved = authors.saved_by("simple", ETAG)
    assert saved is not None
    assert saved.user == "alice"
    assert approvers_for(store, "simple", ETAG, actor="carol", authors=authors) == ["bob"]
    assert not may_approve("alice", None, saved)
    assert may_approve("bob", None, saved)
    proposal = new_proposal(
        spec,
        ETAG,
        author="carol",
        message=None,
        base_etag=None,
        environment=None,
        force=False,
        direct_push=None,
    )
    store.save(proposal)
    assert proposal_approvers(store, proposal, authors) == ["bob"]
    # a version saved anonymously can never satisfy four-eyes
    other = spec_etag(spec.model_copy(update={"description": "anonymous"}))
    authors.record("simple", [other], None)
    store.add_review(
        "simple", ReviewRecord(timestamp=now_iso(), user="bob", etag=other, verdict="approve")
    )
    assert approvers_for(store, "simple", other, actor="carol", authors=authors) == []
    anonymous = authors.saved_by("simple", other)
    assert not may_approve("bob", None, anonymous)  # pointless: it would never count
