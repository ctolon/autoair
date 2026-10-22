"""GitLab (gitlab.com or self-managed) provider using the REST API v4.

GitLab's commit API has no "expected head" parameter, so compare-and-swap is done by
checking the branch head right before committing (a small race window remains).
"""

from __future__ import annotations

import base64
import datetime
import logging
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

DEFAULT_API_URL = "https://gitlab.com/api/v4"


def _enc(value: str) -> str:
    return quote(value, safe="")


def _message(resp: httpx2.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return resp.text[:300]
    if isinstance(data, dict):
        return str(data.get("message") or data.get("error") or data)[:300]
    return str(data)[:300]


def _ts(value: Any) -> datetime.datetime | None:
    if not value:
        return None
    return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))


class GitLabProvider:
    """:class:`~autoair.storage.git.provider.GitProvider` for a GitLab project."""

    name = "gitlab"
    supports_pull_requests = True

    def __init__(
        self,
        project: str | int,
        token: str,
        *,
        api_url: str = DEFAULT_API_URL,
        transport: httpx2.BaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.project = str(project).strip("/").removesuffix(".git")
        self.api_url = api_url.rstrip("/")
        self.location = f"gitlab:{self.project}"
        self._client = httpx2.Client(
            base_url=self.api_url,
            transport=transport,
            timeout=timeout,
            headers={"PRIVATE-TOKEN": token, "User-Agent": "autoair"}
            if token
            else {"User-Agent": "autoair"},
            **tls.httpx_kwargs("git", transport=transport),
        )

    @property
    def _p(self) -> str:
        return f"/projects/{_enc(self.project)}"

    def _request(
        self, method: str, url: str, *, ok_missing: bool = False, **kwargs: Any
    ) -> httpx2.Response | None:
        try:
            resp = self._client.request(method, url, **kwargs)
        except httpx2.HTTPError as exc:
            raise StorageError(f"GitLab request failed: {exc.__class__.__name__}") from exc
        if ok_missing and resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            error = AuthError if resp.status_code in (401, 403) else StorageError
            raise error(f"GitLab API error {resp.status_code}: {_message(resp)}")
        return resp

    def _json(self, method: str, url: str, **kwargs: Any) -> Any:
        resp = self._request(method, url, **kwargs)
        assert resp is not None
        return resp.json()

    # ----------------------------------------------------------------- reads
    def get_file(self, ref: str, path: str) -> tuple[str | None, str | None]:
        check_ref(ref)
        resp = self._request(
            "GET", f"{self._p}/repository/files/{_enc(path)}", params={"ref": ref}, ok_missing=True
        )
        if resp is None:
            return None, None
        data = resp.json()
        content = base64.b64decode(str(data.get("content", ""))).decode("utf-8")
        return content, str(data.get("blob_id")) if data.get("blob_id") else None

    def _exists(self, ref: str, path: str) -> bool:
        resp = self._request(
            "HEAD", f"{self._p}/repository/files/{_enc(path)}", params={"ref": ref}, ok_missing=True
        )
        return resp is not None

    def list_files(self, ref: str, prefix: str) -> list[str]:
        check_ref(ref)
        out: list[str] = []
        page: str | None = "1"
        while page:
            params: dict[str, Any] = {"ref": ref, "per_page": 100, "page": page}
            if prefix.strip("/"):
                params["path"] = prefix.strip("/")
            resp = self._request(
                "GET", f"{self._p}/repository/tree", params=params, ok_missing=True
            )
            if resp is None:
                return out
            out.extend(str(e["path"]) for e in resp.json() if e.get("type") == "blob")
            page = resp.headers.get("x-next-page") or None
        return out

    def head(self, branch: str) -> str | None:
        check_branch(branch)
        resp = self._request(
            "GET", f"{self._p}/repository/branches/{_enc(branch)}", ok_missing=True
        )
        if resp is None:
            return None
        return str(resp.json()["commit"]["id"])

    def token_user(self) -> str | None:
        """Username of the token's owner (audit attribution; ``GET /user``)."""
        data = self._json("GET", "/user")
        name = data.get("username") if isinstance(data, dict) else None
        return str(name) if name else None

    def log(self, path: str, ref: str, limit: int = 50) -> list[CommitInfo]:
        check_ref(ref)
        resp = self._request(
            "GET",
            f"{self._p}/repository/commits",
            params={"path": path, "ref_name": ref, "per_page": max(1, min(limit, 100))},
            ok_missing=True,
        )
        if resp is None:
            return []
        return [
            CommitInfo(
                sha=str(item["id"]),
                author_name=item.get("author_name"),
                author_email=item.get("author_email"),
                timestamp=_ts(item.get("authored_date") or item.get("created_at")),
                message=str(item.get("message") or item.get("title") or ""),
            )
            for item in resp.json()[:limit]
        ]

    # ----------------------------------------------------------------- writes
    def create_branch(self, name: str, from_sha: str) -> None:
        check_branch(name)
        resp = self._client.post(
            f"{self._p}/repository/branches", params={"branch": name, "ref": from_sha}
        )
        if resp.status_code == 400 and "already exists" in _message(resp).lower():
            raise ConflictError(None, f"branch {name!r} already exists")
        if resp.status_code >= 400:
            error = AuthError if resp.status_code in (401, 403) else StorageError
            raise error(f"GitLab API error {resp.status_code}: {_message(resp)}")

    def reset_branch(self, name: str, to_sha: str, *, expect: str | None = None) -> None:
        check_branch(name)
        if expect is not None and self.head(name) != expect:
            # The host's API has no compare-and-swap for this: checked just before the move.
            raise ConflictError(None, f"branch {name!r} moved; reload and retry")
        self._request("DELETE", f"{self._p}/repository/branches/{_enc(name)}", ok_missing=True)
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
        actions: list[dict[str, Any]] = []
        for path, content in sorted(changes.items()):
            exists = base_sha is not None and self._exists(base_sha, path)
            if content is None:
                if exists:
                    actions.append({"action": "delete", "file_path": path})
                continue
            actions.append(
                {
                    "action": "update" if exists else "create",
                    "file_path": path,
                    "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
                    "encoding": "base64",
                }
            )
        if not actions:
            return base_sha or ""
        payload: dict[str, Any] = {
            "branch": branch,
            "commit_message": message,
            "actions": actions,
        }
        if author:
            name, email = parse_author(author, ("AutoAir", "autoair@localhost"))
            payload.update(author_name=name, author_email=email)
        resp = self._client.post(f"{self._p}/repository/commits", json=payload)
        if resp.status_code >= 400:
            text = _message(resp)
            if resp.status_code in (400, 409) and (
                "already exists" in text.lower() or "doesn't exist" in text.lower()
            ):
                raise ConflictError(None, f"branch {branch!r} changed concurrently: {text}")
            raise StorageError(f"GitLab commit failed ({resp.status_code}): {text}")
        data = resp.json()
        parents = data.get("parent_ids") or []
        if base_sha and parents and parents[0] != base_sha:
            log.warning("branch %s moved while committing; changes were stacked on top", branch)
        return str(data["id"])

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
            "source_branch": head,
            "target_branch": base,
            "title": f"Draft: {title}" if opts.draft else title,
            "description": body,
            "remove_source_branch": opts.delete_branch,
        }
        if opts.labels:
            payload["labels"] = ",".join(opts.labels)
        reviewer_ids = [i for i in (self._user_id(r) for r in opts.reviewers) if i is not None]
        if reviewer_ids:
            payload["reviewer_ids"] = reviewer_ids
        data = self._json("POST", f"{self._p}/merge_requests", json=payload)
        return PullRequest(url=str(data["web_url"]), number=int(data["iid"]))

    def _user_id(self, reviewer: str) -> int | None:
        """A numeric id, or the id of a username (``/users?username=``)."""
        if reviewer.isdigit():
            return int(reviewer)
        try:
            resp = self._request("GET", "/users", params={"username": reviewer.lstrip("@")})
            users = resp.json() if resp is not None else []
        except (StorageError, ValueError) as exc:
            log.warning("GitLab: cannot resolve reviewer %r: %s", reviewer, exc)
            return None
        if not users:
            log.warning("GitLab: unknown reviewer %r", reviewer)
            return None
        return int(users[0]["id"])

    def find_open_pr(self, head: str) -> PullRequest | None:
        resp = self._request(
            "GET",
            f"{self._p}/merge_requests",
            params={"source_branch": head, "state": "opened"},
            ok_missing=True,
        )
        if resp is None:
            return None
        items = resp.json()
        if not items:
            return None
        return PullRequest(url=str(items[0]["web_url"]), number=int(items[0]["iid"]))

    def close(self) -> None:
        self._client.close()
