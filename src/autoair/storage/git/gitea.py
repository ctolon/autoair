"""Gitea / Forgejo provider: the git CLI pushes, the REST API opens pull requests.

Gitea has no ``createCommitOnBranch``-style commit API, so commits are built and pushed
exactly like :class:`~autoair.storage.git.local_git.LocalGitProvider` does (bare cache
clone, ``--force-with-lease`` as compare-and-swap). Pull requests go through
``POST /api/v1/repos/{owner}/{repo}/pulls`` with the token of the Airflow connection
(``Authorization: token …``); AutoAir never stores it. Forgejo serves the same API.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit

import httpx2

from autoair.net import tls
from autoair.storage.base import AuthError, StorageError
from autoair.storage.git.local_git import LocalGitProvider
from autoair.storage.git.provider import PullRequest, PullRequestOptions, check_branch

log = logging.getLogger(__name__)

_DRAFT_PREFIX = "WIP: "  # Gitea's work-in-progress (draft) marker
_PAGE_SIZE = 50  # Gitea's default maximum page size
_MAX_PAGES = 20  # open pull requests scanned for an existing one (1000)
_HTTP_REPO = re.compile(r"^(?P<base>https?://[^/]+(?:/.*?)?)/(?P<owner>[^/]+)/(?P<repo>[^/]+?)/?$")


def gitea_repo(remote_url: str, api_url: str | None) -> tuple[str, str, str]:
    """``(api_url, owner, repo)`` of a remote URL (``https://host/owner/repo.git``, an
    ``ssh://`` / ``git@host:owner/repo`` URL with ``api_url`` given)."""
    url = remote_url.strip().removesuffix(".git").rstrip("/")
    match = _HTTP_REPO.match(re.sub(r"(?<=://)[^/@\s]+@", "", url))
    if match is not None:
        default_api = f"{match.group('base')}/api/v1"
        owner, repo = match.group("owner"), match.group("repo")
    else:
        default_api = None
        tail = url.split(":", 1)[-1] if "://" not in url else urlsplit(url).path
        parts = [p for p in tail.split("/") if p]
        if len(parts) < 2:
            raise StorageError(f"cannot tell the Gitea owner/repo from {remote_url!r}")
        owner, repo = parts[-2], parts[-1]
    api = (api_url or default_api or "").rstrip("/")
    if not api:
        raise StorageError(
            "provider=gitea needs storage.git.api_url (https://<host>/api/v1) for an SSH remote"
        )
    return api, owner, repo


class GiteaProvider(LocalGitProvider):
    """:class:`~autoair.storage.git.provider.GitProvider` for a Gitea/Forgejo repository."""

    name = "gitea"
    supports_pull_requests = True

    def __init__(
        self,
        remote_url: str,
        cache_dir: Path | None = None,
        *,
        token: str | None = None,
        api_url: str | None = None,
        api_timeout: float = 30.0,
        transport: httpx2.BaseTransport | None = None,
        **git_kwargs: Any,
    ) -> None:
        super().__init__(remote_url, cache_dir, token=token, **git_kwargs)
        self.api_url, self.owner, self.repo_name = gitea_repo(remote_url, api_url)
        headers = {"Accept": "application/json", "User-Agent": "autoair"}
        if token:
            headers["Authorization"] = f"token {token}"
        self._client = httpx2.Client(
            base_url=self.api_url,
            transport=transport,
            timeout=api_timeout,
            headers=headers,
            **tls.httpx_kwargs("git", transport=transport),
        )

    # ----------------------------------------------------------------- http
    @property
    def _repo_path(self) -> str:
        return f"/repos/{quote(self.owner, safe='')}/{quote(self.repo_name, safe='')}"

    def _api(self, method: str, url: str, **kwargs: Any) -> httpx2.Response:
        try:
            resp = self._client.request(method, url, **kwargs)
        except httpx2.HTTPError as exc:
            raise StorageError(f"Gitea request failed: {exc.__class__.__name__}") from exc
        if resp.status_code >= 400 and resp.status_code != 409:
            error = AuthError if resp.status_code in (401, 403) else StorageError
            raise error(f"Gitea API error {resp.status_code}: {_message(resp)}")
        return resp

    def _best_effort(self, what: str, method: str, url: str, **kwargs: Any) -> Any:
        try:
            resp = self._api(method, url, **kwargs)
        except StorageError as exc:
            log.warning("Gitea: could not set pull request %s: %s", what, exc)
            return None
        return resp.json() if resp.content else None

    def token_user(self) -> str | None:
        """Login of the token's owner (audit attribution; ``GET /user``, needs the
        ``read:user`` scope — without it the lookup fails and is skipped)."""
        data = self._api("GET", "/user").json()
        return str(data.get("login")) if isinstance(data, dict) and data.get("login") else None

    # ----------------------------------------------------------------- pull requests
    def open_pr(
        self,
        head: str,
        base: str,
        title: str,
        body: str,
        options: PullRequestOptions | None = None,
    ) -> PullRequest | None:
        check_branch(head)
        check_branch(base)
        opts = options or PullRequestOptions()
        if opts.draft and not title.startswith(_DRAFT_PREFIX):
            title = f"{_DRAFT_PREFIX}{title}"
        payload = {"head": head, "base": base, "title": title, "body": body}
        resp = self._api("POST", f"{self._repo_path}/pulls", json=payload)
        if resp.status_code == 409:  # one is already open for head -> base
            existing = self.find_open_pr(head)
            if existing is None:
                raise StorageError(f"Gitea API error 409: {_message(resp)}")
            return existing
        data = resp.json()
        pr = PullRequest(url=str(data["html_url"]), number=int(data["number"]))
        if opts.labels:
            self._set_labels(pr.number, opts.labels)
        if opts.reviewers:
            users = [r for r in opts.reviewers if "/" not in r]
            teams = [r.split("/", 1)[1] for r in opts.reviewers if "/" in r]
            self._best_effort(
                "reviewers",
                "POST",
                f"{self._repo_path}/pulls/{pr.number}/requested_reviewers",
                json={"reviewers": users, "team_reviewers": teams},
            )
        return pr

    def _set_labels(self, number: int, names: tuple[str, ...]) -> None:
        """Gitea labels pull requests by label id: look the names up first."""
        known = self._best_effort("labels", "GET", f"{self._repo_path}/labels") or []
        ids = {str(lab.get("name")): int(lab["id"]) for lab in known if "id" in lab}
        missing = [n for n in names if n not in ids]
        if missing:
            log.warning("Gitea: unknown labels %s (create them in the repository)", missing)
        wanted = [ids[n] for n in names if n in ids]
        if wanted:
            self._best_effort(
                "labels",
                "POST",
                f"{self._repo_path}/issues/{number}/labels",
                json={"labels": wanted},
            )

    def find_open_pr(self, head: str) -> PullRequest | None:
        check_branch(head)
        for page in range(1, _MAX_PAGES + 1):
            resp = self._api(
                "GET",
                f"{self._repo_path}/pulls",
                params={"state": "open", "page": page, "limit": _PAGE_SIZE},
            )
            items = resp.json() or []
            for item in items:
                ref = (item.get("head") or {}).get("ref")
                if ref == head:
                    return PullRequest(url=str(item["html_url"]), number=int(item["number"]))
            if len(items) < _PAGE_SIZE:
                return None
        return None

    def close(self) -> None:
        self._client.close()


def _message(resp: httpx2.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return resp.text[:300]
    if isinstance(data, dict):
        return str(data.get("message") or data.get("error") or data)[:300]
    return str(data)[:300]
