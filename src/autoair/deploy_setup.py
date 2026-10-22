"""Deploy-target setup from the UI: where Airflow really reads DAGs from, and pushing there.

In production Airflow rarely reads a local folder: DAGs come from a git repository, through a
``GitDagBundle`` (Airflow 3) or a git-sync sidecar that keeps the DAG folder checked out.
AutoAir must then *push to that repository* (or open pull requests) instead of writing files.

* :func:`discover_candidates` lists the options this Airflow offers: the DAG folder, every
  ``GitDagBundle``, every object-storage bundle (``S3DagBundle``, ``GCSDagBundle``), an
  object-storage prefix entered by hand and — when the DAG folder is a git checkout
  (git-sync) — its remote.
* :class:`DeployTargetStore` persists the target chosen in the UI (``deploy-target.json`` in the
  state folder, or the SQL database); it overrides the ``AUTOAIR_STORAGE__*`` settings and is
  applied without restart.
  Secrets are never stored: credentials come from an Airflow connection (``conn_id``).
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from autoair.config_reference import MASK, is_credential_key, mask_credentials, mask_url_credentials
from autoair.persistence.documents import DocumentStore, FileDocumentStore
from autoair.storage.config import GitStorageSettings, StorageSettings

log = logging.getLogger(__name__)

CandidateKind = Literal["local", "airflow-bundle", "git-checkout", "object-bundle", "object"]
Provider = Literal["local", "github", "gitlab", "bitbucket", "azure", "gitea"]


class DeployCandidate(BaseModel):
    id: str
    kind: CandidateKind
    title: str
    description: str = ""
    repo: str | None = None
    branch: str | None = None
    subdir: str | None = None
    conn_id: str | None = None
    provider: Provider = "local"
    object_url: str | None = Field(
        default=None, description="Object storage candidates: fsspec URL of the DAG prefix."
    )
    settings: StorageSettings = Field(description="Proposed deploy target settings.")


# Object-storage DAG bundles of the provider packages: class name -> (URL scheme, the kwarg
# naming the Airflow connection the bundle reads with).
OBJECT_BUNDLES: dict[str, tuple[str, str]] = {
    "S3DagBundle": ("s3", "aws_conn_id"),
    "GCSDagBundle": ("gs", "gcp_conn_id"),
}


def suggested_provider(url: str | None) -> Provider:
    """API provider for a remote URL (PR mode needs it); plain git otherwise."""
    host = (url or "").lower()
    if "github" in host:
        return "github"
    if "gitlab" in host:
        return "gitlab"
    if "bitbucket.org" in host:
        return "bitbucket"
    if "dev.azure.com" in host or "visualstudio.com" in host:
        return "azure"
    if "gitea" in host or "forgejo" in host or "codeberg.org" in host:
        return "gitea"
    return "local"


def _git(args: list[str], cwd: Path) -> str | None:
    try:
        proc = subprocess.run(
            ["git", *args],  # noqa: S607
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = proc.stdout.strip()
    return out if proc.returncode == 0 and out else None


def _strip_credentials(url: str) -> str:
    """``https://user:token@host/repo`` → ``https://host/repo`` (never show secrets)."""
    return re.sub(r"(?<=://)[^/@]+@", "", url)


def git_checkout(path: Path) -> tuple[str, str | None, str] | None:
    """``(remote url, branch or None if detached, subdir)`` when ``path`` is in a git checkout.

    git-sync keeps the repository at ``<root>/<link>`` (a symlink to a worktree), so the DAG
    folder resolves into a normal checkout.
    """
    real = path.expanduser().resolve()
    if not real.is_dir():
        return None
    top = _git(["rev-parse", "--show-toplevel"], real)
    remote = _git(["remote", "get-url", "origin"], real) if top else None
    if not top or not remote:
        return None
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], real)
    if branch == "HEAD":  # detached (git-sync checks out a revision)
        branch = None
    rel = os.path.relpath(real, top)
    return _strip_credentials(remote), branch, "" if rel == "." else rel


