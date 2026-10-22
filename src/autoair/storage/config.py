"""Storage configuration and the :func:`build_target` factory."""

from __future__ import annotations

import builtins
import hashlib
import logging
import re
import string
import time
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import (
    BaseModel,
    Field,
    SecretStr,
    ValidationInfo,
    field_validator,
    model_validator,
)

from autoair.governance import CONTEXT_PLACEHOLDERS
from autoair.spec import DagSpec
from autoair.storage.base import (
    DEFAULT_HISTORY_LIMIT,
    DeployTarget,
    SaveResult,
    SpecSummary,
    StorageError,
    StoredSpec,
    TargetStatus,
    VersionInfo,
)
from autoair.storage.object import DEFAULT_LOCK_SECONDS, DEFAULT_LOCK_WAIT_SECONDS

if TYPE_CHECKING:
    from autoair.persistence import DocumentStore
    from autoair.storage.git.config import GitCredentials
    from autoair.storage.git.provider import GitProvider

log = logging.getLogger(__name__)


class GitStorageSettings(BaseModel):
    provider: Literal["local", "github", "gitlab", "bitbucket", "azure", "gitea"] = Field(
        default="local",
        description="Git host API: local (git CLI, any remote), github, gitlab, bitbucket, "
        "azure, gitea (Gitea/Forgejo: git CLI push + pull requests over the REST API).",
    )
    repo: str | None = Field(
        default=None,
        description="owner/name (github), project id or path (gitlab), workspace/repo "
        "(bitbucket), organization/project/repository (azure), remote URL (local, gitea).",
    )
    username: str | None = Field(
        default=None, description="Bitbucket app-password user (omit for access tokens)."
    )
    azure_bearer: bool = Field(
        default=False, description="Azure DevOps: token is an Entra ID bearer token, not a PAT."
    )
    base_branch: str | None = Field(default=None, description="Defaults to the bundle ref/main.")
    subdir: str | None = Field(default=None, description="Folder inside the repository.")
    mode: Literal["pr", "push"] = Field(
        default="pr",
        description="pr: a branch + pull request per DAG; push: commit straight to the branch.",
    )
    branch_prefix: str = Field(
        default="autoair/", description="Prefix of pending-change branches (mode=pr)."
    )
    token: SecretStr | None = Field(
        default=None, description="Personal/project access token, app password or PAT."
    )
    api_url: str | None = Field(
        default=None,
        description="REST API base URL (GitHub Enterprise, self-hosted GitLab, Azure DevOps "
        "Server…); default: the public host's. gitea: default <scheme>://<host>/api/v1 of an "
        "http(s) repo URL (required for SSH remotes).",
    )
    environments: dict[str, str] = Field(
        default_factory=dict, description="Environment name -> branch, e.g. {'prod': 'main'}."
    )
    use_airflow_bundle: bool = Field(
        default=False,
        description="Fill repo/branch/subdir/credentials from Airflow's GitDagBundle config.",
    )
    bundle_name: str | None = Field(
        default=None, description="GitDagBundle to read (use_airflow_bundle; default: the first)."
    )
    conn_id: str | None = Field(default=None, description="Airflow git connection id.")
    credentials_ttl: float = Field(
        default=60,
        ge=0,
        description="Seconds the credentials read from an Airflow connection (conn_id, "
        "forced_conn_id, the bundle's or a user's own) are reused before the connection is "
        "read again; on an authentication failure it is re-read at once and the operation "
        "retried, so a rotated token works without a restart. 0 = read on every operation.",
    )
    cache_dir: Path | None = Field(default=None, description="Clone cache (provider=local).")
    ssh_key_file: Path | None = Field(default=None, description="provider=local: SSH key.")
    ssh_key_cache_days: int = Field(
        default=30,
        ge=0,
        description="provider=local/gitea: days an SSH private key from a git connection stays "
        "in the clone cache (<cache_dir>/keys/id_autoair-<hash>, mode 0600) after its last "
        "use. Writing a new key file removes AutoAir's key files unused for longer (only "
        "files of that name owned by this user, never the key being used). 0 keeps them.",
    )
    github_app_id: str | None = Field(default=None, description="GitHub App id.")
    github_installation_id: str | None = Field(
        default=None, description="GitHub App installation id."
    )
    github_private_key: SecretStr | None = Field(
        default=None, description="GitHub App private key (PEM)."
    )
    github_private_key_file: Path | None = Field(
        default=None, description="GitHub App private key file (PEM)."
    )
    fetch_interval: float = Field(
        default=5.0, ge=0, description="provider=local: seconds between fetches of the remote."
    )

    # --- direct push vs. pull request (per request; the IAM layer decides by role)
    direct_push_roles: list[str] | None = Field(
        default=None,
        description="AutoAir roles allowed to push straight to the target branch "
        "(deploy request flag direct_push=true in mode=pr; see iam.force_pr_without_direct_push "
        "for mode=push). Evaluated by autoair.iam; unset = iam.direct_push_roles applies "
        "(default ['Admin']); [] = nobody, except users granted 'deploy:direct_push' "
        "explicitly.",
    )

    # --- credentials policy
    forced_conn_id: str | None = Field(
        default=None,
        description="Admin policy: the Airflow connection every git deploy uses. Overrides "
        "conn_id (and the bundle's connection); the deploy-target dialog cannot change it.",
    )
    credentials_mode: Literal["shared", "per_user"] = Field(
        default="shared",
        description="shared: one service connection/token for everybody; per_user: commits "
        "and pull requests use the deploying user's own Airflow connection (see "
        "user_conn_id_template), read with the user's token so Airflow's RBAC applies. "
        "Reads (listing, history) keep the shared credentials when configured.",
    )
    user_conn_id_template: str = Field(
        default="git_{username}",
        description="per_user: the user's connection id. Placeholders: {username} (the "
        "Airflow username, lower-cased; characters outside [A-Za-z0-9_.-] become '_').",
    )
    user_conn_id_pattern: str | None = Field(
        default=None,
        description="per_user: regex (fullmatch) of connection ids a user may pick in the "
        "deploy dialog instead of the template's (e.g. 'git_.*'); unset: only the template.",
    )
    user_credentials_fallback: bool = Field(
        default=False,
        description="per_user: use the shared credentials when the user has no readable "
        "connection (otherwise such deploys are refused).",
    )
    request_token: Literal["off", "optional", "required"] = Field(
        default="optional",
        description="Git token the user types for one deploy, delete, promotion or pull "
        "request (header X-AutoAir-Git-Token, never stored, logged or audited): off = the "
        "header is refused; optional = a token, when given, replaces the configured "
        "credentials for that one write; required = every git write needs one (the "
        "configured connection, if any, only serves reads; MCP git writes need "
        "mcp_request_token and the header). "
        "A team target uses its own team_storage.<team>.git.request_token, but off here "
        "turns request tokens off for every target. With forced_conn_id set, tokens are "
        "refused unless request_token_overrides_forced.",
        json_schema_extra={"secret": False},
    )
    request_token_overrides_forced: bool = Field(
        default=False,
        description="A request token may replace the admin-forced connection "
        "(forced_conn_id) for that one write. Default: the forced connection always wins and "
        "the X-AutoAir-Git-Token header is refused.",
    )
    mcp_request_token: bool = Field(
        default=False,
        description="MCP over HTTP: save_dag_spec / delete_dag_spec take the user's git token "
        "from the X-AutoAir-Git-Token header of the MCP request (set in the MCP client's "
        "server configuration; never a tool argument, so it stays out of the model's "
        "context and transcripts). request_token, the forced-connection rule and the HTTPS "
        "rule apply as for the REST API. Off: such a header is refused.",
        json_schema_extra={"secret": False},
    )
    request_token_allow_insecure: bool = Field(
        default=False,
        description="Accept X-AutoAir-Git-Token over plain HTTP from a non-loopback client "
        "(the scheme and client address honour server.proxy_headers / "
        "forwarded_allow_ips), and send it to http:// git hosts. Only for test setups.",
    )
    commit_author: Literal["auto", "service", "user"] = Field(
        default="auto",
        description="Commit author: service = the service identity (the user is credited in "
        "trailers and Co-authored-by); user = the deploying user ('Name <email>'); auto = user "
        "with per_user credentials, else service. GitHub's API always commits as the token's "
        "owner (the user is credited with Co-authored-by).",
    )

    # --- branches, commits and pull requests
    branch_template: str = Field(
        default="{branch_prefix}{environment_path}{dag_id}",
        description="mode=pr: branch of a DAG's pending change. Placeholders: {branch_prefix}, "
        "{dag_id}, {environment} (may be empty), {environment_path} ('<env>/' or '').",
    )
    promote_branch_template: str = Field(
        default="{branch_prefix}promote/{from_env}-to-{to_env}/{dag_id}",
        description="Branch of an environment promotion. Placeholders: {branch_prefix}, "
        "{dag_id}, {from_env}, {to_env}.",
    )
    commit_message_template: str = Field(
        default="AutoAir: update DAG {dag_id}",
        description="Commit message of a save without a user message. Placeholders: {dag_id}, "
        "{environment}, {author} and the context placeholders ({user}, {user_name}, "
        "{user_email}, {autoair_version}, {etag_before}, {etag_after}, {validation}, "
        "{approvals}, {link}, {target}, {description}, {context}). The context block and "
        "trailers are always appended unless the template places {context} itself.",
    )
    delete_message_template: str = Field(
        default="AutoAir: delete DAG {dag_id}",
        description="Commit message (and PR title) of a delete. Placeholders: {dag_id}, "
        "{environment}, {author} and the context placeholders (see commit_message_template).",
    )
    promote_message_template: str = Field(
        default="AutoAir: promote DAG {dag_id} from {from_env} to {to_env}",
        description="Commit message (and PR title) of a promotion. Placeholders: {dag_id}, "
        "{from_env}, {to_env}, {author} and the context placeholders.",
    )
    pr_title_template: str = Field(
        default="AutoAir: update DAG {dag_id}",
        description="Pull request title of a save. Placeholders: {dag_id}, {environment}, "
        "{author}, {message} (first line of the commit message) and the context placeholders.",
    )
    pr_body_template: str = Field(
        default="{message}\n\n{description}\n\nOpened by AutoAir.",
        description="Pull request description. Placeholders: {message} (the commit message "
        "without the context), {description} (the PR description typed in the deploy "
        "dialog), {dag_id}, {environment}, {author} and the context placeholders. The context "
        "block is appended unless the template places {context}; it cannot be removed.",
    )
    pr_labels: list[str] = Field(
        default_factory=list,
        description="Labels put on opened pull requests (GitHub, GitLab, Azure DevOps, Gitea).",
    )
    pr_reviewers: list[str] = Field(
        default_factory=list,
        description="Reviewers requested on opened pull requests: GitHub logins (or "
        "'org/team' for teams), GitLab usernames, Bitbucket account UUIDs, Azure DevOps "
        "identity ids, Gitea usernames.",
    )
    pr_draft: bool = Field(default=False, description="Open pull requests as drafts.")
    delete_branch_after_merge: bool = Field(
        default=True,
        description="Ask the host to delete the source branch when the pull request is merged "
        "(GitLab, Bitbucket, Azure DevOps; on GitHub this is a repository setting).",
    )

    # --- committer and transport
    committer_name: str = Field(
        default="AutoAir", description="provider=local: committer name of AutoAir commits."
    )
    committer_email: str = Field(
        default="autoair@localhost",
        description="provider=local: committer e-mail (also the author when none is known).",
    )
    timeout: float = Field(
        default=30, gt=0, description="Seconds per request to the git host's REST API."
    )
    command_timeout: float = Field(
        default=300, gt=0, description="provider=local: seconds a git command may take."
    )
    env_passthrough: list[str] = Field(
        default_factory=list,
        description="provider=local/gitea: environment variables (fnmatch patterns, "
        "case-insensitive) the git CLI inherits besides the ones git, ssh and curl read "
        "(PATH, HOME, locale, TMPDIR, SSH_AUTH_SOCK, SSL_CERT_*, proxy variables, GIT_*), "
        "e.g. ['KRB5CCNAME'] for Kerberos. Nothing else of the server's environment reaches "
        "git.",
    )
    max_attempts: int = Field(
        default=3,
        ge=1,
        le=20,
        description="Saves retried when the branch moves between read and commit.",
    )
    read_cache_size: int = Field(
        default=512, ge=0, description="File reads at immutable commits kept in memory."
    )

    @field_validator("user_conn_id_pattern")
    @classmethod
    def _valid_pattern(cls, value: str | None) -> str | None:
        if value:
            try:
                re.compile(value)
            except re.error as exc:
                raise ValueError(f"invalid regular expression {value!r}: {exc}") from exc
        return value

    @field_validator("user_conn_id_template")
    @classmethod
    def _valid_conn_template(cls, value: str) -> str:
        check_template(value, frozenset({"username"}))
        return value

    @field_validator(
        "branch_template",
        "promote_branch_template",
        "commit_message_template",
        "delete_message_template",
        "promote_message_template",
        "pr_title_template",
        "pr_body_template",
    )
    @classmethod
    def _valid_template(cls, value: str, info: ValidationInfo) -> str:
        check_template(value, TEMPLATE_FIELDS[info.field_name or ""])
        return value


