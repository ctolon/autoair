"""Discover git settings from Airflow's own ``GitDagBundle`` configuration.

Everything here is best effort: Airflow may be missing, misconfigured or unreachable,
in which case ``None`` is returned and the caller falls back to explicit settings.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import BaseModel

log = logging.getLogger(__name__)


class BundleGitSettings(BaseModel):
    name: str
    repo_url: str | None = None
    tracking_ref: str | None = None
    subdir: str | None = None
    git_conn_id: str | None = None


class GitCredentials(BaseModel):
    """Credentials pulled from an Airflow ``git`` connection (never logged)."""

    repo_url: str | None = None
    username: str | None = None
    token: str | None = None
    key_file: str | None = None
    private_key: str | None = None
    github_app_id: str | None = None
    github_installation_id: str | None = None

    def __repr__(self) -> str:  # keep secrets out of logs / tracebacks
        return f"GitCredentials(repo_url={self.repo_url!r}, username={self.username!r})"

    __str__ = __repr__


def _bundle_list() -> list[dict[str, Any]]:
    from airflow.configuration import conf

    raw = conf.get("dag_processor", "dag_bundle_config_list", fallback="[]") or "[]"
    data = json.loads(raw) if isinstance(raw, str) else raw
    return [b for b in data if isinstance(b, dict)] if isinstance(data, list) else []


def bundle_git_settings(name: str | None = None) -> BundleGitSettings | None:
    """The (first, or ``name``d) ``GitDagBundle`` entry of ``dag_bundle_config_list``."""
    try:
        bundles = _bundle_list()
    except Exception:
        log.debug("could not read Airflow's dag_bundle_config_list", exc_info=True)
        return None
    for bundle in bundles:
        if not str(bundle.get("classpath", "")).endswith("GitDagBundle"):
            continue
        if name is not None and bundle.get("name") != name:
            continue
        kwargs = bundle.get("kwargs") or {}
        return BundleGitSettings(
            name=str(bundle.get("name", "")),
            repo_url=kwargs.get("repo_url"),
            tracking_ref=kwargs.get("tracking_ref"),
            subdir=kwargs.get("subdir"),
            git_conn_id=kwargs.get("git_conn_id"),
        )
    return None


def credentials_from_connection(conn_id: str) -> GitCredentials | None:
    """Read login / token / SSH key / GitHub App ids from an Airflow connection."""
    from autoair._compat import AirflowConnectionError, get_airflow_connection

    try:
        conn: Any = get_airflow_connection(conn_id)
    except AirflowConnectionError as exc:
        log.warning("could not read Airflow connection: %s", exc)
        return None
    try:
        extra: dict[str, Any] = dict(conn.extra_dejson or {})
    except Exception:
        extra = {}

    def _opt(value: Any) -> str | None:
        return str(value) if value not in (None, "") else None

    return GitCredentials(
        repo_url=_opt(getattr(conn, "host", None)),
        username=_opt(getattr(conn, "login", None)),
        token=_opt(getattr(conn, "password", None)),
        key_file=_opt(extra.get("key_file")),
        private_key=_opt(extra.get("private_key")),
        github_app_id=_opt(extra.get("github_app_id")),
        github_installation_id=_opt(extra.get("github_installation_id")),
    )