def _bundles() -> list[dict[str, object]]:
    try:
        from autoair.storage.git.config import _bundle_list

        return _bundle_list()
    except Exception:
        log.debug("could not read Airflow's dag bundles", exc_info=True)
        return []


def _object_bundle(bundle: dict[str, object]) -> DeployCandidate | None:
    cls = str(bundle.get("classpath", "")).rsplit(".", 1)[-1]
    if cls not in OBJECT_BUNDLES:
        return None
    scheme, conn_kwarg = OBJECT_BUNDLES[cls]
    raw = bundle.get("kwargs")
    kwargs: dict[str, object] = raw if isinstance(raw, dict) else {}
    bucket = str(kwargs.get("bucket_name") or "").strip("/")
    if not bucket:
        return None
    prefix = str(kwargs.get("prefix") or "").strip("/")
    url = f"{scheme}://{bucket}/{prefix}" if prefix else f"{scheme}://{bucket}"
    name = str(bundle.get("name", scheme))
    conn = kwargs.get(conn_kwarg)
    return DeployCandidate(
        id=f"bundle:{name}",
        kind="object-bundle",
        title=f"Airflow DAG bundle “{name}”",
        description=f"Airflow reads DAGs from this object storage prefix ({cls}).",
        conn_id=str(conn) if conn else None,
        object_url=url,
        settings=StorageSettings(mode="object", object_url=url),
    )


def manual_object_candidate(configured: StorageSettings | None) -> DeployCandidate:
    """An object storage prefix entered by hand; prefilled with the configured one (secrets
    masked: ``***`` stands for the configured credential and is restored on save)."""
    if configured is not None and configured.mode == "object":
        safe = scrub_secrets(configured)
        settings = StorageSettings(
            mode="object", object_url=safe.object_url, storage_options=safe.storage_options
        )
    else:
        settings = StorageSettings(mode="object")
    return DeployCandidate(
        id="object",
        kind="object",
        title="Object storage",
        description="An S3, GCS or Azure Blob prefix (fsspec URL) Airflow reads DAGs from.",
        object_url=settings.object_url,
        settings=settings,
    )


def discover_candidates(
    dags_folder: Path, configured: StorageSettings | None = None
) -> list[DeployCandidate]:
    out = [
        DeployCandidate(
            id="local",
            kind="local",
            title="DAG folder",
            description=str(dags_folder),
            settings=StorageSettings(mode="local"),
        )
    ]
    bundles = _bundles()
    for bundle in bundles:
        if not str(bundle.get("classpath", "")).endswith("GitDagBundle"):
            continue
        kwargs = bundle.get("kwargs") if isinstance(bundle.get("kwargs"), dict) else {}
        assert isinstance(kwargs, dict)
        name = str(bundle.get("name", "git"))
        repo = kwargs.get("repo_url")
        conn = kwargs.get("git_conn_id")
        provider = suggested_provider(str(repo) if repo else None)
        out.append(
            DeployCandidate(
                id=f"bundle:{name}",
                kind="airflow-bundle",
                title=f"Airflow DAG bundle “{name}”",
                description="Airflow reads DAGs from this repository (GitDagBundle).",
                repo=str(repo) if repo else None,
                branch=str(kwargs.get("tracking_ref") or "") or None,
                subdir=str(kwargs.get("subdir") or "") or None,
                conn_id=str(conn) if conn else None,
                provider=provider,
                settings=StorageSettings(
                    mode="git",
                    git=GitStorageSettings(
                        provider=provider,
                        use_airflow_bundle=True,
                        bundle_name=name,
                        mode="pr",
                    ),
                ),
            )
        )
    checkout = git_checkout(dags_folder)
    if checkout:
        remote, branch, subdir = checkout
        provider = suggested_provider(remote)
        out.append(
            DeployCandidate(
                id="checkout",
                kind="git-checkout",
                title="Git repository of the DAG folder",
                description="The DAG folder is a git checkout (e.g. git-sync): deploys must be "
                "pushed to its remote.",
                repo=remote,
                branch=branch,
                subdir=subdir or None,
                provider=provider,
                settings=StorageSettings(
                    mode="git",
                    git=GitStorageSettings(
                        provider=provider,
                        repo=remote,
                        base_branch=branch or "main",
                        subdir=subdir or None,
                        mode="pr",
                    ),
                ),
            )
        )
    for bundle in bundles:
        found = _object_bundle(bundle)
        if found is not None:
            out.append(found)
    out.append(manual_object_candidate(configured))
    return out