TargetKindName = Literal["local", "object", "git"]
ALL_TARGET_KINDS: tuple[TargetKindName, ...] = ("local", "git", "object")


class StorageSettings(BaseModel):
    mode: TargetKindName = Field(
        default="local",
        description="Deploy target: a local folder, object storage (fsspec) or a git repository.",
    )
    allowed_targets: list[TargetKindName] = Field(
        default_factory=lambda: list(ALL_TARGET_KINDS),
        min_length=1,
        description="Target kinds the deploy-target dialog/API offers and accepts, e.g. "
        "['git'] (only repositories) or ['local'] (only the DAG folder). Admin policy: "
        "the UI cannot change it.",
    )
    local_path: Path | None = Field(
        default=None, description="mode=local: folder written to (default: dags_folder/subfolder)."
    )
    object_url: str | None = Field(
        default=None,
        examples=["s3://bucket/dags/autoair"],
        description="mode=object: fsspec URL of the bundle prefix.",
    )
    storage_options: dict[str, Any] = Field(
        default_factory=dict,
        description="mode=object: fsspec storage options (credentials, endpoint…).",
        json_schema_extra={"secret": True},
    )
    history_limit: int = Field(
        default=DEFAULT_HISTORY_LIMIT, ge=1, description="Saved versions kept per DAG."
    )
    object_lock_seconds: float = Field(
        default=DEFAULT_LOCK_SECONDS,
        gt=0,
        description="mode=object: seconds a write lease of a DAG lasts. Writers of one DAG "
        "hold it (a document in AutoAir's state backend, shared by every replica with "
        "database.backend sqlite/postgresql) while they check base_etag and write, so two "
        "replicas never both win; a replica that died holding it blocks that DAG's deploys "
        "at most this long. Must exceed the longest save.",
    )
    object_lock_wait_seconds: float = Field(
        default=DEFAULT_LOCK_WAIT_SECONDS,
        ge=0,
        description="mode=object: seconds a save waits for another writer's lease on the "
        "same DAG before it fails with a conflict (409).",
    )
    require_base_etag: bool = Field(
        default=True,
        description="Overwriting or deleting an existing DAG spec needs the etag it was "
        "loaded with (REST: base_etag or If-Match on PUT, If-Match on DELETE, else HTTP 428 "
        "Precondition Required with the current etag; MCP save_dag_spec and delete_dag_spec: "
        "base_etag, else the call is refused with current_etag). false lets legacy API "
        "clients overwrite or delete without one (last writer wins). Creating a new DAG "
        "never needs an etag.",
    )
    file_mode: str = Field(
        default="0644",
        pattern=r"^0?[0-7]{3,4}$",
        description="mode=local: permissions (octal) of written files.",
    )
    dir_mode: str | None = Field(
        default=None,
        pattern=r"^0?[0-7]{3,4}$",
        description="mode=local: permissions (octal) of created folders (default: umask).",
    )
    temp_dir: Path | None = Field(
        default=None,
        description="mode=local: folder for the temporary file of atomic writes (default: the "
        "target folder). Must be on the same file system, or writes stop being atomic.",
    )
    git: GitStorageSettings = Field(default_factory=GitStorageSettings)

    @model_validator(mode="after")
    def _allowed_mode(self) -> StorageSettings:
        if self.mode not in self.allowed_targets:
            raise ValueError(
                f"storage.mode {self.mode!r} is not one of storage.allowed_targets "
                f"{self.allowed_targets}"
            )
        return self

    def file_mode_int(self) -> int:
        return int(self.file_mode, 8)

    def dir_mode_int(self) -> int | None:
        return int(self.dir_mode, 8) if self.dir_mode else None


