"""Bitbucket Cloud and Azure DevOps providers against in-memory fake servers."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, unquote

import httpx2
import pytest

from autoair.spec import DagSpec
from autoair.storage import ConflictError
from autoair.storage.git.azure import ZERO, AzureDevOpsProvider
from autoair.storage.git.bitbucket import BitbucketProvider
from autoair.storage.git.target import GitTarget

SPEC = DagSpec.model_validate(
    {
        "dag_id": "demo",
        "nodes": [
            {
                "kind": "operator",
                "id": "a",
                "task_id": "a",
                "operator": "airflow.providers.standard.operators.empty.EmptyOperator",
            }
        ],
    }
)


@dataclass
class FakeRepo:
    commits: dict[str, dict[str, object]] = field(default_factory=dict)
    branches: dict[str, str] = field(default_factory=dict)
    prs: list[dict[str, object]] = field(default_factory=list)

    def __post_init__(self) -> None:
        root = self.commit(None, {"README.md": "hi"}, "init", "seed")
        self.branches["main"] = root

    def commit(
        self, parent: str | None, files: dict[str, str | None], msg: str, author: str
    ) -> str:
        tree = dict(self.commits[parent]["files"]) if parent else {}  # type: ignore[arg-type]
        for path, content in files.items():
            if content is None:
                tree.pop(path, None)
            else:
                tree[path] = content
        sha = hashlib.sha1(f"{parent}{msg}{len(self.commits)}".encode()).hexdigest()
        self.commits[sha] = {"parent": parent, "files": tree, "message": msg, "author": author}
        return sha

    def resolve(self, ref: str) -> str | None:
        return self.branches.get(ref) or (ref if ref in self.commits else None)

    def files(self, ref: str) -> dict[str, str]:
        sha = self.resolve(ref)
        return dict(self.commits[sha]["files"]) if sha else {}  # type: ignore[arg-type]

    def history(self, ref: str, path: str) -> list[str]:
        out, sha = [], self.resolve(ref)
        while sha:
            c = self.commits[sha]
            parent = c["parent"]
            before = self.commits[parent]["files"].get(path) if parent else None  # type: ignore[union-attr]
            if c["files"].get(path) != before:  # type: ignore[union-attr]
                out.append(sha)
            sha = parent  # type: ignore[assignment]
        return out


# --------------------------------------------------------------------------- bitbucket


def bitbucket_handler(repo: FakeRepo):
    def handle(req: httpx2.Request) -> httpx2.Response:
        path = unquote(req.url.path).removeprefix("/2.0/repositories/ws/dags")
        q = parse_qs(req.url.query.decode())
        if req.method == "GET" and path.startswith("/refs/branches/"):
            name = path.removeprefix("/refs/branches/")
            if name not in repo.branches:
                return httpx2.Response(404, json={"error": {"message": "no branch"}})
            return httpx2.Response(200, json={"target": {"hash": repo.branches[name]}})
        if req.method == "POST" and path == "/refs/branches":
            body = json.loads(req.content)
            if body["name"] in repo.branches:
                return httpx2.Response(400, json={"error": {"message": "already exists"}})
            repo.branches[body["name"]] = body["target"]["hash"]
            return httpx2.Response(201, json={})
        if req.method == "DELETE" and path.startswith("/refs/branches/"):
            repo.branches.pop(path.removeprefix("/refs/branches/"), None)
            return httpx2.Response(204)
        if req.method == "GET" and path.startswith("/src/"):
            ref, _, file_path = path.removeprefix("/src/").partition("/")
            files = repo.files(ref)
            if file_path.endswith("/") or file_path == "":
                prefix = file_path
                vals = [
                    {"type": "commit_file", "path": p}
                    for p in files
                    if p.startswith(prefix) and "/" not in p[len(prefix) :]
                ]
                return httpx2.Response(200, json={"values": vals})
            if file_path not in files:
                return httpx2.Response(404, json={"error": {"message": "missing"}})
            return httpx2.Response(200, text=files[file_path])
        if req.method == "POST" and path == "/src":
            ctype = req.headers["content-type"]
            fields: dict[str, list[str]] = {}
            if "boundary=" not in ctype:  # urlencoded (e.g. delete-only commits)
                fields = parse_qs(req.content.decode())
            boundary = ctype.split("boundary=")[1].encode() if "boundary=" in ctype else b"\0"
            for part in req.content.split(b"--" + boundary)[1:-1]:
                # part = CRLF headers CRLF CRLF body CRLF (keep the body's own newlines)
                head, _, body = part[2:-2].partition(b"\r\n\r\n")
                name = re.search(rb'name="([^"]+)"', head).group(1).decode()  # type: ignore[union-attr]
                fields.setdefault(name, []).append(body.decode())
            branch = fields["branch"][0]
            parent = fields.get("parents", [None])[0]
            if branch in repo.branches and repo.branches[branch] != parent:
                return httpx2.Response(409, json={"error": {"message": "parent mismatch"}})
            changes: dict[str, str | None] = {p: None for p in fields.get("files", [])}
            for k, v in fields.items():
                if k not in ("branch", "parents", "message", "author", "files"):
                    changes[k] = v[0]
            sha = repo.commit(parent, changes, fields["message"][0], fields.get("author", ["?"])[0])
            repo.branches[branch] = sha
            return httpx2.Response(201, headers={"location": f"https://x/commit/{sha}"})
        if req.method == "GET" and path.startswith("/commits/"):
            ref = path.removeprefix("/commits/")
            shas = repo.history(ref, q["path"][0])
            return httpx2.Response(
                200,
                json={
                    "values": [
                        {
                            "hash": s,
                            "message": repo.commits[s]["message"],
                            "author": {"raw": repo.commits[s]["author"]},
                            "date": "2026-10-14T10:00:00Z",
                        }
                        for s in shas
                    ]
                },
            )
        if req.method == "POST" and path == "/pullrequests":
            body = json.loads(req.content)
            pr = {
                "id": len(repo.prs) + 1,
                "source": body["source"]["branch"]["name"],
                "links": {"html": {"href": f"https://bitbucket/pr/{len(repo.prs) + 1}"}},
            }
            repo.prs.append(pr)
            return httpx2.Response(201, json=pr)
        if req.method == "GET" and path == "/pullrequests":
            src = re.search(r'source.branch.name="([^"]+)"', q["q"][0]).group(1)  # type: ignore[union-attr]
            return httpx2.Response(
                200, json={"values": [p for p in repo.prs if p["source"] == src]}
            )
        return httpx2.Response(500, json={"error": {"message": f"unhandled {req.method} {path}"}})

    return handle


# --------------------------------------------------------------------------- azure


def azure_handler(repo: FakeRepo):
    def handle(req: httpx2.Request) -> httpx2.Response:
        path = req.url.path.removeprefix("/org/proj/_apis/git/repositories/dags")
        q = {k: v[0] for k, v in parse_qs(req.url.query.decode()).items()}
        ref = q.get("versionDescriptor.version", "main")
        if req.method == "GET" and path == "/refs":
            name = q["filter"].removeprefix("heads/")
            value = (
                [{"name": f"refs/heads/{name}", "objectId": repo.branches[name]}]
                if name in repo.branches
                else []
            )
            return httpx2.Response(200, json={"value": value})
        if req.method == "POST" and path == "/refs":
            upd = json.loads(req.content)[0]
            name = upd["name"].removeprefix("refs/heads/")
            current = repo.branches.get(name, ZERO)
            if current != upd["oldObjectId"]:
                return httpx2.Response(
                    200,
                    json={
                        "value": [{"success": False, "updateStatus": "rejectedStaleOldObjectId"}]
                    },
                )
            repo.branches[name] = upd["newObjectId"]
            return httpx2.Response(200, json={"value": [{"success": True}]})
        if req.method == "GET" and path == "/items":
            files = repo.files(ref)
            if "scopePath" in q:
                prefix = q["scopePath"].strip("/")
                prefix = f"{prefix}/" if prefix else ""
                return httpx2.Response(
                    200,
                    json={
                        "value": [
                            {"path": f"/{p}", "isFolder": False}
                            for p in files
                            if p.startswith(prefix) and "/" not in p[len(prefix) :]
                        ]
                    },
                )
            p = q["path"].lstrip("/")
            if p not in files:
                return httpx2.Response(404, json={"message": "not found"})
            return httpx2.Response(200, json={"content": files[p], "objectId": "blob"})
        if req.method == "POST" and path == "/pushes":
            body = json.loads(req.content)
            upd = body["refUpdates"][0]
            name = upd["name"].removeprefix("refs/heads/")
            if repo.branches.get(name, ZERO) != upd["oldObjectId"]:
                return httpx2.Response(409, json={"message": "TF401028: stale"})
            commit = body["commits"][0]
            changes: dict[str, str | None] = {}
            for ch in commit["changes"]:
                p = ch["item"]["path"].lstrip("/")
                changes[p] = (
                    None
                    if ch["changeType"] == "delete"
                    else base64.b64decode(ch["newContent"]["content"]).decode()
                )
            parent = None if upd["oldObjectId"] == ZERO else upd["oldObjectId"]
            sha = repo.commit(
                parent, changes, commit["comment"], commit.get("author", {}).get("name", "?")
            )
            repo.branches[name] = sha
            return httpx2.Response(201, json={"commits": [{"commitId": sha}]})
        if req.method == "GET" and path == "/commits":
            shas = repo.history(
                q["searchCriteria.itemVersion.version"], q["searchCriteria.itemPath"].lstrip("/")
            )
            return httpx2.Response(
                200,
                json={
                    "value": [
                        {
                            "commitId": s,
                            "comment": repo.commits[s]["message"],
                            "author": {
                                "name": repo.commits[s]["author"],
                                "email": "a@b.c",
                                "date": "2026-10-14T10:00:00Z",
                            },
                        }
                        for s in shas
                    ]
                },
            )
        if req.method == "POST" and path == "/pullrequests":
            body = json.loads(req.content)
            pr = {"pullRequestId": len(repo.prs) + 1, "src": body["sourceRefName"]}
            repo.prs.append(pr)
            return httpx2.Response(201, json=pr)
        if req.method == "GET" and path == "/pullrequests":
            src = q["searchCriteria.sourceRefName"]
            return httpx2.Response(200, json={"value": [p for p in repo.prs if p["src"] == src]})
        return httpx2.Response(500, json={"message": f"unhandled {req.method} {path}"})

    return handle


def make(kind: str, repo: FakeRepo):
    if kind == "bitbucket":
        return BitbucketProvider(
            "ws/dags", "tok", transport=httpx2.MockTransport(bitbucket_handler(repo))
        )
    return AzureDevOpsProvider(
        "org/proj/dags",
        "pat",
        api_url="https://azure.test",
        transport=httpx2.MockTransport(azure_handler(repo)),
    )


@pytest.mark.parametrize("kind", ["bitbucket", "azure"])
def test_push_mode_roundtrip_and_conflict(kind):
    repo = FakeRepo()
    target = GitTarget(make(kind, repo), base_branch="main", subdir="dags", mode="push")
    first = target.save(SPEC, {"demo.py": '"""Generated by AutoAir"""\n'}, author="Jane <j@x.io>")
    assert repo.branches["main"] == first.version
    assert "dags/demo.autoair.json" in repo.files("main")
    stored = target.get("demo")
    assert stored is not None
    assert stored.etag == first.etag

    changed = SPEC.model_copy(update={"description": "v2"})
    second = target.save(
        changed, {"demo.py": '"""Generated by AutoAir"""\n# v2\n'}, base_etag=first.etag
    )
    assert [v.version for v in target.history("demo")][:2] == [second.version, first.version]
    with pytest.raises(ConflictError):
        target.save(
            SPEC.model_copy(update={"description": "stale"}),
            {"demo.py": '"""Generated by AutoAir"""\n'},
            base_etag=first.etag,
        )
    assert [s.dag_id for s in target.list()] == ["demo"]
    deleted = target.delete("demo")
    assert deleted
    assert "dags/demo.py" not in repo.files("main")


@pytest.mark.parametrize("kind", ["bitbucket", "azure"])
def test_pr_mode_opens_and_reuses_pull_request(kind):
    repo = FakeRepo()
    target = GitTarget(make(kind, repo), base_branch="main", subdir="dags", mode="pr")
    result = target.save(SPEC, {"demo.py": '"""Generated by AutoAir"""\n'})
    assert result.pr_url
    assert result.branch
    assert result.branch.startswith("autoair/")
    assert "dags/demo.py" not in repo.files("main")
    assert "dags/demo.py" in repo.files(result.branch)
    again = target.save(
        SPEC.model_copy(update={"description": "x"}),
        {"demo.py": '"""Generated by AutoAir"""\n# x\n'},
        base_etag=result.etag,
    )
    assert again.pr_url == result.pr_url
    assert len(repo.prs) == 1