class DeployTargetStore:
    """The deploy target chosen in the UI (overrides environment settings)."""

    KEY = "deploy-target"

    def __init__(self, path: Path, *, documents: DocumentStore | None = None) -> None:
        self.path = path
        # ``<state>/deploy-target.json`` by default; a row of the SQL database when one is
        # configured (every replica then sees the same choice, see :meth:`version`).
        self.key = path.stem if documents is None else self.KEY
        self.documents: DocumentStore = documents or FileDocumentStore(
            str(path.parent), suffix=path.suffix
        )

    def version(self) -> str | None:
        """Version of the saved choice (``None``: nothing saved); changes on every save."""
        try:
            found = self.documents.get(self.key)
        except Exception:
            return None
        return found.version if found is not None else None

    def load(self, configured: StorageSettings | None = None) -> StorageSettings | None:
        """The saved target; credential placeholders (``***``) of its ``storage_options``
        are filled from ``configured`` (the ``AUTOAIR_STORAGE__*`` settings) or dropped."""
        try:
            found = self.documents.get(self.key)
            if found is None:
                return None
            saved = StorageSettings.model_validate_json(found.data)
        except FileNotFoundError:
            return None
        except Exception:
            log.warning("ignoring unreadable %s", self.path, exc_info=True)
            return None
        return restore_secrets(saved, configured)

    def save(self, settings: StorageSettings) -> None:
        # Credentials are never written: storage_options keep a "***" placeholder (a
        # reference to the configured value, resolved by load()).
        safe = scrub_secrets(settings)
        # Files: unique temp file + rename, concurrent saves cannot interleave into a torn
        # file; SQL: one versioned row.
        self.documents.put(
            self.key,
            json.dumps(safe.model_dump(mode="json", exclude_none=True), indent=1).encode(),
        )

    def clear(self) -> bool:
        return self.documents.delete(self.key)


def scrub_secrets(settings: StorageSettings) -> StorageSettings:
    """Tokens/keys are never persisted or returned: use an Airflow connection instead.

    Git tokens are removed; credential-like ``storage_options`` values (``key``, ``secret``,
    ``token``, ``*_access_key*``, ``account_key``, ``sas_token``, ``connection_string``…, at
    any depth) are replaced by ``***`` and URLs lose their ``user:password@``.
    """
    git = settings.git.model_copy(
        update={
            "token": None,
            "github_private_key": None,
            "repo": mask_url_credentials(settings.git.repo) if settings.git.repo else None,
        }
    )
    return settings.model_copy(
        update={
            "git": git,
            "storage_options": mask_credentials(settings.storage_options),
            "object_url": (
                mask_url_credentials(settings.object_url) if settings.object_url else None
            ),
        }
    )


def _restore(value: Any, configured: Any) -> Any:
    if isinstance(value, dict):
        base = configured if isinstance(configured, dict) else {}
        out: dict[str, Any] = {}
        for key, item in value.items():
            if item == MASK:
                if key in base and base[key] != MASK:
                    out[key] = base[key]
                continue  # unresolvable reference: left out (fsspec's own defaults apply)
            out[key] = _restore(item, base.get(key))
        return out
    if isinstance(value, list):
        return [_restore(v, None) for v in value]
    return value


