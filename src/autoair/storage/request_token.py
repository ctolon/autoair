"""Git tokens the user types for one write (``storage.git.request_token``).

The deploy dialog may send a token in the ``X-AutoAir-Git-Token`` header of a deploy,
delete, promotion or proposal deploy. AutoAir never stores git secrets, so the token:

* exists only in memory for that one request: it is wrapped in :class:`RequestToken` (its
  ``repr`` is masked, it cannot be pickled), handed explicitly to
  :func:`build_request_target`, whose throw-away :class:`~autoair.storage.git.GitTarget`
  authenticates with it and is dropped with the request (no cache, no re-read);
* is never logged, audited, put in errors, commit or pull request texts:
  :mod:`autoair.secret_mask` masks it (and its Basic/URL encodings) in log records, audit
  records and error details while the request runs, on top of the providers' own redaction
  (git stderr, remote URLs);
* is only accepted over HTTPS or from a loopback client (``request_token_allow_insecure``
  for test setups), and only sent to ``https://`` git hosts.

Which credentials a git write uses (first match):

1. ``storage.git.request_token`` is ``off`` on the global storage (kill switch), or the
   target has ``forced_conn_id`` without ``request_token_overrides_forced``: a request
   with the header is refused (400); without it, the configured credentials of 3. apply;
2. a token in the header: that token, nothing else (a refused token is a 401/403, never
   retried with other credentials);
3. ``required`` without a token: refused (400); ``optional``: ``forced_conn_id``, then the
   user's own connection (``credentials_mode=per_user``), then the shared connection/token.

A DAG of a team with its own target (``team_storage.<team>``) uses that target's
``git.request_token*`` settings (1.'s kill switch still applies).
"""

from __future__ import annotations

import ipaddress
import logging
import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from autoair.storage.base import DeployTarget, StorageError
from autoair.storage.config import GitStorageSettings, build_request_token_target

log = logging.getLogger(__name__)

HEADER = "X-AutoAir-Git-Token"
MAX_LENGTH = 4096
_MIN_LENGTH = 8  # shorter values are no real token (and would mask ordinary text)
# Visible ASCII only: tokens are opaque printable strings (no spaces, no control chars).
_TOKEN_RE = re.compile(r"^[\x21-\x7e]+$")
_SSH_REMOTE = re.compile(r"^(?:ssh://|[^/@\s:]+@[^:\s/]+:)")

Mode = Literal["off", "optional", "required"]
OFF: Mode = "off"
REQUIRED: Mode = "required"


