"""GitHub (and GitHub Enterprise Server) provider using the REST + GraphQL APIs.

Commits go through GraphQL ``createCommitOnBranch`` whose ``expectedHeadOid`` gives a
server-side compare-and-swap, so concurrent AutoAir instances can never clobber
each other. Authentication is a token (PAT / fine-grained) or a GitHub App
installation token minted on demand (needs ``pyjwt`` + ``cryptography``).
"""

from __future__ import annotations

import base64
import datetime
import importlib
import logging
import threading
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

import httpx2

from autoair.net import tls
from autoair.storage.base import AuthError, ConflictError, StorageError
from autoair.storage.git.provider import (
    CommitInfo,
    PullRequest,
    PullRequestOptions,
    check_branch,
    check_ref,
    parse_author,
)

log = logging.getLogger(__name__)
DEFAULT_API_URL = "https://api.github.com"

_COMMIT_MUTATION = """
mutation($input: CreateCommitOnBranchInput!) {
  createCommitOnBranch(input: $input) { commit { oid url } }
}
"""


def jwt_available() -> bool:
    try:
        importlib.import_module("jwt")
        importlib.import_module("cryptography")
    except ImportError:
        return False
    return True


class GitHubAppTokenSource:
    """Mint (and cache) GitHub App installation tokens: JWT (RS256) -> access token."""

    def __init__(
        self,
        app_id: str | int,
        private_key: str,
        installation_id: str | int,
        *,
        api_url: str = DEFAULT_API_URL,
        transport: httpx2.BaseTransport | None = None,
        clock: Callable[[], float] = time.time,
        timeout: float = 30.0,
    ) -> None:
        if not jwt_available():
            raise StorageError("GitHub App auth needs the 'pyjwt' and 'cryptography' packages")
        self.app_id = str(app_id)
        self._private_key = private_key
        self.installation_id = str(installation_id)
        self.api_url = api_url.rstrip("/")
        self._transport = transport
        self._clock = clock
        self.timeout = timeout
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = threading.Lock()

    def app_jwt(self) -> str:
        jwt: Any = importlib.import_module("jwt")
        now = int(self._clock())
        payload = {"iat": now - 60, "exp": now + 540, "iss": self.app_id}
        return str(jwt.encode(payload, self._private_key, algorithm="RS256"))

    def __call__(self) -> str:
        with self._lock:
            if self._token and self._clock() < self._expires_at - 60:
                return self._token
            with httpx2.Client(
                transport=self._transport,
                timeout=self.timeout,
                **tls.httpx_kwargs("git", transport=self._transport),
            ) as client:
                resp = client.post(
                    f"{self.api_url}/app/installations/{quote(self.installation_id)}/access_tokens",
                    headers={
                        "Authorization": f"Bearer {self.app_jwt()}",
                        "Accept": "application/vnd.github+json",
                    },
                )
            if resp.status_code >= 400:
                raise StorageError(
                    f"GitHub App token request failed ({resp.status_code}): {_message(resp)}"
                )
            data = resp.json()
            self._token = str(data["token"])
            expires = data.get("expires_at")
            self._expires_at = (
                datetime.datetime.fromisoformat(str(expires).replace("Z", "+00:00")).timestamp()
                if expires
                else self._clock() + 3000
            )
            return self._token