def restore_secrets(
    settings: StorageSettings, configured: StorageSettings | None
) -> StorageSettings:
    """Replace ``***`` placeholders of ``storage_options`` (a scrubbed target sent back by the
    UI or read from ``deploy-target.json``) with the configured values at the same path."""
    update: dict[str, Any] = {}
    if settings.storage_options:
        base = configured.storage_options if configured is not None else {}
        update["storage_options"] = _restore(settings.storage_options, base)
    # URLs whose user:password@ was masked: the configured URL they stand for.
    url, known = settings.object_url, configured.object_url if configured else None
    if url and known and url != known and mask_url_credentials(known) == url:
        update["object_url"] = known
    repo, known = settings.git.repo, configured.git.repo if configured else None
    if repo and known and repo != known and mask_url_credentials(known) == repo:
        update["git"] = settings.git.model_copy(update={"repo": known})
    return settings.model_copy(update=update) if update else settings


# Git settings only an administrator sets (configuration), never the deploy-target dialog.
GIT_POLICY_FIELDS = (
    "forced_conn_id",
    "credentials_mode",
    "user_conn_id_template",
    "user_conn_id_pattern",
    "user_credentials_fallback",
    "commit_author",
    "direct_push_roles",
    "request_token",
    "request_token_overrides_forced",
    "request_token_allow_insecure",
    "mcp_request_token",
)


class PolicyError(ValueError):
    """A deploy target chosen in the UI violates the configured policy."""


def apply_policy(chosen: StorageSettings, configured: StorageSettings) -> StorageSettings:
    """``chosen`` (from the UI or deploy-target.json) with the administrator's policy of
    ``configured`` enforced: the allowed target kinds, the forced git connection and the
    credential / commit-author rules. Raises :class:`PolicyError` for a disallowed kind."""
    allowed = list(configured.allowed_targets)
    if chosen.mode not in allowed:
        raise PolicyError(
            f"deploy target kind {chosen.mode!r} is not allowed here "
            f"(storage.allowed_targets: {', '.join(allowed)})"
        )
    git_update: dict[str, Any] = {f: getattr(configured.git, f) for f in GIT_POLICY_FIELDS}
    if configured.git.forced_conn_id:
        git_update["conn_id"] = configured.git.forced_conn_id
    return chosen.model_copy(
        update={"allowed_targets": allowed, "git": chosen.git.model_copy(update=git_update)}
    )


def candidate_mode(candidate: DeployCandidate) -> str:
    return candidate.settings.mode


def unreferenced_secrets(
    settings: StorageSettings, configured: StorageSettings | None
) -> list[str]:
    """Credential ``storage_options`` given in clear that differ from the configured ones
    (they would have to be persisted): their dotted paths."""
    found: list[str] = []

    def walk(value: Any, base: Any, path: str) -> None:
        if not isinstance(value, dict):
            return
        base_map = base if isinstance(base, dict) else {}
        for key, item in value.items():
            here = f"{path}.{key}" if path else str(key)
            if is_credential_key(key):
                if item not in (None, "", MASK) and item != base_map.get(key):
                    found.append(here)
            else:
                walk(item, base_map.get(key), here)

    walk(settings.storage_options, configured.storage_options if configured else {}, "")
    for name, url, known in (
        ("object_url", settings.object_url, configured.object_url if configured else None),
        ("git.repo", settings.git.repo, configured.git.repo if configured else None),
    ):
        if url and url != known and mask_url_credentials(url) != url and MASK not in url:
            found.append(name)
    return found


__all__ = [
    "GIT_POLICY_FIELDS",
    "DeployCandidate",
    "DeployTargetStore",
    "PolicyError",
    "apply_policy",
    "candidate_mode",
    "discover_candidates",
    "git_checkout",
    "manual_object_candidate",
    "restore_secrets",
    "scrub_secrets",
    "suggested_provider",
]