# Placeholders each template may use (checked when the settings are loaded).
TEMPLATE_FIELDS: dict[str, frozenset[str]] = {
    "branch_template": frozenset({"branch_prefix", "dag_id", "environment", "environment_path"}),
    "promote_branch_template": frozenset({"branch_prefix", "dag_id", "from_env", "to_env"}),
    "commit_message_template": frozenset({"dag_id", "environment", "author"})
    | CONTEXT_PLACEHOLDERS,
    "delete_message_template": frozenset({"dag_id", "environment", "author"})
    | CONTEXT_PLACEHOLDERS,
    "promote_message_template": frozenset({"dag_id", "from_env", "to_env", "author"})
    | CONTEXT_PLACEHOLDERS,
    "pr_title_template": frozenset({"dag_id", "environment", "author", "message"})
    | CONTEXT_PLACEHOLDERS,
    "pr_body_template": frozenset({"dag_id", "environment", "author", "message"})
    | CONTEXT_PLACEHOLDERS,
}


def check_template(template: str, allowed: frozenset[str]) -> None:
    """Every ``{placeholder}`` of ``template`` must be one of ``allowed``."""
    try:
        fields = [f for _, f, _, _ in string.Formatter().parse(template) if f is not None]
    except ValueError as exc:
        raise ValueError(f"invalid template {template!r}: {exc}") from exc
    for name in fields:
        if name not in allowed:
            raise ValueError(
                f"unknown placeholder {{{name}}} (allowed: {', '.join(sorted(allowed))})"
            )