def _message(resp: httpx2.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return resp.text[:300]
    if isinstance(data, dict):
        return str(data.get("message") or data.get("error") or data)[:300]
    return str(data)[:300]


def graphql_url(api_url: str) -> str:
    api_url = api_url.rstrip("/")
    if api_url.endswith("/api/v3"):  # GitHub Enterprise Server
        return api_url[: -len("/v3")] + "/graphql"
    return f"{api_url}/graphql"


class GitHubProvider:
    """:class:`~autoair.storage.git.provider.GitProvider` for ``owner/repo`` on GitHub."""

    name = "github"
    supports_pull_requests = True

    def __init__(
        self,
        repo: str,
        token: str | Callable[[], str],
        *,
        api_url: str = DEFAULT_API_URL,
        transport: httpx2.BaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        owner, _, name = repo.strip("/").partition("/")
        if not owner or not name or "/" in name:
            raise StorageError(f"GitHub repo must look like 'owner/name', got {repo!r}")
        self.owner, self.repo_name = owner, name.removesuffix(".git")
        self.api_url = api_url.rstrip("/")
        self.location = f"github:{self.owner}/{self.repo_name}"
        self._token = token
        self._client = httpx2.Client(
            base_url=self.api_url,
            transport=transport,
            timeout=timeout,
            **tls.httpx_kwargs("git", transport=transport),
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "autoair",
            },
        )

    # ----------------------------------------------------------------- http
    @property
    def _repo(self) -> str:
        return f"/repos/{quote(self.owner)}/{quote(self.repo_name)}"

    def _auth(self) -> dict[str, str]:
        token = self._token() if callable(self._token) else self._token
        return {"Authorization": f"Bearer {token}"} if token else {}

    def _request(
        self,
        method: str,
        url: str,
        *,
        ok_missing: bool = False,
        **kwargs: Any,
    ) -> httpx2.Response | None:
        try:
            resp = self._client.request(method, url, headers=self._auth(), **kwargs)
        except httpx2.HTTPError as exc:
            raise StorageError(f"GitHub request failed: {exc.__class__.__name__}") from exc
        if ok_missing and resp.status_code in (404, 409):
            return None
        if resp.status_code >= 400:
            error = AuthError if resp.status_code in (401, 403) else StorageError
            raise error(f"GitHub API error {resp.status_code}: {_message(resp)}")
        return resp

    def _json(self, method: str, url: str, **kwargs: Any) -> Any:
        resp = self._request(method, url, **kwargs)
        assert resp is not None
        return resp.json()

    # ----------------------------------------------------------------- reads
    def get_file(self, ref: str, path: str) -> tuple[str | None, str | None]:
        check_ref(ref)
        resp = self._request(
            "GET",
            f"{self._repo}/contents/{quote(path)}",
            params={"ref": ref},
            ok_missing=True,
        )
        if resp is None:
            return None, None
        data = resp.json()
        if not isinstance(data, dict) or data.get("type") != "file":
            return None, None
        sha = str(data["sha"])
        if data.get("encoding") == "base64" and data.get("content"):
            raw = base64.b64decode(str(data["content"]))
        else:  # > 1 MB files: fetch the blob
            blob = self._json("GET", f"{self._repo}/git/blobs/{sha}")
            raw = base64.b64decode(str(blob.get("content", "")))
        return raw.decode("utf-8"), sha

    def list_files(self, ref: str, prefix: str) -> list[str]:
        check_ref(ref)
        prefix = prefix.strip("/")
        resp = self._request(
            "GET", f"{self._repo}/contents/{quote(prefix)}", params={"ref": ref}, ok_missing=True
        )
        if resp is None:
            return []
        data = resp.json()
        if not isinstance(data, list):
            return []
        return [str(e["path"]) for e in data if e.get("type") == "file"]

    def head(self, branch: str) -> str | None:
        check_branch(branch)
        resp = self._request("GET", f"{self._repo}/git/ref/heads/{quote(branch)}", ok_missing=True)
        if resp is None:
            return None
        data = resp.json()
        if not isinstance(data, dict):  # a prefix match returns a list
            return None
        return str(data["object"]["sha"])

    def token_user(self) -> str | None:
        """Login of the token's owner (audit attribution; ``GET /user``)."""
        data = self._json("GET", "/user")
        return str(data.get("login")) if isinstance(data, dict) and data.get("login") else None

    def log(self, path: str, ref: str, limit: int = 50) -> list[CommitInfo]:
        check_ref(ref)
        resp = self._request(
            "GET",
            f"{self._repo}/commits",
            params={"path": path, "sha": ref, "per_page": max(1, min(limit, 100))},
            ok_missing=True,
        )
        if resp is None:
            return []
        out: list[CommitInfo] = []
        for item in resp.json()[:limit]:
            commit = item.get("commit") or {}
            author = commit.get("author") or {}
            date = author.get("date")
            out.append(
                CommitInfo(
                    sha=str(item["sha"]),
                    author_name=author.get("name"),
                    author_email=author.get("email"),
                    timestamp=(
                        datetime.datetime.fromisoformat(str(date).replace("Z", "+00:00"))
                        if date
                        else None
                    ),
                    message=str(commit.get("message", "")),
                )
            )
        return out

    # ----------------------------------------------------------------- writes
    def create_branch(self, name: str, from_sha: str) -> None:
        check_branch(name)
        resp = self._client.post(
            f"{self._repo}/git/refs",
            json={"ref": f"refs/heads/{name}", "sha": from_sha},
            headers=self._auth(),
        )
        if resp.status_code == 422:
            raise ConflictError(None, f"branch {name!r} already exists")
        if resp.status_code >= 400:
            error = AuthError if resp.status_code in (401, 403) else StorageError
            raise error(f"GitHub API error {resp.status_code}: {_message(resp)}")

    def reset_branch(self, name: str, to_sha: str) -> None:
        check_branch(name)
        self._request(
            "PATCH",
            f"{self._repo}/git/refs/heads/{quote(name)}",
            json={"sha": to_sha, "force": True},
        )

    def commit_files(
        self,
        branch: str,
        base_sha: str | None,
        changes: dict[str, str | None],
        message: str,
        author: str | None,
    ) -> str:
        check_branch(branch)
        if base_sha is None:
            raise StorageError(
                f"branch {branch!r} does not exist; GitHub repositories need an initial commit"
            )
        headline, _, body = message.partition("\n")
        if author:
            # The GraphQL API commits as the token's owner: credit the user as co-author.
            name, email = parse_author(author, ("AutoAir", "autoair@localhost"))
            trailer = f"Co-authored-by: {name} <{email}>"
            if trailer not in body:
                # Join AutoAir's trailer paragraph (trailers must be one final paragraph).
                sep = "\n" if "\nX-AutoAir-User:" in f"\n{body}" else "\n\n"
                body = f"{body.strip()}{sep}{trailer}".strip()
        additions = [
            {"path": p, "contents": base64.b64encode(c.encode("utf-8")).decode("ascii")}
            for p, c in sorted(changes.items())
            if c is not None
        ]
        deletions = [{"path": p} for p, c in sorted(changes.items()) if c is None]
        variables = {
            "input": {
                "branch": {
                    "repositoryNameWithOwner": f"{self.owner}/{self.repo_name}",
                    "branchName": branch,
                },
                "message": {"headline": headline.strip() or "Update via AutoAir", "body": body},
                "expectedHeadOid": base_sha,
                "fileChanges": {"additions": additions, "deletions": deletions},
            }
        }
        data = self._json(
            "POST",
            graphql_url(self.api_url),
            json={"query": _COMMIT_MUTATION, "variables": variables},
        )
        errors = data.get("errors") if isinstance(data, dict) else None
        if errors:
            text = "; ".join(str(e.get("message", e)) for e in errors)
            lowered = text.lower()
            stale = any(str(e.get("type", "")).upper() == "STALE_DATA" for e in errors)
            if stale or "expected branch to point to" in lowered or "expectedheadoid" in lowered:
                raise ConflictError(None, f"branch {branch!r} moved; reload and retry")
            raise StorageError(f"GitHub commit failed: {text[:300]}")
        return str(data["data"]["createCommitOnBranch"]["commit"]["oid"])

    def open_pr(
        self,
        head: str,
        base: str,
        title: str,
        body: str,
        options: PullRequestOptions | None = None,
    ) -> PullRequest | None:
        opts = options or PullRequestOptions()
        payload: dict[str, Any] = {"title": title, "head": head, "base": base, "body": body}
        if opts.draft:
            payload["draft"] = True
        data = self._json("POST", f"{self._repo}/pulls", json=payload)
        pr = PullRequest(url=str(data["html_url"]), number=int(data["number"]))
        if opts.labels:
            self._best_effort(
                "labels",
                "POST",
                f"{self._repo}/issues/{pr.number}/labels",
                json={"labels": list(opts.labels)},
            )
        if opts.reviewers:
            users = [r for r in opts.reviewers if "/" not in r]
            teams = [r.split("/", 1)[1] for r in opts.reviewers if "/" in r]
            self._best_effort(
                "reviewers",
                "POST",
                f"{self._repo}/pulls/{pr.number}/requested_reviewers",
                json={"reviewers": users, "team_reviewers": teams},
            )
        return pr

    def _best_effort(self, what: str, method: str, url: str, **kwargs: Any) -> None:
        try:
            self._request(method, url, **kwargs)
        except StorageError as exc:
            log.warning("GitHub: could not set pull request %s: %s", what, exc)

    def find_open_pr(self, head: str) -> PullRequest | None:
        resp = self._request(
            "GET",
            f"{self._repo}/pulls",
            params={"head": f"{self.owner}:{head}", "state": "open"},
            ok_missing=True,
        )
        if resp is None:
            return None
        items = resp.json()
        if not items:
            return None
        return PullRequest(url=str(items[0]["html_url"]), number=int(items[0]["number"]))

    def close(self) -> None:
        self._client.close()
