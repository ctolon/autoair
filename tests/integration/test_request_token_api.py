"""Git tokens typed per deploy over the REST API (header X-AutoAir-Git-Token).

A real git smart-HTTP host (tests/git_http.py) that only accepts the user's token proves
every write (deploy, PR-mode branch, delete, promotion, proposal deploy) authenticates with
it; the scrubbing test greps captured logs, audit records (API and files) and every
response for the token and its encodings.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import shutil
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from autoair.mcp.iam import IamMiddleware
from autoair.storage.config import GitStorageSettings, StorageSettings
from autoair.storage.request_token import git_token_required
from tests.git_http import GitHttpServer, bare_repo
from tests.integration.test_iam_api import SIMPLE, as_role, make_services

pytestmark = [
    pytest.mark.airflow,
    pytest.mark.skipif(shutil.which("git") is None, reason="git not installed"),
]

TOKEN = "glpat-USERtoken-0123456789abcdef"
HEADER = "X-AutoAir-Git-Token"
BASIC = base64.b64encode(f"x-access-token:{TOKEN}".encode()).decode()


def headers(role: str = "Admin", token: str | None = TOKEN) -> dict[str, str]:
    out = as_role(role)
    if token is not None:
        out[HEADER] = token
    return out


@pytest.fixture
def host(tmp_path: Path):
    root = tmp_path / "srv"
    bare_repo(root, "dags.git")
    with GitHttpServer(root, TOKEN) as server:
        yield server


def client_for(
    tmp_path: Path, repo: str, *, https: bool = True, iam: dict[str, Any] | None = None, **git: Any
) -> TestClient:
    settings = {
        "provider": "local",
        "repo": repo,
        "mode": "push",
        "cache_dir": tmp_path / "cache",
        "request_token_allow_insecure": repo.startswith("http://"),
        **git,
    }
    services = make_services(
        tmp_path,
        iam=iam,
        storage=StorageSettings(mode="git", git=GitStorageSettings(**settings)),
        import_check_on_deploy=False,
        audit_url=str(tmp_path / "audit"),
    )
    from autoair.api.app import create_app

    base = "https://autoair.test" if https else "http://autoair.test"
    client = TestClient(create_app(services), base_url=base, raise_server_exceptions=False)
    client.services = services  # type: ignore[attr-defined]
    return client


def deploy(c: TestClient, h: dict[str, str], **body: Any) -> Any:
    return c.put("/api/specs/simple", json={"spec": SIMPLE, **body}, headers=h)


def pushes(host: GitHttpServer) -> list[str | None]:
    """Authorization of every push request (reads may use the configured credentials)."""
    return [auth for path, auth in host.requests if "receive-pack" in path]


def show(host: GitHttpServer, ref: str, path: str) -> str:
    import subprocess

    return subprocess.run(
        ["git", "--git-dir", str(host.root / "dags.git"), "show", f"{ref}:{path}"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout


# --------------------------------------------------------------------------- happy paths
def test_push_and_pr_mode_deploys_use_the_token(tmp_path: Path, host: GitHttpServer) -> None:
    c = client_for(tmp_path, host.url("dags.git"))
    # No token configured anywhere: the host refuses the configured (none) credentials.
    assert deploy(c, headers(token=None)).status_code == 409
    host.requests.clear()
    ok = deploy(c, headers())
    assert ok.status_code == 200, ok.text
    assert ok.json()["branch"] == "main"
    assert '"dag_id": "simple"' in show(host, "main", "simple.autoair.json")
    assert pushes(host)
    assert all(a == f"Basic {BASIC}" for a in pushes(host))
    # PR mode on a plain git host: the pending branch is pushed with the token too
    pr = deploy(
        c,
        headers(),
        spec={**SIMPLE, "description": "v2"},
        base_etag=ok.json()["etag"],
        direct_push=False,
    )
    assert pr.status_code == 200, pr.text
    assert pr.json()["branch"] == "autoair/simple"
    assert "v2" in show(host, "autoair/simple", "simple.autoair.json")
    audit = c.get("/api/specs/simple/audit", headers=as_role("Admin")).json()
    details = audit[0]["details"]
    assert details["credential"] == "request-token"
    assert details["credentials"] == "request_token"
    assert details["credential_user"] is None  # a plain git host has no user API
    assert "credentials_conn_id" not in details


def test_delete_and_promote_take_the_token(tmp_path: Path, host: GitHttpServer) -> None:
    c = client_for(
        tmp_path,
        host.url("dags.git"),
        environments={"dev": "dev", "prod": "main"},
        request_token="required",
    )
    spec = {**SIMPLE, "environments": {"dev": {}, "prod": {}}}
    ok = c.put("/api/specs/simple", json={"spec": spec, "environment": "dev"}, headers=headers())
    assert ok.status_code == 200, ok.text
    no_token = c.post(
        "/api/vcs/simple/promote",
        json={"from_env": "dev", "to_env": "prod"},
        headers=headers(token=None),
    )
    assert no_token.status_code == 400
    assert no_token.json()["detail"]["code"] == "git_token_required"
    promoted = c.post(
        "/api/vcs/simple/promote", json={"from_env": "dev", "to_env": "prod"}, headers=headers()
    )
    assert promoted.status_code == 200, promoted.text
    assert promoted.json()["branch"].startswith("autoair/promote/dev-to-prod")
    audit = c.get("/api/specs/simple/audit", headers=as_role("Admin")).json()
    assert audit[0]["action"] == "promote"
    assert audit[0]["details"]["credential"] == "request-token"

    assert c.delete("/api/specs/simple", headers=headers(token=None)).status_code == 400
    gone = c.delete("/api/specs/simple", headers=headers())
    assert gone.status_code == 200, gone.text


def test_required_mode_and_mcp(tmp_path: Path, host: GitHttpServer) -> None:
    c = client_for(tmp_path, host.url("dags.git"), request_token="required")
    missing = deploy(c, headers(token=None))
    assert missing.status_code == 400
    assert missing.json()["detail"]["code"] == "git_token_required"
    assert deploy(c, headers()).status_code == 200
    me = c.get("/api/me", headers=as_role("Admin")).json()["deploy"]
    assert me["git_token"] == "required"
    creds = c.get("/api/deploy-target/git-credentials", headers=as_role("Admin")).json()
    assert creds["request_token"]["mode"] == "required"
    assert creds["request_token"]["header"] == HEADER
    assert creds["request_token"]["hint"]["provider"] == "local"
    info = c.get("/api/deploy-target", headers=as_role("Admin")).json()
    assert info["request_token"]["mode"] == "required"
    assert info["policy"]["request_token"] == "required"
    # MCP writes never carry a token: refused while dry runs still answer
    services = c.services  # type: ignore[attr-defined]
    assert git_token_required(services) is True
    middleware = IamMiddleware(git_token_required=lambda: git_token_required(services))

    class Msg:
        def __init__(self, confirm: bool) -> None:
            self.name = "save_dag_spec"
            self.arguments = {"confirm": confirm}

    class Ctx:
        def __init__(self, confirm: bool) -> None:
            self.message = Msg(confirm)

    async def nxt(_ctx: Any) -> str:
        return "ran"

    from fastmcp.exceptions import ToolError

    with pytest.raises(ToolError, match="request_token=required"):
        asyncio.run(middleware.on_call_tool(Ctx(True), nxt))  # type: ignore[arg-type]
    assert asyncio.run(middleware.on_call_tool(Ctx(False), nxt)) == "ran"  # type: ignore[arg-type]


# --------------------------------------------------------------------------- refusals
def test_header_refused_off_non_git_forced(tmp_path: Path, host: GitHttpServer) -> None:
    off = client_for(tmp_path / "off", host.url("dags.git"), request_token="off")
    r = deploy(off, headers())
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "git_token_off"
    assert off.get("/api/me", headers=as_role("Admin")).json()["deploy"]["git_token"] == "off"

    forced = client_for(tmp_path / "forced", host.url("dags.git"), forced_conn_id="git_svc")
    r = deploy(forced, headers())
    assert r.status_code == 400
    assert "git_svc" in r.json()["detail"]["message"]

    override = client_for(
        tmp_path / "override",
        host.url("dags.git"),
        forced_conn_id="git_svc",
        request_token_overrides_forced=True,
    )
    r = deploy(override, headers())
    assert r.status_code == 200, r.text  # the token replaces the forced connection

    services = make_services(tmp_path / "local")  # a local-folder target
    from autoair.api.app import create_app

    local = TestClient(create_app(services), base_url="https://autoair.test")
    r = local.put(
        "/api/specs/simple", json={"spec": SIMPLE}, headers={**headers(), "X-AutoAir-CSRF": "1"}
    )
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "git_token_not_git"
    assert local.get("/api/me", headers=as_role("Admin")).json()["deploy"]["git_token"] is None


def test_plain_http_from_a_remote_client_is_refused(tmp_path: Path) -> None:
    remote = bare_repo(tmp_path / "srv")
    c = client_for(tmp_path, f"file://{remote}", https=False)  # TestClient: host 'testclient'
    r = deploy(c, headers())
    assert r.status_code == 403
    assert r.json()["detail"]["code"] == "git_token_insecure"
    assert TOKEN not in r.text
    # without the header, plain HTTP deploys work as before
    assert deploy(c, headers(token=None)).status_code == 200


def test_rejected_token_is_401_and_not_retried(tmp_path: Path, host: GitHttpServer) -> None:
    c = client_for(tmp_path, host.url("dags.git"), token="configured-service-token-xyz")
    wrong = "wrong-user-token-0000000"
    configured = base64.b64encode(b"x-access-token:configured-service-token-xyz").decode()
    host.requests.clear()
    r = deploy(c, headers(token=wrong))
    assert r.status_code == 401, r.text
    assert r.json()["detail"]["code"] == "git_token_rejected"
    assert wrong not in r.text
    wrong_basic = base64.b64encode(f"x-access-token:{wrong}".encode()).decode()
    # the write tried the user's token only (once), never the configured one
    assert f"Basic {wrong_basic}" in host.seen
    assert f"Basic {configured}" not in pushes(host)
    assert not pushes(host)  # refused at the fetch: nothing was pushed


# --------------------------------------------------------------------------- scrubbing
def test_token_never_reaches_logs_audit_or_responses(
    tmp_path: Path, host: GitHttpServer, caplog: pytest.LogCaptureFixture, monkeypatch: Any
) -> None:
    caplog.set_level(logging.DEBUG)
    c = client_for(tmp_path, host.url("dags.git"), environments={"dev": "dev", "prod": "main"})
    responses: list[Any] = []
    spec = {**SIMPLE, "environments": {"dev": {}, "prod": {}}}
    responses.append(
        c.put("/api/specs/simple", json={"spec": spec, "environment": "dev"}, headers=headers())
    )
    responses.append(
        c.post("/api/specs/simple/deploy-preview", json={"spec": spec}, headers=headers(token=None))
    )
    responses.append(
        c.post(
            "/api/vcs/simple/promote", json={"from_env": "dev", "to_env": "prod"}, headers=headers()
        )
    )
    # a host echoing the credentials in its error (git stderr with the URL / header)
    from autoair.storage.git import local_git

    real_failure = local_git.git_failure

    wrong = TOKEN + "-wrong"
    wrong_basic = base64.b64encode(f"x-access-token:{wrong}".encode()).decode()

    def echoing(what: str, stderr: str) -> Any:
        return real_failure(what, f"{stderr} https://x-access-token:{wrong}@host {wrong_basic}")

    monkeypatch.setattr(local_git, "git_failure", echoing)
    responses.append(deploy(c, headers(token=wrong)))
    monkeypatch.setattr(local_git, "git_failure", real_failure)

    # an unexpected failure whose message contains the token: masked 502, no traceback
    from autoair.storage.git.target import GitTarget

    def boom(self: Any, *a: Any, **kw: Any) -> Any:
        logging.getLogger("autoair.storage.git").error("about to fail with %s", TOKEN)
        raise RuntimeError(f"exploded with {TOKEN} and {BASIC}")

    monkeypatch.setattr(GitTarget, "save", boom)
    exploded = deploy(c, headers(), spec={**spec, "description": "boom"})
    assert exploded.status_code == 502
    responses.append(exploded)
    monkeypatch.undo()
    responses.append(c.delete("/api/specs/simple", headers=headers()))
    responses.append(c.get("/api/specs/simple/audit", headers=as_role("Admin")))
    responses.append(c.get("/api/deploy-target", headers=as_role("Admin")))

    assert responses[0].status_code == 200, responses[0].text
    assert responses[2].status_code == 200, responses[2].text
    assert responses[3].status_code == 401  # TOKEN-wrong refused by the host
    assert "***" in responses[3].text
    secrets = (TOKEN, BASIC, wrong, wrong_basic)
    for r in responses:
        blob = r.text + "\n".join(f"{k}: {v}" for k, v in r.headers.items())
        for secret in secrets:
            assert secret not in blob, (r.request.method, r.request.url, r.text)
    for secret in secrets:
        assert secret not in caplog.text
        for record in caplog.records:
            assert secret not in str(record.__dict__)
    audit_files = list((tmp_path / "audit").rglob("*.jsonl"))
    assert audit_files
    for path in audit_files:
        text = path.read_text()
        assert "request-token" in text
        for secret in secrets:
            assert secret not in text
    # nothing on disk either (clone cache, deploy-target state)
    for path in tmp_path.rglob("*"):
        if path.is_file() and "srv" not in path.parts and path.stat().st_size < 2_000_000:
            data = path.read_bytes()
            for secret in secrets:
                assert secret.encode() not in data, path
    # the commit and PR texts never carry it either
    import subprocess

    log = subprocess.run(
        ["git", "--git-dir", str(host.root / "dags.git"), "log", "--all", "--format=%B"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    for secret in secrets:
        assert secret not in log


# --------------------------------------------------------------------------- MCP
MCP_JSON = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
    "mcp-protocol-version": "2025-06-18",
}


def mcp_call(c: TestClient, name: str, args: dict[str, Any], token: str | None) -> Any:
    h = {**MCP_JSON, **as_role("Admin")}
    if token is not None:
        h[HEADER] = token
    resp = c.post(
        "/mcp/",
        headers=h,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": args},
        },
    )
    assert resp.status_code == 200, resp.text
    return resp


def test_mcp_header_token_flag_off_is_refused(tmp_path: Path, host: GitHttpServer) -> None:
    c = client_for(tmp_path, host.url("dags.git"))
    r = mcp_call(c, "save_dag_spec", {"spec": SIMPLE, "confirm": True}, TOKEN)
    result = r.json()["result"]
    assert result["isError"]
    assert "mcp_request_token" in r.text
    assert TOKEN not in r.text
    assert not pushes(host)  # nothing written with any credentials


def test_mcp_deploys_and_deletes_with_the_header_token(
    tmp_path: Path, host: GitHttpServer, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    c = client_for(tmp_path, host.url("dags.git"), mcp_request_token=True, request_token="required")
    # required: an MCP write without the header is refused (dry runs still answer)
    no_token = mcp_call(c, "save_dag_spec", {"spec": SIMPLE, "confirm": True}, None)
    assert no_token.json()["result"]["isError"]
    assert "X-AutoAir-Git-Token header" in no_token.text
    dry = mcp_call(c, "save_dag_spec", {"spec": SIMPLE, "confirm": False}, None)
    assert not dry.json()["result"]["isError"]

    host.requests.clear()
    saved = mcp_call(c, "save_dag_spec", {"spec": SIMPLE, "confirm": True}, TOKEN)
    assert not saved.json()["result"]["isError"], saved.text
    assert '"dag_id": "simple"' in show(host, "main", "simple.autoair.json")
    assert pushes(host)
    assert all(a == f"Basic {BASIC}" for a in pushes(host))

    wrong = TOKEN + "-mcp-wrong"
    refused = mcp_call(
        c, "save_dag_spec", {"spec": {**SIMPLE, "description": "x"}, "confirm": True}, wrong
    )
    assert refused.json()["result"]["isError"]
    assert "rejected your token" in refused.text

    gone = mcp_call(c, "delete_dag_spec", {"dag_id": "simple", "confirm": True}, TOKEN)
    assert not gone.json()["result"]["isError"], gone.text
    assert show(host, "main", "simple.autoair.json") == ""

    audit = c.get("/api/specs/simple/audit", headers=as_role("Admin")).json()
    assert [a["action"] for a in audit[:2]] == ["delete", "deploy"]
    assert all(a["details"]["credential"] == "request-token" for a in audit[:2])
    assert all(a["details"]["via"] == "mcp" for a in audit[:2])
    wrong_basic = base64.b64encode(f"x-access-token:{wrong}".encode()).decode()
    texts = [no_token.text, dry.text, saved.text, refused.text, gone.text, caplog.text]
    texts += [p.read_text() for p in (tmp_path / "audit").rglob("*.jsonl")]
    for text in texts:
        for secret in (TOKEN, BASIC, wrong, wrong_basic):
            assert secret not in text


def test_mcp_header_token_follows_the_rest_rules(tmp_path: Path) -> None:
    remote = bare_repo(tmp_path / "srv")
    insecure = client_for(tmp_path / "a", f"file://{remote}", https=False, mcp_request_token=True)
    r = mcp_call(insecure, "save_dag_spec", {"spec": SIMPLE, "confirm": True}, TOKEN)
    assert r.json()["result"]["isError"]
    assert "HTTPS" in r.text
    forced = client_for(
        tmp_path / "b", f"file://{remote}", mcp_request_token=True, forced_conn_id="git_svc"
    )
    r = mcp_call(forced, "save_dag_spec", {"spec": SIMPLE, "confirm": True}, TOKEN)
    assert r.json()["result"]["isError"]
    assert "git_svc" in r.text
    off = client_for(
        tmp_path / "c", f"file://{remote}", mcp_request_token=True, request_token="off"
    )
    r = mcp_call(off, "delete_dag_spec", {"dag_id": "simple", "confirm": True}, TOKEN)
    assert r.json()["result"]["isError"]
    assert "request_token is off" in r.text
    for resp in (r,):
        assert TOKEN not in resp.text