_URL_SLUG = re.compile(
    r"^(?:[a-z+]+://(?:[^@/]+@)?[^/]+/|[^@\s]+@[^:\s]+:)(?P<slug>.+?)(?:\.git)?/?$"
)


def repo_slug(url_or_slug: str) -> str:
    """``https://github.com/o/r.git`` / ``git@github.com:o/r.git`` -> ``o/r``."""
    match = _URL_SLUG.match(url_or_slug.strip())
    return match.group("slug") if match else url_or_slug.strip().strip("/")


def object_locks(root: Any, storage: StorageSettings) -> DocumentStore | None:
    """mode=object: the document store holding the per-DAG write leases of
    :class:`~autoair.storage.object.ObjectStorageTarget` (``root``: the full ``Settings``;
    a SQL ``database.backend`` makes them shared by every replica)."""
    if storage.mode != "object":
        return None
    from autoair.persistence import document_store, is_sql

    if not is_sql(root):
        log.warning(
            "storage.mode=object with database.backend=file: write leases are shared only "
            "by the processes of this host; use a SQL backend for several replicas"
        )
    return document_store(
        root, "object_deploy_locks", file_url=str(Path(root.state_dir) / "object-locks")
    )


def build_target(
    settings: StorageSettings,
    *,
    default_local_path: Path,
    locks: DocumentStore | None = None,
) -> DeployTarget:
    """Instantiate the configured deploy target (``locks``: :func:`object_locks`)."""
    if settings.mode == "local":
        from autoair.storage.local import LocalFolderTarget

        return LocalFolderTarget(
            settings.local_path or default_local_path,
            history_limit=settings.history_limit,
            file_mode=settings.file_mode_int(),
            dir_mode=settings.dir_mode_int(),
            temp_dir=settings.temp_dir,
        )
    if settings.mode == "object":
        if not settings.object_url:
            raise StorageError("storage mode 'object' needs object_url")
        from autoair.storage.object import ObjectStorageTarget

        return ObjectStorageTarget(
            settings.object_url,
            settings.storage_options,
            history_limit=settings.history_limit,
            locks=locks,
            lock_seconds=settings.object_lock_seconds,
            lock_wait_seconds=settings.object_lock_wait_seconds,
        )
    return _build_git(settings.git)


