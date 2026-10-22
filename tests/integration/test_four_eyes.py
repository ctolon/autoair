"""Four-eyes governance (iam.require_approval, iam.force_pr_without_direct_push) applies to
every write that changes what Airflow runs: deploys, deletes, and their MCP counterparts.

Regression tests for the 0.2.3 review: a DAG delete went straight to the branch (no proposal,
no forced pull request), and the approval rule was computed differently by the deploy route,
the proposal routes, the admin overview and the MCP middleware."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import httpx2
import pytest
from fastapi.testclient import TestClient

from autoair.api.app import build_services, create_app
from autoair.api.routers.admin import proposals_overview
from autoair.auth import TokenVerifier, bearer_passthrough
from autoair.iam.config import IamSettings
from autoair.iam.proposals import (
    ProposalStore,
    ReviewRecord,
    approvers_for,
    delete_etag,
    needs_approval,
    new_proposal,
    now_iso,
    write_mode,
)
from autoair.settings import Settings
from autoair.spec import DagSpec
from autoair.storage import spec_etag
from autoair.storage.config import GitStorageSettings, StorageSettings
from tests.integration.test_iam_api import (
    AIRFLOW,
    SIMPLE,
    airflow_ok,
    as_role,
    deploy,
    git_client,
    git_repo,  # noqa: F401 - fixture
    make_client,
    make_services,
)

pytestmark = pytest.mark.airflow


def _files_on(repo: Path, branch: str) -> set[str]:
    out = subprocess.run(
        ["git", "--git-dir", str(repo), "ls-tree", "-r", "--name-only", branch],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return {Path(p).name for p in out.split()}


def _branches(repo: Path) -> set[str]:
    out = subprocess.run(
        ["git", "--git-dir", str(repo), "branch", "--format=%(refname:short)"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return set(out.split())


def _approve(c: TestClient, etag: str, user: str = "rita") -> httpx2.Response:
    return c.post(
        "/api/specs/simple/reviews",
        json={"etag": etag, "verdict": "approve"},
        headers=as_role("Reviewer", user),
    )


def _deployed_with_approval(c: TestClient) -> None:
    proposed = deploy(c, "Deployer")
    assert proposed.status_code == 202, proposed.text
    etag = proposed.json()["proposal_etag"]
    assert _approve(c, etag).status_code == 200
    done = c.post(f"/api/specs/simple/proposals/{etag}/deploy", headers=as_role("Deployer"))
    assert done.status_code == 200, done.text


# --------------------------------------------------------------------------- REST delete
def _delete(c: TestClient, *who: str) -> Any:
    """DELETE the DAG with the etag a viewer loads now (If-Match, like the UI sends)."""
    stored = c.get("/api/specs/simple", headers=as_role("Viewer"))
    headers = as_role(*who)
    if stored.status_code == 200:
        headers["If-Match"] = stored.json()["etag"]
    return c.delete("/api/specs/simple", headers=headers)


def test_delete_needs_an_approval_under_four_eyes(tmp_path: Path) -> None:
    c = make_client(make_services(tmp_path, iam={"require_approval": True}))
    _deployed_with_approval(c)
    dag_file = tmp_path / "dags" / "simple.py"
    assert dag_file.exists()

    proposed = _delete(c, "Deployer")
    assert proposed.status_code == 202, proposed.text
    body = proposed.json()
    assert body["deleted"] is False
    assert body["status"] == "pending_approval"
    assert dag_file.exists()  # nothing went live
    etag = body["proposal_etag"]
    stored = c.get("/api/specs/simple", headers=as_role("Viewer")).json()
    assert etag == delete_etag(DagSpec.model_validate(stored["spec"]))

    listed = c.get("/api/specs/simple/proposals", headers=as_role("Viewer")).json()
    assert [(p["etag"], p["action"], p["author"]) for p in listed] == [(etag, "delete", "deployer")]
    url = f"/api/specs/simple/proposals/{etag}"
    # not approved yet; the author cannot approve their own delete
    assert c.post(f"{url}/delete", headers=as_role("Deployer")).status_code == 403
    assert _approve(c, etag, "deployer").status_code == 403
    # a delete proposal is not deployed as a spec
    assert _approve(c, etag).status_code == 200
    assert c.post(f"{url}/deploy", headers=as_role("Deployer")).status_code == 409
    # executing needs spec:delete and deploy
    assert c.post(f"{url}/delete", headers=as_role("Reviewer", "rita")).status_code == 403
    done = c.post(f"{url}/delete", headers=as_role("Deployer"))
    assert done.status_code == 200, done.text
    assert done.json()["deleted"] is True
    assert done.json()["status"] == "deleted"
    assert not dag_file.exists()
    assert c.get(url, headers=as_role("Viewer")).status_code == 404
    audit = c.get("/api/specs/simple/audit", headers=as_role("Admin")).json()
    assert audit[0]["action"] == "delete"
    assert audit[0]["details"]["approved_by"] == ["rita"]
    assert audit[0]["details"]["proposed_by"] == "deployer"


def test_direct_delete_of_an_approved_delete_goes_live(tmp_path: Path) -> None:
    c = make_client(make_services(tmp_path, iam={"require_approval": True}))
    _deployed_with_approval(c)
    etag = _delete(c, "Deployer").json()["proposal_etag"]
    assert _approve(c, etag).status_code == 200
    done = _delete(c, "Admin", "bob")
    assert done.status_code == 200, done.text
    assert done.json()["deleted"] is True
    assert c.get("/api/specs/simple/proposals", headers=as_role("Viewer")).json() == []


def test_delete_without_four_eyes_stays_direct(tmp_path: Path) -> None:
    c = make_client(make_services(tmp_path))
    assert deploy(c, "Deployer").status_code == 200
    done = _delete(c, "Deployer")
    assert done.status_code == 200, done.text
    assert done.json() == {
        "deleted": True,
        "status": "deleted",
        "proposal_etag": None,
        "message": None,
    }
    gone = _delete(c, "Deployer")
    assert gone.json()["deleted"] is False
    assert gone.json()["status"] == "not_found"


def test_live_delete_needs_deploy(tmp_path: Path) -> None:
    pruner = {"roles": {"Pruner": ["builder:use", "spec:read", "spec:delete"]}}
    c = make_client(make_services(tmp_path, iam=pruner))
    assert deploy(c, "Deployer").status_code == 200
    denied = _delete(c, "Pruner")
    assert denied.status_code == 403
    assert "'deploy'" in denied.json()["detail"]
    assert (tmp_path / "dags" / "simple.py").exists()


def test_delete_follows_forced_pull_requests(tmp_path: Path, git_repo: Path) -> None:  # noqa: F811
    c = git_client(tmp_path, git_repo, "push", force_pr_without_direct_push=True)
    assert deploy(c, "Admin").json()["branch"] == "main"
    assert "simple.py" in _files_on(git_repo, "main")
    resp = _delete(c, "Deployer")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "pr_opened"
    assert "simple.py" in _files_on(git_repo, "main")  # still live until the PR merges
    assert "autoair/simple" in _branches(git_repo)
    # a user holding deploy:direct_push still deletes on the branch
    assert _delete(c, "Admin").json()["status"] == "deleted"
    assert "simple.py" not in _files_on(git_repo, "main")


def test_delete_in_pr_mode_is_exempt_from_proposals(tmp_path: Path, git_repo: Path) -> None:  # noqa: F811
    c = git_client(tmp_path, git_repo, "pr", require_approval=True)
    etag = deploy(c, "Author").json()["etag"]
    # the admin pushes it live (needs an approval: four-eyes)
    pushed = deploy(c, "Admin", direct_push=True, base_etag=etag)
    assert pushed.status_code == 202
    assert _approve(c, pushed.json()["proposal_etag"]).status_code == 200
    assert deploy(c, "Admin", direct_push=True, base_etag=etag).status_code == 200
    assert "simple.py" in _files_on(git_repo, "main")
    resp = _delete(c, "Deployer")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "pr_opened"
    assert c.get("/api/specs/simple/proposals", headers=as_role("Viewer")).json() == []


# --------------------------------------------------------------------------- stale proposals
def _proposal_store(services: Any) -> ProposalStore:
    from autoair.api.routers.specs import _store

    store = _store(services)
    assert isinstance(store, ProposalStore)
    return store


def _stored_etag(c: TestClient) -> str:
    return str(c.get("/api/specs/simple", headers=as_role("Viewer")).json()["etag"])


def test_a_proposal_the_stored_spec_moved_past_is_refused(tmp_path: Path) -> None:
    c = make_client(make_services(tmp_path, iam={"require_approval": True}))
    _deployed_with_approval(c)
    base = _stored_etag(c)
    first = deploy(c, "Deployer", spec={**SIMPLE, "description": "v2"}, base_etag=base)
    assert first.status_code == 202, first.text
    stale_etag = first.json()["proposal_etag"]
    assert _approve(c, stale_etag).status_code == 200
    # another approved version goes live first
    other = deploy(c, "Deployer", spec={**SIMPLE, "description": "v3"}, base_etag=base)
    other_etag = other.json()["proposal_etag"]
    assert _approve(c, other_etag).status_code == 200
    url = "/api/specs/simple/proposals"
    assert c.post(f"{url}/{other_etag}/deploy", headers=as_role("Deployer")).status_code == 200
    now = _stored_etag(c)

    detail = c.get(f"{url}/{stale_etag}", headers=as_role("Viewer")).json()
    assert detail["stale"] is True
    refused = c.post(f"{url}/{stale_etag}/deploy", headers=as_role("Deployer"))
    assert refused.status_code == 409, refused.text
    assert "changed since this proposal was made" in refused.json()["detail"]
    assert now in refused.json()["detail"]
    assert _approve(c, stale_etag, "rob").status_code == 409
    assert _stored_etag(c) == now  # v3 stays
    spec = c.get("/api/specs/simple", headers=as_role("Viewer")).json()["spec"]
    assert spec["description"] == "v3"


def test_a_proposal_without_base_etag_for_an_existing_dag_must_be_proposed_again(
    tmp_path: Path,
) -> None:
    """Proposals stored before they recorded their base version (or made for a DAG that did
    not exist yet) cannot be told apart from a blind overwrite once the DAG exists."""
    services = make_services(tmp_path, iam={"require_approval": True})
    c = make_client(services)
    _deployed_with_approval(c)
    store = _proposal_store(services)
    legacy_spec = DagSpec.model_validate({**SIMPLE, "description": "legacy"})
    etag = spec_etag(legacy_spec)
    store.save(
        new_proposal(
            legacy_spec,
            etag,
            author="deployer",
            message=None,
            base_etag=None,
            environment=None,
            force=False,
            direct_push=None,
        )
    )
    store.add_review(
        "simple", ReviewRecord(timestamp=now_iso(), user="rita", etag=etag, verdict="approve")
    )
    url = f"/api/specs/simple/proposals/{etag}"
    assert c.get(url, headers=as_role("Viewer")).json()["stale"] is True
    refused = c.post(f"{url}/deploy", headers=as_role("Deployer"))
    assert refused.status_code == 409, refused.text
    assert "propose this version again" in refused.json()["detail"]
    assert _approve(c, etag, "rob").status_code == 409
    spec = c.get("/api/specs/simple", headers=as_role("Viewer")).json()["spec"]
    assert spec.get("description") != "legacy"


def test_a_stale_delete_proposal_is_refused(tmp_path: Path) -> None:
    c = make_client(make_services(tmp_path, iam={"require_approval": True}))
    _deployed_with_approval(c)
    proposed = _delete(c, "Deployer")
    assert proposed.status_code == 202, proposed.text
    delete_key = proposed.json()["proposal_etag"]
    assert _approve(c, delete_key).status_code == 200
    base = _stored_etag(c)
    other = deploy(c, "Deployer", spec={**SIMPLE, "description": "v3"}, base_etag=base)
    other_etag = other.json()["proposal_etag"]
    assert _approve(c, other_etag).status_code == 200
    url = "/api/specs/simple/proposals"
    assert c.post(f"{url}/{other_etag}/deploy", headers=as_role("Deployer")).status_code == 200
    refused = c.post(f"{url}/{delete_key}/delete", headers=as_role("Deployer"))
    assert refused.status_code == 409, refused.text
    assert "propose deleting this DAG again" in refused.json()["detail"]
    assert (tmp_path / "dags" / "simple.py").exists()


def test_delete_needs_the_loaded_etag(tmp_path: Path) -> None:
    c = make_client(make_services(tmp_path))
    assert deploy(c, "Deployer").status_code == 200
    etag = _stored_etag(c)
    blind = c.delete("/api/specs/simple", headers=as_role("Deployer"))
    assert blind.status_code == 428, blind.text
    assert etag in blind.json()["detail"]
    assert blind.headers["etag"] == f'"{etag}"'
    stale = c.delete("/api/specs/simple", headers={**as_role("Deployer"), "If-Match": "0" * 64})
    assert stale.status_code == 409, stale.text
    assert (tmp_path / "dags" / "simple.py").exists()
    done = c.delete("/api/specs/simple", headers={**as_role("Deployer"), "If-Match": f'"{etag}"'})
    assert done.status_code == 200, done.text
    assert done.json()["deleted"] is True
    # nothing stored: no etag needed (not found)
    gone = c.delete("/api/specs/simple", headers=as_role("Deployer"))
    assert gone.status_code == 200
    assert gone.json()["status"] == "not_found"


def test_a_blind_delete_is_refused_before_it_is_proposed(tmp_path: Path) -> None:
    c = make_client(make_services(tmp_path, iam={"require_approval": True}))
    _deployed_with_approval(c)
    blind = c.delete("/api/specs/simple", headers=as_role("Deployer"))
    assert blind.status_code == 428, blind.text
    assert c.get("/api/specs/simple/proposals", headers=as_role("Viewer")).json() == []


def test_setting_relaxes_the_delete_etag(tmp_path: Path) -> None:
    c = make_client(make_services(tmp_path, storage={"require_base_etag": False}))
    assert deploy(c, "Deployer").status_code == 200
    done = c.delete("/api/specs/simple", headers=as_role("Deployer"))
    assert done.status_code == 200, done.text
    assert done.json()["deleted"] is True


# --------------------------------------------------------------------------- one rule
@pytest.mark.parametrize(
    ("require", "mode", "expected"),
    [
        (False, None, False),
        (False, "push", False),
        (False, "pr", False),
        (True, None, True),  # local folder / object storage: always live
        (True, "push", True),
        (True, "pr", False),  # the pull request is the proposal
    ],
)
def test_needs_approval_matrix(require: bool, mode: Any, expected: bool) -> None:
    assert needs_approval(IamSettings(require_approval=require), mode) is expected


@pytest.mark.parametrize(
    ("target", "flag", "allowed", "force", "expected"),
    [
        (None, None, False, True, None),  # not git: always live
        ("push", None, False, False, "push"),
        ("push", None, False, True, "pr"),  # forced pull request
        ("push", None, True, True, "push"),  # direct-push holders keep pushing
        ("push", False, True, False, "pr"),  # asking for a PR is always allowed
        ("pr", None, True, True, "pr"),
        ("pr", True, True, False, "push"),
    ],
)
def test_write_mode_matrix(
    target: Any, flag: bool | None, allowed: bool, force: bool, expected: Any
) -> None:
    iam = IamSettings(force_pr_without_direct_push=force)
    assert write_mode(target, direct_push=flag, allowed=allowed, iam=iam) == expected


def _review(store: ProposalStore, etag: str, user: str | None, verdict: str = "approve") -> None:
    store.add_review(
        "simple", ReviewRecord(timestamp=now_iso(), user=user, etag=etag, verdict=verdict)
    )


@pytest.mark.parametrize(
    ("author", "actor", "reviewers", "expected"),
    [
        ("alice", "alice", ["bob"], ["bob"]),  # the author deploys after another approval
        ("alice", "carol", ["alice"], []),  # self-approval of the author never counts
        ("alice", "carol", ["bob", "alice"], ["bob"]),
        (None, "carol", ["bob"], []),  # an anonymous proposal cannot be approved
        ("alice", None, ["bob"], []),  # an anonymous caller cannot use approvals
        (None, "alice", ["bob"], ["bob"]),  # no proposal: the caller is the author
        (None, "alice", ["alice"], []),
    ],
)
def test_approvers_matrix(
    tmp_path: Path,
    author: str | None,
    actor: str | None,
    reviewers: list[str],
    expected: list[str],
) -> None:
    store = ProposalStore(str(tmp_path / "p"), ttl=3600)
    spec = DagSpec.model_validate(SIMPLE)
    etag = spec_etag(spec)
    has_proposal = not (author is None and actor == "alice")
    if has_proposal:
        store.save(
            new_proposal(
                spec,
                etag,
                author=author,
                message=None,
                base_etag=None,
                environment=None,
                force=False,
                direct_push=None,
            )
        )
    for user in reviewers:
        _review(store, etag, user)
    assert approvers_for(store, "simple", etag, actor=actor) == expected


def test_changes_requested_withdraws_an_approval(tmp_path: Path) -> None:
    store = ProposalStore(str(tmp_path / "p"), ttl=3600)
    _review(store, "abc", "bob")
    _review(store, "abc", "bob", "request_changes")
    assert approvers_for(store, "simple", "abc", actor="alice") == []


def test_admin_overview_follows_the_iam_switch(tmp_path: Path) -> None:
    """features.iam off turns four-eyes off (iam_without_roles): the admin overview must
    say so, like /api/me and the deploy route."""
    settings = Settings(  # type: ignore[call-arg]
        dags_folder=tmp_path / "dags",
        subfolder="",
        ai={"enabled": False},
        airflow_url=AIRFLOW,
        iam=IamSettings(require_approval=True),
        features={"iam": False},
    )
    verifier = TokenVerifier(AIRFLOW, transport=httpx2.MockTransport(airflow_ok))
    services = build_services(settings, bearer_passthrough(verifier))
    create_app(services)
    assert proposals_overview(services).require_approval is False


def test_admin_overview_counts_approvals_like_the_deploy_route(tmp_path: Path) -> None:
    services = make_services(tmp_path, iam={"require_approval": True})
    c = make_client(services)
    etag = deploy(c, "Deployer").json()["proposal_etag"]
    _approve(c, etag)
    _approve(c, etag, "deployer")  # refused (own proposal), never counted
    rows = proposals_overview(services).proposals
    assert [(r.etag, r.action, r.approvals) for r in rows] == [(etag, "deploy", ["rita"])]


# --------------------------------------------------------------------------- MCP
def _mcp_client(tmp_path: Path, storage: StorageSettings | None = None, **iam: Any) -> TestClient:
    pytest.importorskip("fastmcp")
    extra: dict[str, Any] = {"storage": storage} if storage is not None else {}
    settings = Settings(  # type: ignore[call-arg]
        dags_folder=tmp_path / "dags",
        subfolder="",
        airflow_url=AIRFLOW,
        ai={"enabled": False},
        import_check_on_deploy=False,
        iam=IamSettings(strategies=["role_mapping"], **iam),
        **extra,
    )
    verifier = TokenVerifier(AIRFLOW, transport=httpx2.MockTransport(airflow_ok))
    return TestClient(
        create_app(build_services(settings, bearer_passthrough(verifier))),
        raise_server_exceptions=False,
    )


def _mcp(c: TestClient, role: str, name: str, args: dict[str, Any], user: str | None = None) -> Any:
    headers = {
        **as_role(role, user),
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "mcp-protocol-version": "2025-06-18",
    }
    resp = c.post(
        "/mcp/",
        headers=headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": args},
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["result"]


def _git_storage(tmp_path: Path, repo: Path, mode: str) -> StorageSettings:
    return StorageSettings(
        mode="git",
        git=GitStorageSettings(
            provider="local", repo=f"file://{repo}", mode=mode, cache_dir=tmp_path / "cache"
        ),  # type: ignore[arg-type]
    )


def _out(result: Any) -> dict[str, Any]:
    assert not result["isError"], result
    out: dict[str, Any] = result["structuredContent"]
    return out


def test_mcp_save_is_proposed_under_four_eyes(tmp_path: Path) -> None:
    """Like PUT /specs: a save that would go live without another user's approval is stored
    as a deploy proposal; once someone else approved it, the save goes live."""
    c = _mcp_client(tmp_path, require_approval=True)
    dag_file = tmp_path / "dags" / "simple.py"
    spec = {"spec": SIMPLE, "confirm": True}
    dry = _out(_mcp(c, "Deployer", "save_dag_spec", {"spec": SIMPLE}))
    assert "proposal" in dry["message"]
    proposed = _out(_mcp(c, "Deployer", "save_dag_spec", spec))
    assert (proposed["saved"], proposed["status"]) == (False, "pending_approval")
    etag = proposed["proposal_etag"]
    assert etag == spec_etag(DagSpec.model_validate(SIMPLE))
    assert not dag_file.exists()
    listed = c.get("/api/specs/simple/proposals", headers=as_role("Viewer")).json()
    assert [(p["etag"], p["author"]) for p in listed] == [(etag, "deployer")]
    # saving again keeps the proposal (and its author); the author cannot approve it
    again = _out(_mcp(c, "Deployer", "save_dag_spec", spec))
    assert again["status"] == "pending_approval"
    assert _approve(c, etag, "deployer").status_code == 403
    assert _approve(c, etag).status_code == 200
    saved = _out(_mcp(c, "Deployer", "save_dag_spec", spec))
    assert (saved["saved"], saved["status"]) == (True, "saved"), saved
    assert dag_file.exists()
    assert c.get("/api/specs/simple/proposals", headers=as_role("Viewer")).json() == []
    audit = c.get("/api/specs/simple/audit", headers=as_role("Admin")).json()
    assert any(r["action"] == "propose" and r["details"].get("via") == "mcp" for r in audit)


def test_mcp_delete_is_proposed_under_four_eyes(tmp_path: Path) -> None:
    plain = _mcp_client(tmp_path)
    assert not _mcp(plain, "Admin", "save_dag_spec", {"spec": SIMPLE, "confirm": True})["isError"]
    c = _mcp_client(tmp_path, require_approval=True)
    dag_file = tmp_path / "dags" / "simple.py"
    # a dry run still answers
    current = _out(_mcp(c, "Admin", "delete_dag_spec", {"dag_id": "simple"}))["current_etag"]
    args = {"dag_id": "simple", "confirm": True, "base_etag": current}
    proposed = _out(_mcp(c, "Deployer", "delete_dag_spec", args))
    assert (proposed["deleted"], proposed["status"]) == (False, "pending_approval")
    key = proposed["proposal_etag"]
    stored = c.get("/api/specs/simple", headers=as_role("Viewer")).json()
    assert key == delete_etag(DagSpec.model_validate(stored["spec"]))
    assert dag_file.exists()
    listed = c.get("/api/specs/simple/proposals", headers=as_role("Viewer")).json()
    assert [(p["etag"], p["action"], p["author"]) for p in listed] == [(key, "delete", "deployer")]
    assert _approve(c, key, "deployer").status_code == 403  # the proposer cannot approve
    assert _approve(c, key).status_code == 200
    done = _out(_mcp(c, "Deployer", "delete_dag_spec", args))
    assert (done["deleted"], done["status"]) == (True, "deleted"), done
    assert not dag_file.exists()
    assert c.get("/api/specs/simple/proposals", headers=as_role("Viewer")).json() == []


def test_mcp_writes_follow_forced_pull_requests(tmp_path: Path, git_repo: Path) -> None:  # noqa: F811
    """iam.force_pr_without_direct_push: the writes of callers without deploy:direct_push
    open a pull request, like the REST deploy and delete routes."""
    c = _mcp_client(
        tmp_path, _git_storage(tmp_path, git_repo, "push"), force_pr_without_direct_push=True
    )
    spec = {"spec": SIMPLE, "confirm": True}
    opened = _out(_mcp(c, "Deployer", "save_dag_spec", spec))
    assert (opened["saved"], opened["status"]) == (True, "pr_opened"), opened
    assert "simple.py" not in _files_on(git_repo, "main")
    assert "autoair/simple" in _branches(git_repo)
    # direct-push holders are not forced
    current = _out(_mcp(c, "Admin", "save_dag_spec", {"spec": SIMPLE}))["current_etag"]
    live = _out(_mcp(c, "Admin", "save_dag_spec", {**spec, "base_etag": current}))
    assert live["status"] == "saved", live
    assert "simple.py" in _files_on(git_repo, "main")
    current = _out(_mcp(c, "Deployer", "delete_dag_spec", {"dag_id": "simple"}))["current_etag"]
    removed = _out(
        _mcp(
            c,
            "Deployer",
            "delete_dag_spec",
            {"dag_id": "simple", "confirm": True, "base_etag": current},
        )
    )
    assert (removed["deleted"], removed["status"]) == (True, "pr_opened"), removed
    assert "simple.py" in _files_on(git_repo, "main")  # live until the PR is merged


def test_mcp_pull_requests_need_no_deploy(tmp_path: Path, git_repo: Path) -> None:  # noqa: F811
    """An Author (spec:write, no deploy) opens a pull request over MCP, like over REST."""
    c = _mcp_client(tmp_path, _git_storage(tmp_path, git_repo, "pr"))
    authored = _out(_mcp(c, "Author", "save_dag_spec", {"spec": SIMPLE, "confirm": True}))
    assert (authored["saved"], authored["status"]) == (True, "pr_opened"), authored
    assert "simple.py" not in _files_on(git_repo, "main")


def test_mcp_live_writes_need_deploy(tmp_path: Path) -> None:
    """Without a pull request or a proposal a write goes live: that needs 'deploy'."""
    c = _mcp_client(tmp_path)
    refused = _mcp(c, "Author", "save_dag_spec", {"spec": SIMPLE, "confirm": True})
    assert refused["isError"]
    assert "'deploy'" in refused["content"][0]["text"]
    assert not (tmp_path / "dags" / "simple.py").exists()


def test_mcp_writes_in_pr_mode_are_exempt_from_four_eyes(tmp_path: Path, git_repo: Path) -> None:  # noqa: F811
    c = _mcp_client(tmp_path, _git_storage(tmp_path, git_repo, "pr"), require_approval=True)
    saved = _mcp(c, "Deployer", "save_dag_spec", {"spec": SIMPLE, "confirm": True})
    assert not saved["isError"], saved
    assert "simple.py" not in _files_on(git_repo, "main")  # a pull request, not live
    assert "autoair/simple" in _branches(git_repo)