class RequestToken:
    """A git token typed for one request. Its ``repr``/``str`` never show it."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "RequestToken('***')"

    __str__ = __repr__

    def __reduce__(self) -> Any:
        raise TypeError("a request token cannot be pickled or copied to storage")


class RequestTokenError(Exception):
    """The header cannot be used (HTTP ``status``; ``code`` for the UI)."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message

    def detail(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


class RequestTokenHint(BaseModel):
    """What kind of token the user should create on the git host."""

    provider: str
    token_type: str = Field(description="Token kind to create on the host.")
    scopes: list[str] = Field(default_factory=list, description="Scopes/permissions needed.")
    docs_url: str | None = None


class RequestTokenInfo(BaseModel):
    """Whether (and how) deploys of a git target take a token typed by the user."""

    mode: Mode = Field(
        description="off: not accepted; optional: replaces the configured credentials "
        "when given; required: every git write needs one."
    )
    header: str = HEADER
    reason: str | None = Field(
        default=None, description="Why tokens are off (global switch, forced connection)."
    )
    hint: RequestTokenHint | None = None


def _hint(provider: str, kind: str, scopes: list[str], docs: str | None = None) -> RequestTokenHint:
    return RequestTokenHint(provider=provider, token_type=kind, scopes=scopes, docs_url=docs)


HINTS: dict[str, RequestTokenHint] = {
    "github": _hint(
        "github",
        "Fine-grained personal access token (or a classic token with 'repo')",
        ["contents:write", "pull_requests:write"],
        "https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/"
        "managing-your-personal-access-tokens",
    ),
    # write_repository alone only allows git push; AutoAir commits over the REST API.
    "gitlab": _hint(
        "gitlab",
        "Personal, project or group access token",
        ["api"],
        "https://docs.gitlab.com/user/profile/personal_access_tokens/",
    ),
    "bitbucket": _hint(
        "bitbucket",
        "Repository, project or workspace access token",
        ["repository:write", "pullrequest:write"],
        "https://support.atlassian.com/bitbucket-cloud/docs/access-tokens/",
    ),
    "azure": _hint(
        "azure",
        "Personal access token",
        ["Code (Read & write)"],
        "https://learn.microsoft.com/azure/devops/organizations/accounts/"
        "use-personal-access-tokens-to-authenticate",
    ),
    "gitea": _hint(
        "gitea",
        "Access token",
        ["write:repository"],  # + read:user for audit attribution (optional)
        "https://docs.gitea.com/development/api-usage",
    ),
    "local": _hint(
        "local",
        "Token of the git host, sent as HTTPS Basic credentials of the push",
        [],  # host-specific: write access to the repository
    ),
}


def root_settings(services: Any) -> GitStorageSettings:
    """The process' configured git settings (the global kill switch lives there)."""
    configured = getattr(services, "configured_storage", None)
    storage = configured or services.settings.storage
    git: GitStorageSettings = storage.git
    return git


def effective_mode(cfg: GitStorageSettings, root: GitStorageSettings | None = None) -> Mode:
    """``cfg``'s mode after the global kill switch and the forced-connection rule."""
    return _mode_and_reason(cfg, root)[0]


def _mode_and_reason(
    cfg: GitStorageSettings, root: GitStorageSettings | None
) -> tuple[Mode, str | None]:
    if root is not None and root.request_token == OFF:
        return "off", "storage.git.request_token is off"
    if cfg.forced_conn_id and not cfg.request_token_overrides_forced:
        return "off", (
            f"the administrator forces the git connection {cfg.forced_conn_id!r} "
            "(storage.git.request_token_overrides_forced is false)"
        )
    if cfg.request_token == OFF:
        return "off", "storage.git.request_token is off for this deploy target"
    return cfg.request_token, None


def info(
    cfg: GitStorageSettings, root: GitStorageSettings | None, kind: str
) -> RequestTokenInfo | None:
    """What the UI shows for a target of ``kind`` (``None``: not a git target)."""
    if kind != "git":
        return None
    mode, reason = _mode_and_reason(cfg, root)
    return RequestTokenInfo(
        mode=mode, reason=reason, hint=HINTS.get(cfg.provider) if mode != OFF else None
    )


def secure_transport(scheme: str, client_host: str | None) -> bool:
    """HTTPS (as seen through trusted proxy headers) or a loopback client."""
    if scheme == "https":
        return True
    if not client_host:
        return False
    if client_host == "localhost":
        return True
    try:
        return ipaddress.ip_address(client_host).is_loopback
    except ValueError:
        return False


def accept(
    raw: str | None,
    *,
    kind: str,
    cfg: GitStorageSettings,
    root: GitStorageSettings | None,
    scheme: str,
    client_host: str | None,
) -> RequestToken | None:
    """The header's token for a write to a target of ``kind``, ``None`` without one.

    Raises :class:`RequestTokenError` (never echoing the value) when it cannot be used."""
    if raw is None:
        return None
    if kind != "git":
        raise RequestTokenError(
            400,
            "git_token_not_git",
            f"{HEADER} is only accepted for git deploy targets (this one is {kind!r})",
        )
    mode, reason = _mode_and_reason(cfg, root)
    if mode == OFF:
        raise RequestTokenError(400, "git_token_off", f"{HEADER} is not accepted here: {reason}")
    if not cfg.request_token_allow_insecure and not secure_transport(scheme, client_host):
        raise RequestTokenError(
            403,
            "git_token_insecure",
            f"{HEADER} is only accepted over HTTPS (or from this machine); revoke the token "
            "if it crossed an untrusted network (storage.git.request_token_allow_insecure)",
        )
    value = raw.strip()
    if not value:
        raise RequestTokenError(400, "git_token_empty", f"{HEADER} is empty")
    if len(value) < _MIN_LENGTH or len(value) > MAX_LENGTH or not _TOKEN_RE.fullmatch(value):
        raise RequestTokenError(
            400,
            "git_token_malformed",
            f"{HEADER} does not look like a git token ({_MIN_LENGTH}-{MAX_LENGTH} visible "
            "ASCII characters, no spaces)",
        )
    return RequestToken(value)


def required_error() -> RequestTokenError:
    return RequestTokenError(
        400,
        "git_token_required",
        f"this deploy target needs your own git token for every write ({HEADER}; "
        "storage.git.request_token=required)",
    )


def check_required(
    token: RequestToken | None,
    *,
    kind: str,
    cfg: GitStorageSettings,
    root: GitStorageSettings | None,
) -> None:
    """Refuse a git write without a token when the target requires one."""
    if token is None and kind == "git" and effective_mode(cfg, root) == REQUIRED:
        raise required_error()


def git_token_required(services: Any) -> bool:
    """The default deploy target is git and every write needs a request token (MCP tools,
    which never take one, cannot write)."""
    try:
        storage = services.settings.storage
        return bool(
            storage.mode == "git"
            and effective_mode(storage.git, root_settings(services)) == REQUIRED
        )
    except AttributeError:  # bare services (tests, stdio without settings)
        return False


def _insecure_url(url: Any) -> bool:
    return isinstance(url, str) and url.strip().lower().startswith("http://")


def build_request_target(
    cfg: GitStorageSettings, token: RequestToken, *, transport: Any = None
) -> DeployTarget:
    """A git target of ``cfg`` that authenticates only with ``token`` (this write only).

    Refuses SSH remotes (the token cannot authenticate them, and the configured key must
    not stand in for it) and plain-HTTP git hosts (unless request_token_allow_insecure)."""
    try:
        target = build_request_token_target(cfg, token.reveal(), transport=transport)
    except StorageError as exc:
        raise RequestTokenError(400, "git_token_target", str(exc)) from None
    provider = getattr(target, "provider", None)
    remote = getattr(provider, "remote_url", None)
    if isinstance(remote, str) and _SSH_REMOTE.match(remote.strip()):
        raise RequestTokenError(
            400,
            "git_token_ssh_remote",
            "a git token authenticates HTTPS remotes only; this target pushes over SSH "
            "(use an https:// repository URL)",
        )
    urls = [remote, getattr(provider, "api_url", None), getattr(provider, "base", None)]
    if not cfg.request_token_allow_insecure and any(_insecure_url(u) for u in urls):
        raise RequestTokenError(
            400,
            "git_token_insecure_host",
            "the git host is reached over plain HTTP: a token is only sent over HTTPS "
            "(storage.git.request_token_allow_insecure)",
        )
    return target


def token_user(target: DeployTarget) -> str | None:
    """The git host's name for the token's owner (audit attribution), best effort."""
    lookup = getattr(getattr(target, "provider", None), "token_user", None)
    if lookup is None:
        return None
    try:
        name = lookup()
    except Exception as exc:  # scopes without user access, hosts without such an API
        log.debug("cannot tell whose git token this is: %s", type(exc).__name__)
        return None
    return str(name)[:200] if name else None


__all__ = [
    "HEADER",
    "HINTS",
    "Mode",
    "RequestToken",
    "RequestTokenError",
    "RequestTokenHint",
    "RequestTokenInfo",
    "accept",
    "build_request_target",
    "check_required",
    "effective_mode",
    "git_token_required",
    "info",
    "required_error",
    "root_settings",
    "secure_transport",
    "token_user",
]
