"""Git provider driving the ``git`` CLI against any remote (plain push, no PR API).

A dedicated *bare* cache repository is used (never Airflow's own bundle clones): files
are read with ``git ls-tree`` / ``cat-file`` from remote-tracking refs and commits are
built with plumbing commands on a temporary index, then pushed with
``--force-with-lease`` so the remote branch acts as a compare-and-swap register.
"""

from __future__ import annotations

import base64
import contextlib
import datetime
import hashlib
import logging
import os
import re
import shlex
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path

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

_SHA_RE = re.compile(r"^[0-9a-f]{40,64}$")
_USERINFO_RE = re.compile(r"(?<=://)[^/@\s]+@")
# stderr of a git command the remote refused for its credentials (HTTP and SSH).
_AUTH_FAILED_RE = re.compile(
    r"authentication failed|could not read (username|password)|invalid (username or )?"
    r"(password|credentials)|permission denied \(publickey|terminal prompts disabled|"
    r"returned error: 40[13]|http (basic: )?access denied|unauthori[sz]ed",
    re.IGNORECASE,
)


def git_failure(what: str, stderr: str) -> StorageError:
    """:class:`AuthError` when ``stderr`` says the credentials were refused."""
    error = AuthError if _AUTH_FAILED_RE.search(stderr) else StorageError
    return error(f"{what}: {stderr}")


_CACHE_LOCKS: dict[Path, threading.RLock] = {}
_CACHE_LOCKS_GUARD = threading.Lock()


def cache_lock(cache_dir: Path) -> threading.RLock:
    """One lock per clone cache: providers built per request (a token the user typed, a
    user's own connection) share the cache of the configured one without racing on it."""
    with _CACHE_LOCKS_GUARD:
        return _CACHE_LOCKS.setdefault(cache_dir, threading.RLock())


def redact_url(url: str) -> str:
    return _USERINFO_RE.sub("***@", url)


def default_cache_dir(remote_url: str) -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    digest = hashlib.sha256(redact_url(remote_url).encode()).hexdigest()[:16]
    return base / "autoair" / "git" / digest


