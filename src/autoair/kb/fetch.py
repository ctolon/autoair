"""Reading documentation: HTTP (SSRF-checked), filesystems / object storage, git.

HTTP fetches go through :mod:`autoair.net.tls` (client ``kb``: CA bundle, client
certificate, proxy) and, without a proxy, through the pinned transport of
:mod:`autoair.ai.net`: every hop (redirects are followed manually) must be on the
allow-list and resolve to a public address, and the connection goes to the checked IP.
Responses are size-capped while streaming; 429/5xx are retried with back-off; a global
token bucket keeps the request rate polite; conditional requests (ETag / Last-Modified)
make refreshes cheap.

Credentials are never configured here: git and object storage read them from an Airflow
connection (``conn_id``) at the moment they are used.
"""

from __future__ import annotations

import fnmatch
import threading
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx2

from autoair.ai.net import PinnedTransport, UrlNotAllowed, check_url_sync
from autoair.kb.config import KbFetchSettings
from autoair.net import tls

_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
#: Longest Retry-After honoured (s): a server asking for more makes the page fail.
_MAX_RETRY_AFTER = 60.0


class FetchError(Exception):
    """A page could not be read (reported in the status, the run continues)."""


class PageTooLarge(FetchError):
    pass


@dataclass
class Response:
    url: str
    final_url: str
    status: int
    content: bytes = b""
    content_type: str = ""
    etag: str | None = None
    last_modified: str | None = None

    @property
    def not_modified(self) -> bool:
        return self.status == 304

    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")


