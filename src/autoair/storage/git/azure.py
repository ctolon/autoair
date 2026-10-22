"""Azure DevOps Repos provider (REST API 7.1).

Pushes carry ``refUpdates[].oldObjectId`` = expected head, so compare-and-swap is atomic.
Auth: personal access token (Basic) or an Entra ID bearer token (``bearer=True``).
"""

from __future__ import annotations

import base64
import datetime
from typing import Any

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

API_VERSION = "7.1"
ZERO = "0" * 40
_SHA_LEN = 40


def _message(resp: httpx2.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return resp.text[:300]
    if isinstance(data, dict):
        return str(data.get("message") or data)[:300]
    return str(data)[:300]


def _ts(value: Any) -> datetime.datetime | None:
    if not value:
        return None
    return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _version(ref: str) -> dict[str, str]:
    is_sha = len(ref) == _SHA_LEN and all(c in "0123456789abcdefABCDEF" for c in ref)
    return {
        "versionDescriptor.version": ref,
        "versionDescriptor.versionType": "commit" if is_sha else "branch",
    }


class AzureDevOpsProvider:
    """:class:`~autoair.storage.git.provider.GitProvider` for ``org/project/repository``."""

    name = "azure"
    supports_pull_requests = True

    def __init__(
        self,
        repo: str,
        token: str,
        *,
        api_url: str = "https://dev.azure.com",
        bearer: bool = False,
        transport: httpx2.BaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        parts = repo.strip("/").removesuffix(".git").split("/")
        if len(parts) != 3:
            raise StorageError("Azure DevOps repo must be 'organization/project/repository'")
        self.org, self.project, self.repository = parts
        self.base = api_url.rstrip("/")
        self.location = f"azure:{'/'.join(parts)}"
        headers = {"User-Agent": "autoair"}
        auth = None
        if bearer and token:
            headers["Authorization"] = f"Bearer {token}"
        elif token:
            auth = httpx2.BasicAuth("", token)
        self._client = httpx2.Client(
            base_url=f"{self.base}/{self.org}/{self.project}/_apis/git/repositories/"
            f"{self.repository}",
            auth=auth,
            headers=headers,
            transport=transport,
            timeout=timeout,
            params={"api-version": API_VERSION},
            **tls.httpx_kwargs("git", transport=transport),
        )

    def _request(
        self, method: str, url: str, *, ok_missing: bool = False, **kwargs: Any
    ) -> httpx2.Response | None:
        try:
            resp = self._client.request(method, url, **kwargs)
        except httpx2.HTTPError as exc:
            raise StorageError(f"Azure DevOps request failed: {exc.__class__.__name__}") from exc
        if ok_missing and resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            error = AuthError if resp.status_code in (401, 403) else StorageError
            raise error(f"Azure DevOps API error {resp.status_code}: {_message(resp)}")
        return resp

    # ----------------------------------------------------------------- reads
    def get_file(self, ref: str, path: str) -> tuple[str | None, str | None]:
        check_ref(ref)
        resp = self._request(
            "GET",
            "/items",
            ok_missing=True,
            params={
                "path": f"/{path.lstrip('/')}",
                "includeContent": "true",
                "$format": "json",
                **_version(ref),
            },
        )
        if resp is None:
            return None, None
        data = resp.json()
        return str(data.get("content", "")), data.get("objectId")

    def list_files(self, ref: str, prefix: str) -> list[str]:
        check_ref(ref)
        scope = f"/{prefix.strip('/')}" if prefix.strip("/") else "/"
        resp = self._request(
            "GET",
            "/items",
            ok_missing=True,
            params={"scopePath": scope, "recursionLevel": "OneLevel", **_version(ref)},
        )
        if resp is None:
            return []
        return [
            str(v["path"]).lstrip("/")
            for v in resp.json().get("value", [])
            if not v.get("isFolder")
        ]

    def head(self, branch: str) -> str | None:
        check_branch(branch)
        resp = self._request("GET", "/refs", params={"filter": f"heads/{branch}"}, ok_missing=True)
        if resp is None:
            return None
        for ref in resp.json().get("value", []):
            if ref.get("name") == f"refs/heads/{branch}":
                return str(ref["objectId"])
        return None

    def token_user(self) -> str | None:
        """Display name of the PAT's owner (audit attribution; organization
        ``_apis/connectionData``)."""
        resp = self._request(
            "GET",
            f"{self.base}/{self.org}/_apis/connectionData",
            params={"api-version": f"{API_VERSION}-preview"},
        )
        data = resp.json() if resp is not None else None
        user = data.get("authenticatedUser") if isinstance(data, dict) else None
        if not isinstance(user, dict):
            return None
        name = user.get("providerDisplayName") or user.get("customDisplayName")
        return str(name) if name else None

    def log(self, path: str, ref: str, limit: int = 50) -> list[CommitInfo]:
        check_ref(ref)
        version = _version(ref)
        resp = self._request(
            "GET",
            "/commits",
            ok_missing=True,
            params={
                "searchCriteria.itemPath": f"/{path.lstrip('/')}",
                "searchCriteria.itemVersion.version": version["versionDescriptor.version"],
                "searchCriteria.itemVersion.versionType": version["versionDescriptor.versionType"],
                "searchCriteria.$top": max(1, min(limit, 100)),
            },
        )
        if resp is None:
            return []
        return [
            CommitInfo(
                sha=str(c["commitId"]),
                timestamp=_ts((c.get("author") or {}).get("date")),
                author_name=(c.get("author") or {}).get("name"),
                author_email=(c.get("author") or {}).get("email"),
                message=str(c.get("comment") or ""),
            )
            for c in resp.json().get("value", [])[:limit]
        ]

    # ----------------------------------------------------------------- writes
    def _update_ref(self, name: str, old: str, new: str) -> None:
        resp = self._request(
            "POST",
            "/refs",
            json=[{"name": f"refs/heads/{name}", "oldObjectId": old, "newObjectId": new}],
        )
        assert resp is not None
        results = resp.json().get("value", [])
        if results and not results[0].get("success", True):
            status = str(results[0].get("updateStatus", "failed"))
            if "stale" in status.lower() or "exists" in status.lower():
                raise ConflictError(None, f"branch {name!r}: {status}")
            raise StorageError(f"Azure DevOps ref update failed: {status}")

    def create_branch(self, name: str, from_sha: str) -> None:
        check_branch(name)
        self._update_ref(name, ZERO, from_sha)

    def reset_branch(self, name: str, to_sha: str, *, expect: str | None = None) -> None:
        check_branch(name)
        current = expect or self.head(name)
        self._update_ref(name, current or ZERO, to_sha)  # oldObjectId: compare-and-swap

    def commit_files(
        self,
        branch: str,
        base_sha: str | None,
        changes: dict[str, str | None],
        message: str,
        author: str | None,
    ) -> str:
        check_branch(branch)
        items: list[dict[str, Any]] = []
        for path, content in sorted(changes.items()):
            exists = base_sha is not None and self.get_file(base_sha, path)[0] is not None
            item = {"path": f"/{path.lstrip('/')}"}
            if content is None:
                if exists:
                    items.append({"changeType": "delete", "item": item})
                continue
            items.append(
                {
                    "changeType": "edit" if exists else "add",
                    "item": item,
                    "newContent": {
                        "content": base64.b64encode(content.encode()).decode("ascii"),
                        "contentType": "base64encoded",
                    },
                }
            )
        if not items:
            return base_sha or ""
        commit: dict[str, Any] = {"comment": message, "changes": items}
        if author:
            name, email = parse_author(author, ("AutoAir", "autoair@localhost"))
            commit["author"] = {"name": name, "email": email}
        resp = self._client.post(
            "/pushes",
            json={
                "refUpdates": [{"name": f"refs/heads/{branch}", "oldObjectId": base_sha or ZERO}],
                "commits": [commit],
            },
        )
        if resp.status_code == 409 or (resp.status_code == 400 and "TF401028" in resp.text):
            raise ConflictError(None, f"branch {branch!r} changed concurrently")
        if resp.status_code >= 400:
            raise StorageError(f"Azure DevOps push failed ({resp.status_code}): {_message(resp)}")
        return str(resp.json()["commits"][0]["commitId"])

    def _pr_url(self, number: int) -> str:
        return f"{self.base}/{self.org}/{self.project}/_git/{self.repository}/pullrequest/{number}"

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
            "sourceRefName": f"refs/heads/{head}",
            "targetRefName": f"refs/heads/{base}",
            "title": title,
            "description": body,
            "isDraft": opts.draft,
            "completionOptions": {"deleteSourceBranch": opts.delete_branch},
        }
        if opts.labels:
            payload["labels"] = [{"name": label} for label in opts.labels]
        if opts.reviewers:
            payload["reviewers"] = [{"id": r} for r in opts.reviewers]
        resp = self._request("POST", "/pullrequests", json=payload)
        assert resp is not None
        number = int(resp.json()["pullRequestId"])
        return PullRequest(url=self._pr_url(number), number=number)

    def find_open_pr(self, head: str) -> PullRequest | None:
        resp = self._request(
            "GET",
            "/pullrequests",
            ok_missing=True,
            params={
                "searchCriteria.sourceRefName": f"refs/heads/{head}",
                "searchCriteria.status": "active",
            },
        )
        if resp is None:
            return None
        values = resp.json().get("value", [])
        if not values:
            return None
        number = int(values[0]["pullRequestId"])
        return PullRequest(url=self._pr_url(number), number=number)

    def close(self) -> None:
        self._client.close()