class LocalGitProvider:
    """:class:`~autoair.storage.git.provider.GitProvider` using the ``git`` executable."""

    name = "local"
    supports_pull_requests = False

    def __init__(
        self,
        remote_url: str,
        cache_dir: Path | None = None,
        *,
        token: str | None = None,
        username: str = "x-access-token",
        ssh_key_file: Path | None = None,
        private_key: str | None = None,
        fetch_interval: float = 0.0,
        committer: tuple[str, str] = ("AutoAir", "autoair@localhost"),
        git_binary: str = "git",
        command_timeout: float = 300,
    ) -> None:
        self.remote_url = remote_url
        self.location = redact_url(remote_url)
        self.cache_dir = (cache_dir or default_cache_dir(remote_url)).expanduser().resolve()
        self.repo = self.cache_dir / "repo.git"
        self._token = token
        self._username = username
        self._ssh_key_file = ssh_key_file
        self._private_key = private_key
        self.fetch_interval = fetch_interval
        self.committer = committer
        self.git_binary = git_binary
        self.command_timeout = command_timeout
        self._lock = cache_lock(self.cache_dir)
        self._last_fetch = 0.0
        self._initialised = False

    # ----------------------------------------------------------------- plumbing
    def _env(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        env = dict(os.environ)
        env.update(
            GIT_TERMINAL_PROMPT="0",
            GIT_ASKPASS="echo",
            LC_ALL="C",
            GIT_COMMITTER_NAME=self.committer[0],
            GIT_COMMITTER_EMAIL=self.committer[1],
        )
        # ``git -c`` options through the environment, so nothing (token) shows up in argv:
        # TLS/proxy options of AUTOAIR_OUTBOUND__* (autoair.net.tls) and the token header.
        config = tls.git_config()
        if not tls.trust_env("git"):
            tls.strip_proxy_env(env)
        env.update(tls.git_env())
        if self._token:
            # Through GIT_CONFIG_* (environment): never in argv, the remote URL or .git/config.
            config.append(("http.extraHeader", f"Authorization: Basic {self._basic()}"))
            # No credential helper may store or offer anything for this remote.
            config.append(("credential.helper", ""))
        if config:
            env["GIT_CONFIG_COUNT"] = str(len(config))
            for i, (key, value) in enumerate(config):
                env[f"GIT_CONFIG_KEY_{i}"] = key
                env[f"GIT_CONFIG_VALUE_{i}"] = value
        key_file = self._key_file()
        if key_file is not None:
            env["GIT_SSH_COMMAND"] = (
                f"ssh -i {shlex.quote(str(key_file))} -o IdentitiesOnly=yes "
                "-o StrictHostKeyChecking=accept-new"
            )
        if extra:
            env.update(extra)
        return env

    def _key_file(self) -> Path | None:
        if self._ssh_key_file is not None:
            return self._ssh_key_file.expanduser()
        if not self._private_key:
            return None
        path = self.cache_dir / "id_autoair"
        if not path.exists():
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(self._private_key.strip() + "\n")
        return path

    def _redact(self, text: str) -> str:
        text = _USERINFO_RE.sub("***@", text)
        if self._token:
            for form in (self._token, self._basic()):
                text = text.replace(form, "***")
        return text.strip()

    def _basic(self) -> str:
        """The ``user:token`` Basic credentials of the ``http.extraHeader`` (base64)."""
        return base64.b64encode(f"{self._username}:{self._token}".encode()).decode()

    def _git(
        self,
        *args: str,
        stdin: str | None = None,
        check: bool = True,
        env: dict[str, str] | None = None,
        bare: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        cmd = [self.git_binary, *(("--git-dir", str(self.repo)) if bare else ()), *args]
        proc = subprocess.run(
            cmd,
            input=stdin,
            capture_output=True,
            text=True,
            env=self._env(env),
            check=False,
            timeout=self.command_timeout,
        )
        if check and proc.returncode != 0:
            raise git_failure(f"git {args[0]} failed", self._redact(proc.stderr))
        return proc

    def _ensure_repo(self) -> None:
        if self._initialised:
            return
        if not (self.repo / "HEAD").exists():
            self.repo.mkdir(parents=True, exist_ok=True)
            self._git("init", "--bare", "-q", str(self.repo), bare=False)
            self._git("remote", "add", "origin", self.remote_url)
        else:
            self._git("remote", "set-url", "origin", self.remote_url)
        self._git("config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
        self._initialised = True

    def _fetch(self, *, force: bool = False) -> None:
        self._ensure_repo()
        now = time.monotonic()
        if not force and self._last_fetch and now - self._last_fetch < self.fetch_interval:
            return
        self._git("fetch", "--prune", "-q", "origin")
        self._last_fetch = now

    def _rev(self, ref: str) -> str | None:
        """Resolve a branch name or a commit sha to something ``git`` accepts."""
        check_ref(ref)
        candidate = f"refs/remotes/origin/{ref}"
        if self._git("rev-parse", "--verify", "-q", candidate, check=False).returncode == 0:
            return candidate
        proc = self._git("rev-parse", "--verify", "-q", f"{ref}^{{commit}}", check=False)
        return proc.stdout.strip() if proc.returncode == 0 else None

    @contextlib.contextmanager
    def _session(self) -> Iterator[None]:
        with self._lock:
            self._fetch()
            yield

    # ----------------------------------------------------------------- reads
    def get_file(self, ref: str, path: str) -> tuple[str | None, str | None]:
        with self._session():
            rev = self._rev(ref)
            if rev is None:
                return None, None
            listing = self._git("ls-tree", rev, "--", path).stdout.strip()
            if not listing:
                return None, None
            meta, _, _ = listing.partition("\t")
            _mode, kind, sha = meta.split()
            if kind != "blob":
                return None, None
            return self._git("cat-file", "blob", sha).stdout, sha

    def list_files(self, ref: str, prefix: str) -> list[str]:
        with self._session():
            rev = self._rev(ref)
            if rev is None:
                return []
            prefix = prefix.strip("/")
            args = ["ls-tree", rev] + (["--", f"{prefix}/"] if prefix else [])
            out: list[str] = []
            for line in self._git(*args).stdout.splitlines():
                meta, _, name = line.partition("\t")
                if meta.split()[1] == "blob":
                    out.append(name)
            return out

    def head(self, branch: str) -> str | None:
        check_branch(branch)
        with self._session():
            proc = self._git(
                "rev-parse", "--verify", "-q", f"refs/remotes/origin/{branch}", check=False
            )
            return proc.stdout.strip() if proc.returncode == 0 else None

    def log(self, path: str, ref: str, limit: int = 50) -> list[CommitInfo]:
        with self._session():
            rev = self._rev(ref)
            if rev is None:
                return []
            proc = self._git(
                "log",
                f"-n{max(1, limit)}",
                "--format=%H%x1f%an%x1f%ae%x1f%aI%x1f%B%x1e",
                rev,
                "--",
                path,
            )
        out: list[CommitInfo] = []
        for record in proc.stdout.split("\x1e"):
            record = record.strip("\n")
            if not record:
                continue
            sha, name, email, date, body = record.split("\x1f", 4)
            out.append(
                CommitInfo(
                    sha=sha,
                    author_name=name or None,
                    author_email=email or None,
                    timestamp=datetime.datetime.fromisoformat(date),
                    message=body.strip(),
                )
            )
        return out

    # ----------------------------------------------------------------- writes
    def _push(self, sha: str, branch: str, expect: str | None) -> None:
        lease = f"--force-with-lease=refs/heads/{branch}:{expect or ''}"
        proc = self._git("push", "-q", lease, "origin", f"{sha}:refs/heads/{branch}", check=False)
        if proc.returncode != 0:
            self._fetch(force=True)
            current = self._git(
                "rev-parse", "--verify", "-q", f"refs/remotes/origin/{branch}", check=False
            ).stdout.strip()
            if (current or None) != expect:
                raise ConflictError(None, f"branch {branch!r} moved; reload and retry")
            raise git_failure("git push failed", self._redact(proc.stderr))
        self._git("update-ref", f"refs/remotes/origin/{branch}", sha)

    def create_branch(self, name: str, from_sha: str) -> None:
        check_branch(name)
        with self._lock:
            self._ensure_repo()
            self._push(from_sha, name, None)

    def reset_branch(self, name: str, to_sha: str) -> None:
        check_branch(name)
        with self._lock:
            self._ensure_repo()
            self._git("push", "-q", "--force", "origin", f"{to_sha}:refs/heads/{name}")
            self._git("update-ref", f"refs/remotes/origin/{name}", to_sha)

    def commit_files(
        self,
        branch: str,
        base_sha: str | None,
        changes: dict[str, str | None],
        message: str,
        author: str | None,
    ) -> str:
        check_branch(branch)
        name, email = parse_author(author, self.committer)
        with self._session(), tempfile.TemporaryDirectory(dir=self.cache_dir) as tmp:
            current = self._git(
                "rev-parse", "--verify", "-q", f"refs/remotes/origin/{branch}", check=False
            ).stdout.strip()
            if (current or None) != base_sha:
                raise ConflictError(None, f"branch {branch!r} moved; reload and retry")
            index = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
            if base_sha:
                self._git("read-tree", base_sha, env=index)
            else:
                self._git("read-tree", "--empty", env=index)
            entries: list[str] = []
            for path, content in sorted(changes.items()):
                if content is None:
                    entries.append(f"0 {'0' * len(base_sha or 'x' * 40)}\t{path}")
                    continue
                blob = self._git("hash-object", "-w", "--stdin", stdin=content).stdout.strip()
                entries.append(f"100644 {blob}\t{path}")
            # --index-info works without a work tree (unlike --remove / --cacheinfo).
            self._git("update-index", "--index-info", stdin="\n".join(entries) + "\n", env=index)
            tree = self._git("write-tree", env=index).stdout.strip()
            if base_sha and tree == self._git("rev-parse", f"{base_sha}^{{tree}}").stdout.strip():
                return base_sha  # nothing changed
            parents = ["-p", base_sha] if base_sha else []
            commit = self._git(
                "commit-tree",
                tree,
                *parents,
                stdin=message,
                env={"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email},
            ).stdout.strip()
            self._push(commit, branch, base_sha)
            log.info("pushed %s to %s/%s", commit[:12], self.location, branch)
            return commit

    def open_pr(
        self,
        head: str,
        base: str,
        title: str,
        body: str,
        options: PullRequestOptions | None = None,
    ) -> PullRequest | None:
        return None  # plain git remotes have no pull requests

    def find_open_pr(self, head: str) -> PullRequest | None:
        return None