UNAVAILABLE_RETRY_SECONDS = 10.0
"""Reads retry building an unavailable deploy target at most this often (writes always)."""


class UnavailableTarget:
    """Stand-in for a deploy target that cannot be built yet (missing Airflow connection,
    unreachable bundle config…), so AutoAir still starts: reads are empty, writes fail
    with the reason, :meth:`status` reports it (``/api/ready``, the admin panel), and the
    owner (:class:`autoair.api.app.Services`) retries building it, at the latest on the
    next deploy."""

    def __init__(self, settings: StorageSettings, error: BaseException | str) -> None:
        self.kind: TargetKindName = settings.mode
        self.settings = settings
        self.error = ""
        self.retry_at = 0.0
        self.failed(error)

    def failed(self, error: BaseException | str) -> None:
        self.error = str(error) or type(error).__name__
        self.retry_at = time.monotonic() + UNAVAILABLE_RETRY_SECONDS

    def _refuse(self) -> StorageError:
        return StorageError(
            f"the {self.kind} deploy target is unavailable: {self.error} (AutoAir retries "
            "building it on the next deploy)"
        )

    def list(self) -> list[SpecSummary]:
        return []

    def get(self, dag_id: str) -> StoredSpec | None:
        return None

    def save(
        self,
        spec: DagSpec,
        files: dict[str, str],
        *,
        base_etag: str | None = None,
        author: str | None = None,
        message: str | None = None,
        environment: str | None = None,
        force: bool = False,
        **_options: Any,
    ) -> SaveResult:
        raise self._refuse()

    def delete(
        self,
        dag_id: str,
        *,
        base_etag: str | None = None,
        author: str | None = None,
        **_options: Any,
    ) -> bool:
        raise self._refuse()

    def history(self, dag_id: str, limit: int = 50) -> builtins.list[VersionInfo]:
        return []

    def get_version(self, dag_id: str, version: str) -> DagSpec:
        raise self._refuse()

    def status(self) -> TargetStatus:
        return TargetStatus(
            kind=self.kind,
            location="(unavailable)",
            writable=False,
            details={"error": self.error, "unavailable": True},
        )


