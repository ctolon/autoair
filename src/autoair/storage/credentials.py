"""Per-user git credentials (``storage.git.credentials_mode = per_user``).

Each user commits and opens pull requests with **their own Airflow connection**:

1. the connection id comes from ``storage.git.user_conn_id_template`` (``git_{username}``),
   or the user picks one matching ``storage.git.user_conn_id_pattern``;
2. AutoAir asks Airflow for that connection **with the user's token**
   (``GET /api/v2/connections/{id}``), so Airflow's RBAC decides whether the user may use it;
3. only then the secret is resolved server-side through Airflow's secrets backend /
   metadata database (Airflow's REST API never returns passwords), used for this one write
   and dropped — AutoAir stores no credentials.

Without a readable connection the deploy is refused, unless
``storage.git.user_credentials_fallback`` allows the shared credentials.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any, Literal

from autoair.storage.base import DeployTarget, StorageError
from autoair.storage.config import GitStorageSettings, build_git_target_with

_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")

CredentialSource = Literal["shared", "user", "shared_fallback", "forced", "request_token"]


class CredentialsError(Exception):
    """The user's git credentials cannot be used (HTTP ``status``)."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class ResolvedTarget:
    target: DeployTarget
    source: CredentialSource
    conn_id: str | None = None


def user_conn_id(cfg: GitStorageSettings, username: str) -> str:
    """The template's connection id for ``username`` (sanitised for Airflow ids)."""
    safe = _UNSAFE.sub("_", username.strip().lower()).strip("_") or "user"
    return cfg.user_conn_id_template.format(username=safe)


def allowed_choice(cfg: GitStorageSettings, conn_id: str) -> bool:
    """A connection id the user may pick (``user_conn_id_pattern``, fullmatch)."""
    return (
        bool(cfg.user_conn_id_pattern)
        and re.fullmatch(cfg.user_conn_id_pattern or "", conn_id) is not None
    )


def chosen_conn_id(cfg: GitStorageSettings, username: str | None, requested: str | None) -> str:
    if not username:
        raise CredentialsError(
            403, "per-user git credentials need a signed-in user (storage.git.credentials_mode)"
        )
    if requested:
        if requested == user_conn_id(cfg, username) or allowed_choice(cfg, requested):
            return requested
        raise CredentialsError(
            400,
            f"connection {requested!r} is not allowed for git deploys "
            "(storage.git.user_conn_id_pattern)",
        )
    return user_conn_id(cfg, username)


def _user_cache_dir(cfg: GitStorageSettings, username: str) -> Path | None:
    """provider=local: one clone per user (providers of different users never share one)."""
    if cfg.provider != "local":
        return cfg.cache_dir
    from autoair.storage.git.local_git import default_cache_dir

    base = cfg.cache_dir or default_cache_dir(cfg.repo or "")
    return base / "users" / hashlib.sha256(username.encode()).hexdigest()[:16]


ConnectionCheck = Callable[[str], Awaitable[Any]]
SecretLoader = Callable[[str], Any]


async def resolve_user_target(
    shared: DeployTarget,
    cfg: GitStorageSettings,
    *,
    username: str | None,
    requested_conn_id: str | None,
    check_access: ConnectionCheck,
    load_secret: SecretLoader | None = None,
    run_sync: Callable[..., Awaitable[Any]] | None = None,
) -> ResolvedTarget:
    """The target a write of ``username`` goes through.

    ``check_access(conn_id)`` asks Airflow with the user's token: it returns the connection
    (or ``None`` when missing) and raises on 401/403 (``AirflowError``-like with a
    ``status``). ``load_secret(conn_id)`` resolves the credentials server-side.
    """
    if cfg.forced_conn_id:
        return ResolvedTarget(shared, "forced", cfg.forced_conn_id)
    if cfg.credentials_mode != "per_user" or getattr(shared, "kind", None) != "git":
        return ResolvedTarget(shared, "shared")
    conn_id = chosen_conn_id(cfg, username, requested_conn_id)
    problem: str | None = None
    try:
        found = await check_access(conn_id)
    except Exception as exc:
        status = getattr(exc, "status", None)
        if status in (401, 403):
            problem = f"Airflow does not let you read connection {conn_id!r}"
        else:
            raise CredentialsError(502, f"cannot check connection {conn_id!r}: {exc}") from exc
    else:
        if found is None:
            problem = f"you have no Airflow connection {conn_id!r} with your git credentials"
    if problem is None:
        if load_secret is None:
            from autoair.storage.git.config import credentials_from_connection

            load_secret = credentials_from_connection
        creds = await run_sync(load_secret, conn_id) if run_sync else load_secret(conn_id)
        if creds is None or not (
            getattr(creds, "token", None)
            or getattr(creds, "private_key", None)
            or getattr(creds, "key_file", None)
        ):
            problem = (
                f"connection {conn_id!r} holds no usable git credentials (password/token or "
                "SSH key) for AutoAir's server"
            )
        else:
            user_cfg = cfg.model_copy(
                update={"cache_dir": _user_cache_dir(cfg, username or conn_id)}
            )
            try:
                # Read per deploy; re-read once more if the host refuses the credentials.
                target = build_git_target_with(
                    user_cfg, creds, reload=partial(load_secret, conn_id)
                )
            except StorageError as exc:
                raise CredentialsError(400, f"connection {conn_id!r}: {exc}") from exc
            return ResolvedTarget(target, "user", conn_id)
    if cfg.user_credentials_fallback:
        return ResolvedTarget(shared, "shared_fallback", conn_id)
    raise CredentialsError(
        403,
        f"{problem}; ask an Airflow admin to create it (storage.git.credentials_mode=per_user)",
    )


__all__ = [
    "CredentialsError",
    "ResolvedTarget",
    "allowed_choice",
    "chosen_conn_id",
    "resolve_user_target",
    "user_conn_id",
]