class RateLimiter:
    """A token bucket shared by every fetch thread of one run."""

    def __init__(self, per_second: float) -> None:
        self.rate = per_second
        self.capacity = max(1.0, per_second)
        self.tokens = self.capacity
        self.updated = time.monotonic()
        self._lock = threading.Lock()

    def wait(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                delay = (1 - self.tokens) / self.rate
            time.sleep(delay)


class HttpFetcher:
    """Blocking HTTP client of one ingestion run (thread-safe)."""

    def __init__(
        self,
        settings: KbFetchSettings,
        *,
        transport: httpx2.BaseTransport | None = None,
        resolve_dns: bool = True,
    ) -> None:
        self.settings = settings
        self.limiter = RateLimiter(settings.requests_per_second)
        self._transport = transport
        #: False only in tests with a mock transport (no DNS); the allow-list still applies.
        self._resolve_dns = resolve_dns
        self._clients: dict[bool, httpx2.Client] = {}
        self._lock = threading.Lock()

    def _client(self, pinned: bool) -> httpx2.Client:
        with self._lock:
            client = self._clients.get(pinned)
            if client is None:
                transport: httpx2.BaseTransport | None = self._transport
                if pinned:
                    inner = transport or httpx2.HTTPTransport(**tls.transport_kwargs("kb"))
                    transport = PinnedTransport(inner)
                options = tls.httpx_kwargs("kb", transport=transport)
                client = httpx2.Client(
                    timeout=self.settings.timeout_seconds,
                    follow_redirects=False,
                    headers={"user-agent": self.settings.user_agent},
                    transport=transport,
                    **options,
                )
                self._clients[pinned] = client
            return client

    def close(self) -> None:
        with self._lock:
            for client in self._clients.values():
                client.close()
            self._clients.clear()

    def get(
        self,
        url: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
        domains: Sequence[str] = (),
        allow_private: bool = False,
    ) -> Response:
        """GET ``url`` (conditional when ``etag``/``last_modified`` are given). With
        ``allow_private`` (an internal mirror) only ``domains`` are allowed, not the
        public allow-list: a private address is accepted for the mirror's host alone."""
        allowed = list(domains) if allow_private else [*self.settings.allowed_domains, *domains]
        headers: dict[str, str] = {"accept": "*/*"}
        if etag:
            headers["if-none-match"] = etag
        if last_modified:
            headers["if-modified-since"] = last_modified
        current = url
        # A proxy resolves the host itself; a private mirror is reached as configured.
        pinned = self._resolve_dns and not allow_private and tls.proxy_for("kb", url) is None
        client = self._client(pinned)
        for _ in range(self.settings.max_redirects + 1):
            check_url_sync(
                current,
                domains=allowed,
                allow_private=allow_private or not self._resolve_dns,
            )
            response = self._attempt(client, current, headers)
            if response.status in (301, 302, 303, 307, 308):
                if not response.final_url:
                    raise FetchError(f"redirect without a location: {current}")
                current = response.final_url
                continue
            response.url = url
            return response
        raise FetchError(f"too many redirects (> {self.settings.max_redirects}): {url}")

    def _attempt(self, client: httpx2.Client, url: str, headers: dict[str, str]) -> Response:
        delay = 1.0
        for attempt in range(self.settings.retries + 1):
            self.limiter.wait()
            try:
                return self._once(client, url, headers)
            except _Retry as retry:
                if attempt >= self.settings.retries:
                    raise FetchError(f"HTTP {retry.status} from {url}") from None
                wait = retry.after if retry.after is not None else delay
                if wait > _MAX_RETRY_AFTER:
                    raise FetchError(f"HTTP {retry.status} from {url} (retry later)") from None
                time.sleep(wait)
                delay = min(delay * 2, _MAX_RETRY_AFTER)
            except httpx2.TimeoutException as exc:
                if attempt >= self.settings.retries:
                    raise FetchError(f"timeout reading {url}") from exc
                time.sleep(delay)
                delay = min(delay * 2, _MAX_RETRY_AFTER)
            except httpx2.HTTPError as exc:
                raise FetchError(f"cannot read {url}: {exc}") from exc
        raise FetchError(f"cannot read {url}")  # pragma: no cover - loop always returns

    def _once(self, client: httpx2.Client, url: str, headers: dict[str, str]) -> Response:
        with client.stream("GET", url, headers=headers) as resp:
            status = resp.status_code
            if status in (301, 302, 303, 307, 308):
                location = resp.headers.get("location")
                return Response(url, urljoin(url, location) if location else "", status)
            if status in _RETRY_STATUS:
                after = resp.headers.get("retry-after")
                raise _Retry(status, float(after) if after and after.isdigit() else None)
            body = bytearray()
            if status == 200:
                for chunk in resp.iter_bytes():
                    body.extend(chunk)
                    if len(body) > self.settings.max_page_bytes:
                        raise PageTooLarge(
                            f"{url} is larger than {self.settings.max_page_bytes} bytes"
                        )
            return Response(
                url=url,
                final_url=url,
                status=status,
                content=bytes(body),
                content_type=resp.headers.get("content-type", "").split(";")[0].strip(),
                etag=resp.headers.get("etag"),
                last_modified=resp.headers.get("last-modified"),
            )


class _Retry(Exception):
    def __init__(self, status: int, after: float | None) -> None:
        super().__init__(status)
        self.status = status
        self.after = after


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


# --------------------------------------------------------------------------- filesystems


def open_fs(url: str, conn_id: str | None, storage_options: dict[str, Any]) -> tuple[Any, str]:
    """(fsspec filesystem, root path) of a folder or object-storage URL. With ``conn_id``
    the filesystem is Airflow's ObjectStoragePath for that connection (credentials stay
    in Airflow)."""
    if conn_id:
        import importlib

        path_cls: Any = None
        for module in ("airflow.sdk", "airflow.io.path"):
            try:
                path_cls = importlib.import_module(module).ObjectStoragePath
                break
            except (ImportError, AttributeError):
                continue
        if path_cls is None:
            raise FetchError("conn_id needs Airflow's ObjectStoragePath (apache-airflow installed)")
        path = path_cls(url, conn_id=conn_id)
        return path.fs, str(path.path).rstrip("/")
    import fsspec

    fs, root = fsspec.core.url_to_fs(url, **storage_options)
    return fs, str(root).rstrip("/")


def walk_files(
    fs: Any, root: str, suffixes: Sequence[str], *, limit: int
) -> Iterator[tuple[str, str]]:
    """(absolute path, path relative to ``root``) of the files with one of ``suffixes``."""
    count = 0
    try:
        found = fs.find(root)
    except (FileNotFoundError, OSError) as exc:
        raise FetchError(f"cannot list {root}: {exc}") from exc
    for path in sorted(str(p) for p in found):
        rel = path[len(root) :].lstrip("/") if path.startswith(root) else path
        if any(part.startswith(".") for part in rel.split("/")):
            continue
        if not rel.lower().endswith(tuple(suffixes)):
            continue
        yield path, rel
        count += 1
        if count >= limit:
            return


def read_file(fs: Any, path: str, max_bytes: int) -> bytes:
    try:
        size = fs.size(path)
    except (FileNotFoundError, OSError):
        size = None
    if size is not None and size > max_bytes:
        raise PageTooLarge(f"{path} is larger than {max_bytes} bytes")
    with fs.open(path, "rb") as fh:
        data: bytes = fh.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise PageTooLarge(f"{path} is larger than {max_bytes} bytes")
    return data


def matches(path: str, include: Sequence[str], exclude: Sequence[str]) -> bool:
    if include and not any(fnmatch.fnmatchcase(path, p) for p in include):
        return False
    return not any(fnmatch.fnmatchcase(path, p) for p in exclude)


# --------------------------------------------------------------------------- git


def checkout(
    url: str | None,
    ref: str | None,
    dest: Path,
    *,
    conn_id: str | None,
    sparse: Sequence[str] = (),
    timeout: float = 900,
) -> tuple[Path, str]:
    """Shallow checkout of ``ref`` into ``dest``; returns (folder, commit sha). The git
    credentials come from the Airflow connection ``conn_id`` (token through
    ``GIT_CONFIG_*`` environment variables, never argv or the remote URL)."""
    from autoair.storage.base import StorageError
    from autoair.storage.git.local_git import LocalGitProvider

    token = username = private_key = key_file = None
    if conn_id:
        from autoair.storage.git.config import credentials_from_connection

        creds = credentials_from_connection(conn_id)
        if creds is None:
            raise FetchError(f"cannot read the git connection {conn_id!r}")
        url = url or creds.repo_url
        token, username = creds.token, creds.username
        private_key, key_file = creds.private_key, creds.key_file
    if not url:
        raise FetchError("git source without a url")
    if "://" in url and (urlsplit(url).username or urlsplit(url).password):
        raise FetchError("git url must not contain credentials (use conn_id)")

    class _Checkout(LocalGitProvider):
        def run(self, *args: str) -> str:
            return self._git(*args, bare=False).stdout.strip()

    repo = _Checkout(
        url,
        dest,
        token=token,
        username=username or "x-access-token",
        private_key=private_key,
        ssh_key_file=Path(key_file) if key_file else None,
        command_timeout=timeout,
    )
    work = dest / "worktree"
    try:
        if not (work / ".git").exists():
            work.mkdir(parents=True, exist_ok=True)
            repo.run("init", "-q", str(work))
            repo.run("-C", str(work), "remote", "add", "origin", url)
        else:
            repo.run("-C", str(work), "remote", "set-url", "origin", url)
        if sparse:
            repo.run("-C", str(work), "sparse-checkout", "set", "--no-cone", *sparse)
        repo.run("-C", str(work), "fetch", "-q", "--depth", "1", "origin", ref or "HEAD")
        repo.run("-C", str(work), "checkout", "-q", "--force", "FETCH_HEAD")
        sha = repo.run("-C", str(work), "rev-parse", "HEAD")
    except StorageError as exc:
        raise FetchError(f"git checkout of {repo.location} failed: {exc}") from exc
    return work, sha


__all__ = [
    "FetchError",
    "HttpFetcher",
    "PageTooLarge",
    "RateLimiter",
    "Response",
    "UrlNotAllowed",
    "airflow_connection",
    "checkout",
    "host_of",
    "matches",
    "open_fs",
    "read_file",
    "walk_files",
]


def airflow_connection(conn_id: str) -> Any:
    """An Airflow connection (env vars, secrets backend, metadata DB, then the Task SDK);
    raises :class:`FetchError` without exposing anything of it."""
    from autoair._compat import AirflowConnectionError, get_airflow_connection

    try:
        return get_airflow_connection(conn_id)
    except AirflowConnectionError as exc:
        raise FetchError(f"cannot read: {exc}") from exc