def build_target_or_unavailable(
    settings: StorageSettings,
    *,
    default_local_path: Path,
    locks: DocumentStore | None = None,
) -> DeployTarget:
    """:func:`build_target`, or an :class:`UnavailableTarget` carrying the error (logged)."""
    try:
        return build_target(settings, default_local_path=default_local_path, locks=locks)
    except Exception as exc:
        log.warning(
            "deploy target (storage.mode=%s) unavailable, starting degraded: %s",
            settings.mode,
            exc,
        )
        return UnavailableTarget(settings, exc)


def build_git_target_with(
    cfg: GitStorageSettings,
    credentials: GitCredentials,
    *,
    reload: Callable[[], GitCredentials | None] | None = None,
) -> DeployTarget:
    """The git target of ``cfg`` using ``credentials`` (a user's own connection) instead of
    the shared ones. ``reload`` re-reads them (cache TTL, retry after an auth failure)."""
    return _build_git(cfg, credentials, reload=reload)


def _fingerprint(creds: GitCredentials | None) -> str:
    """Identifies a set of credentials without keeping the secrets around in clear."""
    if creds is None:
        return ""
    return hashlib.sha256(creds.model_dump_json().encode()).hexdigest()


class ConnectionCredentials:
    """Credentials of an Airflow connection, re-read every ``ttl`` seconds (and on demand
    after an authentication failure), so a rotated token works without a restart. Hands a
    new provider to the :class:`~autoair.storage.git.target.GitTarget` only when they
    changed; a failed or empty read keeps the last known credentials."""

    def __init__(
        self,
        load: Callable[[], GitCredentials | None],
        make: Callable[[GitCredentials | None], Any],
        current: GitCredentials | None,
        *,
        ttl: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._load = load
        self._make = make
        self._fingerprint = _fingerprint(current)
        self._ttl = ttl
        self._clock = clock
        self._expires = clock() + ttl

    def __call__(self, force: bool = False) -> Any:
        now = self._clock()
        if not force and now < self._expires:
            return None
        self._expires = now + self._ttl
        creds = self._load()
        if creds is None:
            return None
        fingerprint = _fingerprint(creds)
        if fingerprint == self._fingerprint:
            return None
        provider = self._make(creds)
        self._fingerprint = fingerprint
        log.info("git credentials of the Airflow connection changed; using the new ones")
        return provider


def build_request_token_target(
    cfg: GitStorageSettings, token: str, *, transport: Any = None
) -> DeployTarget:
    """The git target of ``cfg`` authenticated with a token the user typed for this one
    write (:mod:`autoair.storage.request_token`): nothing from the configured connection
    is used but the repository location, nothing is cached or re-read, and the target is
    dropped after the request. ``transport`` (an ``httpx2`` transport) is a test hook."""
    from autoair.storage.git.config import GitCredentials

    return _build_git(cfg, GitCredentials(token=token), request_token=True, transport=transport)


def _build_git(
    cfg: GitStorageSettings,
    creds_override: GitCredentials | None = None,
    *,
    reload: Callable[[], GitCredentials | None] | None = None,
    request_token: bool = False,
    transport: Any = None,
) -> DeployTarget:
    from autoair.storage.git import GitTarget
    from autoair.storage.git.config import bundle_git_settings, credentials_from_connection
    from autoair.storage.git.provider import PullRequestOptions

    repo, base_branch, subdir, conn_id = cfg.repo, cfg.base_branch, cfg.subdir, cfg.conn_id
    if cfg.forced_conn_id:
        conn_id = cfg.forced_conn_id
    if cfg.use_airflow_bundle:
        bundle = bundle_git_settings(cfg.bundle_name)
        if bundle is None:
            raise StorageError("use_airflow_bundle is set but no GitDagBundle is configured")
        repo = repo or bundle.repo_url
        base_branch = base_branch or bundle.tracking_ref
        subdir = subdir if subdir is not None else bundle.subdir
        conn_id = cfg.forced_conn_id or conn_id or bundle.git_conn_id
    creds: GitCredentials | None = creds_override
    if creds is None and conn_id:
        creds = credentials_from_connection(conn_id)
    if request_token and not repo and conn_id:
        # Only the repository location of the configured connection; never its secret.
        shared = credentials_from_connection(conn_id)
        repo = shared.repo_url if shared else None
    repo = repo or (creds.repo_url if creds else None)
    if not repo:
        hint = f" or a host in Airflow connection {conn_id!r}" if conn_id else ""
        raise StorageError(f"git storage needs a repository (git.repo{hint})")
    repository = repo
    per_user = creds_override is not None

    def make(credentials: GitCredentials | None) -> Any:
        return _git_provider(
            cfg,
            repository,
            credentials,
            per_user=per_user,
            request_token=request_token,
            transport=transport,
        )

    provider = make(creds)
    refresh: ConnectionCredentials | None = None
    if request_token:
        pass  # a token typed for this one write: never re-read, never replaced
    elif per_user and reload is not None:
        refresh = ConnectionCredentials(reload, make, creds, ttl=cfg.credentials_ttl)
    elif not per_user and conn_id and (cfg.forced_conn_id or not cfg.token):
        # The token (or SSH key / GitHub App) comes from the Airflow connection.
        refresh = ConnectionCredentials(
            partial(credentials_from_connection, conn_id), make, creds, ttl=cfg.credentials_ttl
        )
    return GitTarget(
        provider,
        base_branch=base_branch or "main",
        subdir=subdir or "",
        mode=cfg.mode,
        branch_prefix=cfg.branch_prefix,
        environments=cfg.environments,
        branch_template=cfg.branch_template,
        promote_branch_template=cfg.promote_branch_template,
        commit_message_template=cfg.commit_message_template,
        delete_message_template=cfg.delete_message_template,
        promote_message_template=cfg.promote_message_template,
        pr_title_template=cfg.pr_title_template,
        pr_body_template=cfg.pr_body_template,
        pr_options=PullRequestOptions(
            draft=cfg.pr_draft,
            labels=tuple(cfg.pr_labels),
            reviewers=tuple(cfg.pr_reviewers),
            delete_branch=cfg.delete_branch_after_merge,
        ),
        direct_push_roles=cfg.direct_push_roles,
        max_attempts=cfg.max_attempts,
        cache_size=cfg.read_cache_size,
        provider_refresh=refresh,
    )


def _git_provider(
    cfg: GitStorageSettings,
    repo: str,
    creds: GitCredentials | None,
    *,
    per_user: bool,
    request_token: bool = False,
    transport: Any = None,
) -> GitProvider:
    """The host API client of ``cfg`` authenticated with ``creds`` (an Airflow connection;
    ``per_user``: the deploying user's own; ``request_token``: only the token the user
    typed for this write — GitHub App, SSH keys and app-password users are not used)."""
    from autoair.storage.git import (
        GitHubAppTokenSource,
        GitHubProvider,
        GitLabProvider,
        LocalGitProvider,
    )

    # Per-user credentials, and a forced connection, replace any configured token.
    if per_user or cfg.forced_conn_id:
        token = creds.token if creds else None
    else:
        token = cfg.token.get_secret_value() if cfg.token else (creds.token if creds else None)
    private_key = cfg.github_private_key.get_secret_value() if cfg.github_private_key else None
    if private_key is None and cfg.github_private_key_file is not None:
        private_key = cfg.github_private_key_file.expanduser().read_text(encoding="utf-8")

    provider: GitProvider
    # per_user without shared credentials: anonymous reads (public repositories); every
    # write uses the deploying user's own connection (autoair.storage.credentials).
    # request_token=required: every write brings its own token, reads may be anonymous.
    needs_request_token = cfg.request_token == "required"  # noqa: S105 - a mode, not a secret
    anonymous = (cfg.credentials_mode == "per_user" or needs_request_token) and not per_user
    http: dict[str, Any] = {"transport": transport} if transport is not None else {}
    if cfg.provider == "github":
        app_id = cfg.github_app_id or (creds.github_app_id if creds else None)
        installation = cfg.github_installation_id or (
            creds.github_installation_id if creds else None
        )
        api_url = cfg.api_url or "https://api.github.com"
        if app_id and installation and not request_token:
            key = private_key or (creds.private_key if creds else None)
            if not key:
                raise StorageError("GitHub App auth needs a private key")
            provider = GitHubProvider(
                repo_slug(repo),
                GitHubAppTokenSource(
                    app_id, key, installation, api_url=api_url, timeout=cfg.timeout
                ),
                api_url=api_url,
                timeout=cfg.timeout,
            )
        else:
            if not token and not anonymous:
                raise StorageError("GitHub storage needs a token or GitHub App credentials")
            provider = GitHubProvider(
                repo_slug(repo), token or "", api_url=api_url, timeout=cfg.timeout, **http
            )
    elif cfg.provider == "gitlab":
        if not token and not anonymous:
            raise StorageError("GitLab storage needs a token")
        provider = GitLabProvider(
            repo if repo.isdigit() else repo_slug(repo),
            token or "",
            api_url=cfg.api_url or "https://gitlab.com/api/v4",
            timeout=cfg.timeout,
            **http,
        )
    elif cfg.provider == "bitbucket":
        if not token and not anonymous:
            raise StorageError("Bitbucket storage needs an access token or app password")
        from autoair.storage.git.bitbucket import DEFAULT_API_URL, BitbucketProvider

        provider = BitbucketProvider(
            repo,
            token or "",
            # A request token is an access token (Bearer), not the service's app password.
            username=None if request_token else cfg.username,
            api_url=cfg.api_url or DEFAULT_API_URL,
            timeout=cfg.timeout,
            **http,
        )
    elif cfg.provider == "azure":
        if not token and not anonymous:
            raise StorageError("Azure DevOps storage needs a PAT or bearer token")
        from autoair.storage.git.azure import AzureDevOpsProvider

        provider = AzureDevOpsProvider(
            repo,
            token or "",
            api_url=cfg.api_url or "https://dev.azure.com",
            bearer=cfg.azure_bearer and not request_token,  # a typed token is a PAT
            timeout=cfg.timeout,
            **http,
        )
    else:  # local / gitea: the git CLI pushes; gitea opens pull requests over its API
        key_file = cfg.ssh_key_file or (Path(creds.key_file) if creds and creds.key_file else None)
        if request_token:
            key_file = None  # the typed token authenticates the HTTPS push, nothing else
        git_kwargs: dict[str, Any] = {
            "token": token,
            "username": (creds.username if creds and creds.username else "x-access-token"),
            "ssh_key_file": key_file,
            "private_key": (
                None if key_file or request_token else (creds.private_key if creds else None)
            ),
            "fetch_interval": cfg.fetch_interval,
            "committer": (cfg.committer_name, cfg.committer_email),
            "command_timeout": cfg.command_timeout,
            "env_passthrough": cfg.env_passthrough,
            "ssh_key_cache_days": cfg.ssh_key_cache_days,
        }
        if cfg.provider == "gitea":
            from autoair.storage.git.gitea import GiteaProvider

            provider = GiteaProvider(
                repo,
                cfg.cache_dir,
                api_url=cfg.api_url,
                api_timeout=cfg.timeout,
                **http,
                **git_kwargs,
            )
        else:
            provider = LocalGitProvider(repo, cfg.cache_dir, **git_kwargs)
    return provider
