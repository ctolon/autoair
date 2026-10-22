"""Bitbucket Cloud provider (REST API 2.0).

Commits use the ``/src`` endpoint with ``parents`` set to the expected head; the branch
head is also checked right before committing. Auth: repository/workspace access token
(Bearer) or ``username`` + app password (Basic).
"""

from __future__ import annotations

import datetime
import logging
import re
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
DEFAULT_API_URL = "https://api.bitbucket.org/2.0"


def _message(resp: httpx2.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return resp.text[:300]
    if isinstance(data, dict) and isinstance(data.get("error"), dict):
        return str(data["error"].get("message") or data["error"])[:300]
    return str(data)[:300]


def _ts(value: Any) -> datetime.datetime | None:
    if not value:
        return None
    return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))


class BitbucketProvider:
    """:class:`~autoair.storage.git.provider.GitProvider` for ``workspace/repo_slug``."""

    name = "bitbucket"
    supports_pull_requests = True

    def __init__(
        self,
        repo: str,
        token: str,
        *,
        username: str | None = None,
        api_url: str = DEFAULT_API_URL,
        transport: httpx2.BaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.repo = repo.strip("/").removesuffix(".git")
        if self.repo.count("/") != 1:
            raise StorageError("Bitbucket repo must be 'workspace/repo_slug'")
        self.location = f"bitbucket:{self.repo}"
        auth = httpx2.BasicAuth(username, token) if username and token else None
        headers = {"User-Agent": "autoair"}
        if not username and token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx2.Client(
            base_url=api_url.rstrip("/"),
            auth=auth,
            headers=headers,
            transport=transport,
            timeout=timeout,
            **tls.httpx_kwargs("git", transport=transport),
        )

    @property
    def _r(self) -> str:
        return f"/repositories/{self.repo}"

    def _request(
        self, method: str, url: str, *, ok_missing: bool = False, **kwargs: Any
    ) -> httpx2.Response | None:
        try:
            resp = self._client.request(method, url, **kwargs)
        except httpx2.HTTPError as exc:
            raise StorageError(f"Bitbucket request failed: {exc.__class__.__name__}") from exc
        if ok_missing and resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            error = AuthError if resp.status_code in (401, 403) else StorageError
            raise error(f"Bitbucket API error {resp.status_code}: {_message(resp)}")
        return resp

    # ----------------------------------------------------------------- reads
    def get_file(self, ref: str, path: str) -> tuple[str | None, str | None]:
        check_ref(ref)
        resp = self._request(
            "GET", f"{self._r}/src/{quote(ref, safe='')}/{quote(path)}", ok_missing=True
        )
        if resp is None:
            return None, None
        return resp.text, None

    def list_files(self, ref: str, prefix: str) -> list[str]:
        check_ref(ref)
        directory = prefix.strip("/")
        url: str | None = (
            f"{self._r}/src/{quote(ref, safe='')}/{quote(directory)}/"
            if directory
            else f"{self._r}/src/{quote(ref, safe='')}/"
        )
        out: list[str] = []
        while url:
            resp = self._request("GET", url, params={"pagelen": 100}, ok_missing=True)
            if resp is None:
                return out
            data = resp.json()
            out += [
                str(v["path"]) for v in data.get("values", []) if v.get("type") == "commit_file"
            ]
            url = data.get("next")
        return out

    def head(self, branch: str) -> str | None:
        check_branch(branch)
        resp = self._request(
            "GET", f"{self._r}/refs/branches/{quote(branch, safe='')}", ok_missing=True
        )
        return None if resp is None else str(resp.json()["target"]["hash"])

    def token_user(self) -> str | None:
        """Account of the token (audit attribution; ``GET /user``, access tokens of a
        repository answer with their bot account or refuse)."""
        resp = self._request("GET", "/user")
        data = resp.json() if resp is not None else None
        if not isinstance(data, dict):
            return None
        name = data.get("username") or data.get("nickname") or data.get("display_name")
        return str(name) if name else None

    def log(self, path: str, ref: str, limit: int = 50) -> list[CommitInfo]:
        check_ref(ref)
        resp = self._request(
            "GET",
            f"{self._r}/commits/{quote(ref, safe='')}",
            params={"path": path, "pagelen": max(1, min(limit, 100))},
            ok_missing=True,
        )
        if resp is None:
            return []
        out = []
        for item in resp.json().get("values", [])[:limit]:
            raw = str((item.get("author") or {}).get("raw") or "")
            name, email = parse_author(raw, ("", "")) if raw else (None, None)
            out.append(
                CommitInfo(
                    sha=str(item["hash"]),
                    timestamp=_ts(item.get("date")),
                    author_name=name or None,
                    author_email=email or None,
                    message=str(item.get("message") or ""),
                )
            )
        return out

    # ----------------------------------------------------------------- writes
    def create_branch(self, name: str, from_sha: str) -> None:
        check_branch(name)
        resp = self._client.post(
            f"{self._r}/refs/branches", json={"name": name, "target": {"hash": from_sha}}
        )
        if resp.status_code in (400, 409) and "exist" in _message(resp).lower():
            raise ConflictError(None, f"branch {name!r} already exists")
        if resp.status_code >= 400:
            error = AuthError if resp.status_code in (401, 403) else StorageError
            raise error(f"Bitbucket API error {resp.status_code}: {_message(resp)}")

    def reset_branch(self, name: str, to_sha: str, *, expect: str | None = None) -> None:
        check_branch(name)
        if expect is not None and self.head(name) != expect:
            # The host's API has no compare-and-swap for this: checked just before the move.
            raise ConflictError(None, f"branch {name!r} moved; reload and retry")
        self._request("DELETE", f"{self._r}/refs/branches/{quote(name, safe='')}", ok_missing=True)
        self.create_branch(name, to_sha)

    def commit_files(
        self,
        branch: str,
        base_sha: str | None,
        changes: dict[str, str | None],
        message: str,
        author: str | None,
    ) -> str:
        check_branch(branch)
        if self.head(branch) != base_sha:
            raise ConflictError(None, f"branch {branch!r} moved; reload and retry")
        data: dict[str, Any] = {"message": message, "branch": branch}
        if base_sha:
            data["parents"] = base_sha
        if author:
            name, email = parse_author(author, ("AutoAir", "autoair@localhost"))
            data["author"] = f"{name} <{email}>"
        deletes = [p for p, c in sorted(changes.items()) if c is None]
        if deletes:
            data["files"] = deletes
        files = {
            p: (p.rsplit("/", 1)[-1], c.encode("utf-8"))
            for p, c in sorted(changes.items())
            if c is not None
        }
        if not files and not deletes:
            return base_sha or ""
        resp = self._client.post(f"{self._r}/src", data=data, files=files)
        if resp.status_code in (409,) or (
            resp.status_code == 400 and "parent" in _message(resp).lower()
        ):
            raise ConflictError(None, f"branch {branch!r} changed concurrently")
        if resp.status_code >= 400:
            raise StorageError(f"Bitbucket commit failed ({resp.status_code}): {_message(resp)}")
        match = re.search(r"/commit/([0-9a-f]{7,64})", resp.headers.get("location", ""))
        if match:
            return match.group(1)
        new = self.head(branch)
        if new is None or new == base_sha:
            raise StorageError("Bitbucket commit did not move the branch")
        return new

    def open_pr(
        self,
        head: str,
        base: str,
        title: str,
        body: str,
        options: PullRequestOptions | None = None,
    ) -> PullRequest | None:
        opts = options or PullRequestOptions()
        payload: dict[str, Any] = {
            "title": title,
            "description": body,
            "close_source_branch": opts.delete_branch,
            "source": {"branch": {"name": head}},
            "destination": {"branch": {"name": base}},
        }
        if opts.draft:
            payload["draft"] = True
        if opts.reviewers:
            payload["reviewers"] = [
                {"uuid": r} if r.startswith("{") else {"account_id": r} for r in opts.reviewers
            ]
        if opts.labels:
            log.info("Bitbucket pull requests have no labels; ignoring %s", list(opts.labels))
        resp = self._request("POST", f"{self._r}/pullrequests", json=payload)
        assert resp is not None
        data = resp.json()
        return PullRequest(url=str(data["links"]["html"]["href"]), number=int(data["id"]))

    def find_open_pr(self, head: str) -> PullRequest | None:
        query = f'source.branch.name="{head}" AND state="OPEN"'
        resp = self._request("GET", f"{self._r}/pullrequests", params={"q": query}, ok_missing=True)
        if resp is None:
            return None
        values = resp.json().get("values", [])
        if not values:
            return None
        return PullRequest(url=str(values[0]["links"]["html"]["href"]), number=int(values[0]["id"]))

    def close(self) -> None:
        self._client.close()
