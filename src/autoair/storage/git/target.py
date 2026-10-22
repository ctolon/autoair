"""Git deploy target: specs + generated DAGs committed to a repository.

Pairs with Airflow's ``GitDagBundle``. Two modes:

* ``push``: every save is one commit straight onto the base (or environment) branch,
  guarded by compare-and-swap on the branch head;
* ``pr``: every save commits to ``<branch_prefix><dag_id>`` (created from the base
  head when absent) and opens / reuses a pull request into the base branch. While such
  a pending branch exists, :meth:`GitTarget.get` returns the work-in-progress version so
  the editor keeps building on it.

Each save is a single commit, so the spec, the generated file(s) and the AutoAir state
file always change atomically.
"""

from __future__ import annotations

import builtins
import functools
import logging
import re
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any, Literal, ParamSpec, TypeVar

from pydantic import ValidationError

from autoair.loader import SPEC_SUFFIX
from autoair.spec import DagSpec
from autoair.storage.base import (
    STATE_DIR,
    AuthError,
    ConflictError,
    DagState,
    ExistingFile,
    HandWrittenError,
    NotFoundError,
    SaveResult,
    SpecSummary,
    StorageError,
    StoredSpec,
    TargetKind,
    TargetStatus,
    VersionInfo,
    check_dag_id,
    check_files,
    check_overwrite,
    is_owned,
    main_file,
    parse_state,
    serialize_spec,
    sha256_text,
    source_sha,
    spec_etag,
    spec_filename,
    summary_of,
    utcnow,
)
from autoair.storage.git.provider import (
    GitProvider,
    PullRequest,
    PullRequestOptions,
    check_branch,
    check_ref,
)

if TYPE_CHECKING:
    from autoair.governance import DeployContext

log = logging.getLogger(__name__)

GitMode = Literal["pr", "push"]
FileRenderer = Callable[[DagSpec, "str | None"], dict[str, str]]
"""``(spec, environment) -> {file name: content}`` used to re-render on promotion."""

_FULL_SHA = re.compile(r"^[0-9a-f]{40,64}$")
_MAX_ATTEMPTS = 3  # default of AUTOAIR_STORAGE__GIT__MAX_ATTEMPTS
_CACHE_SIZE = 512  # default of AUTOAIR_STORAGE__GIT__READ_CACHE_SIZE


class _Fmt(dict[str, str]):
    """``str.format_map`` mapping: placeholders the caller does not know render empty."""

    def __missing__(self, key: str) -> str:
        return ""


@dataclass(frozen=True)
class CommitTexts:
    commit_message: str
    pr_title: str
    pr_body: str


class _Retry(Exception):
    """A branch moved between reading and committing; start over."""


ProviderRefresh = Callable[[bool], "GitProvider | None"]
"""``refresh(force) -> provider``: re-reads the credentials (an Airflow connection) when
their cache expired or ``force``; returns a new provider when they changed, else ``None``."""

_P = ParamSpec("_P")
_R = TypeVar("_R")


def _fresh_credentials(method: Callable[_P, _R]) -> Callable[_P, _R]:
    """Refresh connection-backed credentials (cache TTL) before ``method``; when the host
    refuses them (:class:`AuthError`), re-read the connection and retry once if it changed
    (a rotated token works without a restart). Operations are idempotent, so a retry after
    a partial write (branch pushed, PR refused) finishes the job."""

    @functools.wraps(method)
    def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        target = args[0]
        assert isinstance(target, GitTarget)
        target.refresh_credentials()
        try:
            return method(*args, **kwargs)
        except AuthError:
            if not target.refresh_credentials(force=True):
                raise
            log.info("git credentials changed after an authentication failure; retrying")
            return method(*args, **kwargs)

    return wrapper


class GitTarget:
    """A :class:`~autoair.storage.base.DeployTarget` backed by a :class:`GitProvider`."""

    kind: TargetKind = "git"

    def __init__(
        self,
        provider: GitProvider,
        *,
        base_branch: str = "main",
        subdir: str = "",
        mode: GitMode = "pr",
        branch_prefix: str = "autoair/",
        environments: dict[str, str] | None = None,
        branch_template: str = "{branch_prefix}{environment_path}{dag_id}",
        promote_branch_template: str = "{branch_prefix}promote/{from_env}-to-{to_env}/{dag_id}",
        commit_message_template: str = "AutoAir: update DAG {dag_id}",
        delete_message_template: str = "AutoAir: delete DAG {dag_id}",
        promote_message_template: str = "AutoAir: promote DAG {dag_id} from {from_env} to {to_env}",
        pr_title_template: str = "AutoAir: update DAG {dag_id}",
        pr_body_template: str = "{message}\n\n{description}\n\nOpened by AutoAir.",
        pr_options: PullRequestOptions | None = None,
        direct_push_roles: Iterable[str] | None = None,
        max_attempts: int = _MAX_ATTEMPTS,
        cache_size: int = _CACHE_SIZE,
        provider_refresh: ProviderRefresh | None = None,
    ) -> None:
        self.provider = provider
        # Credentials read from an Airflow connection (storage.git.credentials_ttl).
        self._provider_refresh = provider_refresh
        self._refresh_lock = threading.Lock()
        self.base_branch = check_branch(base_branch)
        parts = [p for p in subdir.replace("\\", "/").split("/") if p and p != "."]
        if any(p == ".." for p in parts):
            raise StorageError("subdir must stay inside the repository")
        self.subdir = "/".join(parts)
        self.mode: GitMode = mode
        self.branch_prefix = branch_prefix
        self.environments = {k: check_branch(v) for k, v in (environments or {}).items()}
        self.branch_template = branch_template
        self.promote_branch_template = promote_branch_template
        self.commit_message_template = commit_message_template
        self.delete_message_template = delete_message_template
        self.promote_message_template = promote_message_template
        self.pr_title_template = pr_title_template
        self.pr_body_template = pr_body_template
        self.pr_options = pr_options or PullRequestOptions()
        # Informational (status); autoair.iam.deps.direct_push_roles decides who may push.
        self.direct_push_roles = None if direct_push_roles is None else frozenset(direct_push_roles)
        self.max_attempts = max(1, max_attempts)
        self.cache_size = max(0, cache_size)
        self._cache: OrderedDict[tuple[str, str, str], Any] = OrderedDict()
        self._cache_lock = threading.Lock()

    def refresh_credentials(self, *, force: bool = False) -> bool:
        """Swap in a provider built from re-read credentials; ``True`` when they changed."""
        if self._provider_refresh is None:
            return False
        with self._refresh_lock:
            try:
                provider = self._provider_refresh(force)
            except Exception:
                log.warning("cannot re-read the git credentials", exc_info=True)
                return False
            if provider is None:
                return False
            self.provider = provider
            return True

    # ----------------------------------------------------------------- paths
    def _path(self, name: str) -> str:
        return f"{self.subdir}/{name}" if self.subdir else name

    def _spec_path(self, dag_id: str) -> str:
        return self._path(spec_filename(dag_id))

    def _state_path(self, dag_id: str) -> str:
        return self._path(f"{STATE_DIR}/{check_dag_id(dag_id)}.json")

    def _branch_for(self, environment: str | None) -> str:
        if not environment:
            return self.base_branch
        try:
            return self.environments[environment]
        except KeyError:
            raise StorageError(f"no branch configured for environment {environment!r}") from None

    def pending_branch(self, dag_id: str, environment: str | None = None) -> str:
        name = self.branch_template.format_map(
            _Fmt(
                branch_prefix=self.branch_prefix,
                dag_id=check_dag_id(dag_id),
                environment=environment or "",
                environment_path=f"{environment}/" if environment else "",
            )
        )
        return check_branch(name)

    def _text(self, template: str, **values: str | None) -> str:
        return template.format_map(_Fmt({k: v or "" for k, v in values.items()}))

    def _describe(self, branch: str, path: str) -> str:
        return f"{self.provider.location}@{branch}:{path}"

    # ----------------------------------------------------------------- cached reads
    def _cached(self, key: tuple[str, str, str], load: Callable[[], Any]) -> Any:
        if not _FULL_SHA.fullmatch(key[1]):  # only immutable snapshots are cacheable
            return load()
        # Requests run in a thread pool: OrderedDict reordering is not thread-safe.
        with self._cache_lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return self._cache[key]
        value = load()  # outside the lock: a provider round-trip must not serialise reads
        with self._cache_lock:
            self._cache[key] = value
            while len(self._cache) > self.cache_size:
                self._cache.popitem(last=False)
        return value

    def _read(self, ref: str, path: str) -> str | None:
        content: str | None = self._cached(
            ("file", ref, path), lambda: self.provider.get_file(ref, path)[0]
        )
        return content

    def _read_spec(self, ref: str, dag_id: str) -> StoredSpec | None:
        raw = self._read(ref, self._spec_path(dag_id))
        if raw is None:
            return None
        try:
            spec = DagSpec.model_validate_json(raw)
        except ValidationError as exc:
            raise StorageError(f"stored spec for {dag_id!r} is invalid: {exc}") from exc
        return StoredSpec(spec=spec, etag=spec_etag(spec), version=ref)

    def _read_state(self, ref: str, dag_id: str) -> DagState | None:
        raw = self._read(ref, self._state_path(dag_id))
        if raw is None:
            return None
        try:
            return DagState.model_validate_json(raw)
        except ValidationError:
            log.warning("ignoring unreadable AutoAir state for %s", dag_id)
            return None

    def _open_pr(self, branch: str) -> PullRequest | None:
        if not self.provider.supports_pull_requests:
            return None
        return self.provider.find_open_pr(branch)

    def _pending(self, dag_id: str, environment: str | None) -> tuple[str, str, PullRequest | None]:
        """``(branch, head, open PR)`` of an active pending branch or ``("", "", None)``."""
        branch = self.pending_branch(dag_id, environment)
        head = self.provider.head(branch)
        if head is None:
            return "", "", None
        pr = self._open_pr(branch)
        if self.provider.supports_pull_requests and pr is None:
            return "", "", None  # merged or closed: the branch is stale
        return branch, head, pr

    # ----------------------------------------------------------------- reads
    @_fresh_credentials
    def get(self, dag_id: str, *, environment: str | None = None) -> StoredSpec | None:
        check_dag_id(dag_id)
        if self.mode == "pr":
            branch, head, pr = self._pending(dag_id, environment)
            if branch:
                stored = self._read_spec(head, dag_id)
                if stored is not None:
                    stored.pending_branch = branch
                    stored.pending_pr_url = pr.url if pr else None
                return stored
        base_head = self.provider.head(self._branch_for(environment))
        return self._read_spec(base_head, dag_id) if base_head else None

    @_fresh_credentials
    def current_file(
        self, dag_id: str, *, environment: str | None = None, mode: GitMode | None = None
    ) -> ExistingFile | None:
        """The main DAG file a save would commit on top of: on the pending branch when a
        pull request for the DAG is open (PR mode), else on the target branch."""
        check_dag_id(dag_id)
        mode = mode or self.mode
        target = self._branch_for(environment)
        branch, head = target, self.provider.head(target)
        if mode == "pr":
            pending, pending_head, _pr = self._pending(dag_id, environment)
            if pending:
                branch, head = pending, pending_head
        if not head:
            return None
        name = f"{dag_id}.py"
        path = self._path(name)
        content = self._read(head, path)
        if content is None:
            return None
        state = self._read_state(head, dag_id)
        return ExistingFile(
            name=name,
            content=content,
            location=self._describe(branch, path),
            recorded_sha=state.files.get(name) if state else None,
            source_sha=state.source_sha if state else None,
            hand_written=state.hand_written if state else False,
        )

    @_fresh_credentials
    def list(self, *, environment: str | None = None) -> list[SpecSummary]:
        branch = self._branch_for(environment)
        head = self.provider.head(branch)
        if head is None:
            return []
        names: list[str] = self._cached(
            ("ls", head, self.subdir), lambda: self.provider.list_files(head, self.subdir)
        )
        out: list[SpecSummary] = []
        for path in sorted(names):
            name = path.rsplit("/", 1)[-1]
            if not name.endswith(SPEC_SUFFIX) or name.startswith("."):
                continue
            dag_id = name.removesuffix(SPEC_SUFFIX)
            try:
                stored = self._read_spec(head, dag_id)
            except StorageError:
                log.warning("skipping unreadable spec %s", path, exc_info=True)
                continue
            if stored is None or stored.spec.dag_id != dag_id:
                continue
            state = self._read_state(head, dag_id)
            commits = self._cached(("log", head, path), partial(self.provider.log, path, head, 1))
            out.append(
                summary_of(
                    stored.spec,
                    updated_at=(commits[0].timestamp if commits else None) or utcnow(),
                    dag_file=self._describe(
                        branch,
                        self._path(main_file(dag_id, list(state.files) if state else [])),
                    ),
                    etag=stored.etag,
                )
            )
        return out

    @_fresh_credentials
    def history(
        self, dag_id: str, limit: int = 50, *, environment: str | None = None
    ) -> builtins.list[VersionInfo]:
        commits = self.provider.log(
            self._spec_path(dag_id), self._branch_for(environment), max(1, limit)
        )
        return [
            VersionInfo(version=c.sha, timestamp=c.timestamp, author=c.author, message=c.message)
            for c in commits
        ]

    @_fresh_credentials
    def get_version(self, dag_id: str, version: str) -> DagSpec:
        check_ref(version)
        stored = self._read_spec(version, dag_id)
        if stored is None:
            raise NotFoundError(f"no version {version!r} of {dag_id!r}")
        return stored.spec

    @_fresh_credentials
    def status(self) -> TargetStatus:
        details: dict[str, Any] = {
            "provider": self.provider.name,
            "base_branch": self.base_branch,
            "mode": self.mode,
            "subdir": self.subdir,
            "branch_prefix": self.branch_prefix,
            "environments": dict(self.environments),
            "direct_push_roles": (
                None if self.direct_push_roles is None else sorted(self.direct_push_roles)
            ),
            "pr_draft": self.pr_options.draft,
        }
        writable = True
        try:
            details["head"] = self.provider.head(self.base_branch)
        except StorageError as exc:
            writable = False
            details["error"] = str(exc)
        return TargetStatus(
            kind="git", location=self.provider.location, writable=writable, details=details
        )

    # ----------------------------------------------------------------- writes
    def _work_branch(self, branch: str, target: str) -> tuple[str | None, PullRequest | None]:
        """Head to commit on top of (creating / recycling ``branch`` as needed)."""
        if branch == target:
            return self.provider.head(target), None
        head = self.provider.head(branch)
        if head is not None:
            pr = self._open_pr(branch)
            if self.provider.supports_pull_requests and pr is None:
                base = self.provider.head(target)
                if base is None:
                    raise StorageError(f"branch {target!r} does not exist")
                try:
                    self.provider.reset_branch(branch, base, expect=head)
                except ConflictError:
                    raise _Retry from None  # pushed to meanwhile: decide again
                return base, None
            return head, pr
        base = self.provider.head(target)
        if base is None:
            raise StorageError(f"branch {target!r} does not exist")
        try:
            self.provider.create_branch(branch, base)
        except ConflictError:
            raise _Retry from None
        return base, None

    def _commit(
        self,
        dag_id: str,
        spec: DagSpec | None,
        files: dict[str, str],
        *,
        branch: str,
        head: str | None,
        base_etag: str | None,
        force: bool,
        author: str | None,
        message: str,
        environment: str | None,
        as_service: bool = False,
        expected: dict[str, str] | None = None,
        hand_written: bool | None = None,
        allow_hand_written: bool = False,
    ) -> tuple[str | None, bool]:
        """Commit a save (``spec``) or a delete (``spec is None``) on ``branch``.

        ``expected``: file name -> sha256 of the file a source-preserving deploy patched
        (overwritten only while it is unchanged on the branch). Returns ``(commit sha,
        changed)``.
        """
        expected = expected or {}
        current = self._read_spec(head, dag_id) if head else None
        if base_etag is not None and (current is None or current.etag != base_etag):
            raise ConflictError(current)
        state = (
            parse_state(
                self._read(head, self._state_path(dag_id)),
                self._describe(branch, self._state_path(dag_id)),
                dag_id,
            )
            if head
            else None
        )
        recorded = state.files if state else {}
        hashes = {name: sha256_text(content) for name, content in files.items()}
        changes: dict[str, str | None] = {}
        kept = state.hand_written if state is not None else False
        for name, content in files.items():
            path = self._path(name)
            existing = self._read(head, path) if head else None
            # Every write (loader/YAML output modes, raw code, promotion, MCP) commits here,
            # so the kept hand-written file is protected here and not only by the plan.
            if kept and existing is not None and name not in expected and not allow_hand_written:
                raise HandWrittenError(self._describe(branch, path))
            check_overwrite(
                self._describe(branch, path),
                existing,
                recorded.get(name),
                hashes[name],
                force,
                expected.get(name),
                unknown=state is None,
            )
            changes[path] = content
        olds = list(recorded) or ([f"{dag_id}.py"] if spec is None else [])
        for name in olds:
            if name in files:
                continue
            path = self._path(name)
            existing = self._read(head, path) if head else None
            if existing is None:
                continue
            if kept and not allow_hand_written:
                raise HandWrittenError(self._describe(branch, path))
            if kept:
                check_overwrite(
                    self._describe(branch, path), existing, None, None, False, recorded.get(name)
                )
                changes[path] = None
                continue
            if not is_owned(existing):
                log.warning("leaving %s in place: it is not owned by AutoAir", path)
                continue
            check_overwrite(
                self._describe(branch, path),
                existing,
                recorded.get(name),
                None,
                force,
                unknown=state is None,
            )
            changes[path] = None
        if hand_written is not None:
            kept = hand_written
        if spec is None:
            if current is None:
                return head, False
            changes[self._spec_path(dag_id)] = None
            changes[self._state_path(dag_id)] = None
        else:
            etag = spec_etag(spec)
            if (
                current is not None
                and state is not None
                and current.etag == etag
                and state.files == hashes
                and state.source_sha == source_sha(spec)
                and state.hand_written == kept
                and all(v is not None for v in changes.values())
            ):
                return head, False
            changes[self._spec_path(dag_id)] = serialize_spec(spec)
            changes[self._state_path(dag_id)] = (
                DagState(
                    dag_id=dag_id,
                    etag=etag,
                    files=hashes,
                    updated_at=utcnow(),
                    author=author,
                    environment=environment,
                    source_sha=source_sha(spec),
                    hand_written=kept,
                ).model_dump_json(indent=2)
                + "\n"
            )
        try:
            # as_service: the service identity authors the commit (the user is in trailers)
            sha = self.provider.commit_files(
                branch, head, changes, message, None if as_service else author
            )
        except ConflictError:
            raise _Retry from None
        return sha, sha != head

    @_fresh_credentials
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
        mode: GitMode | None = None,
        context: DeployContext | None = None,
        co_author: bool = False,
        expected: dict[str, str] | None = None,
        hand_written: bool | None = None,
        allow_hand_written: bool = False,
    ) -> SaveResult:
        """Commit ``spec`` + ``files``; ``mode`` overrides the configured mode for this save
        (the deploy route decides it from the caller's ``deploy:direct_push`` right).
        ``allow_hand_written``: the caller may replace a hand-written file AutoAir keeps
        without ``expected`` covering it (``deploy:overwrite_ejected``).

        ``context`` (:mod:`autoair.governance`) adds the mandatory context block and git
        trailers to the commit message and the pull request description; ``co_author``
        credits the user with ``Co-authored-by`` (commits authored by the service)."""
        dag_id = check_dag_id(spec.dag_id)
        check_files(dag_id, files)
        target = self._branch_for(environment)
        mode = mode or self.mode
        branch = target if mode == "push" else self.pending_branch(dag_id, environment)
        texts = self.compose(
            dag_id,
            environment=environment,
            author=author,
            message=message,
            context=context,
            co_author=co_author,
        )
        commit_message = texts.commit_message
        sha, changed, pr = self._with_retries(
            lambda: self._work_branch(branch, target),
            lambda head: self._commit(
                dag_id,
                spec,
                files,
                branch=branch,
                head=head,
                base_etag=base_etag,
                force=force,
                author=author,
                message=commit_message,
                environment=environment,
                as_service=co_author,
                expected=expected,
                hand_written=hand_written,
                allow_hand_written=allow_hand_written,
            ),
            dag_id=dag_id,
            environment=environment,
        )
        if branch != target and pr is None:
            pr = self._ensure_pr(branch, target, texts.pr_title, texts.pr_body)
        path = self._describe(branch, self._path(main_file(dag_id, files)))
        verb = "committed" if changed else "unchanged"
        return SaveResult(
            path=path,
            etag=spec_etag(spec),
            version=sha,
            pr_url=pr.url if pr else None,
            branch=branch,
            message=f"{verb}: {path}" + (f"; pull request {pr.url}" if pr else ""),
        )

    @_fresh_credentials
    def delete(
        self,
        dag_id: str,
        *,
        base_etag: str | None = None,
        author: str | None = None,
        environment: str | None = None,
        force: bool = False,
        mode: GitMode | None = None,
        context: DeployContext | None = None,
        co_author: bool = False,
        allow_hand_written: bool = False,
    ) -> bool:
        check_dag_id(dag_id)
        target = self._branch_for(environment)
        mode = mode or self.mode
        branch = target if mode == "push" else self.pending_branch(dag_id, environment)
        if self.get(dag_id, environment=environment) is None:
            return False
        names = {"dag_id": dag_id, "environment": environment, "author": author}
        title = self._text(self.delete_message_template, **self._context_values(context), **names)
        message = self._governed(
            title,
            context,
            co_author=co_author,
            placed="{context}" in self.delete_message_template,
        )
        _sha, changed, pr = self._with_retries(
            lambda: self._work_branch(branch, target),
            lambda head: self._commit(
                dag_id,
                None,
                {},
                branch=branch,
                head=head,
                base_etag=base_etag,
                force=force,
                author=author,
                message=message,
                environment=environment,
                as_service=co_author,
                allow_hand_written=allow_hand_written,
            ),
            dag_id=dag_id,
            environment=environment,
        )
        if changed and branch != target and pr is None:
            self._ensure_pr(
                branch,
                target,
                title.split("\n", 1)[0],
                self._pr_body(title if context else message, context, names),
            )
        return changed

    # ----------------------------------------------------------------- governance
    def compose(
        self,
        dag_id: str,
        *,
        environment: str | None = None,
        author: str | None = None,
        message: str | None = None,
        context: DeployContext | None = None,
        co_author: bool = False,
    ) -> CommitTexts:
        """The commit message and pull request title/description a save would use (also
        the deploy dialog's preview): the user's message (or commit_message_template), the
        mandatory context block and the trailers."""
        names = {"dag_id": dag_id, "environment": environment, "author": author}
        user_message = (message or "").strip()
        base = user_message or self._text(
            self.commit_message_template, **self._context_values(context), **names
        )
        commit = self._governed(
            base,
            context,
            co_author=co_author,
            placed=not user_message and "{context}" in self.commit_message_template,
        )
        title_values = {**self._context_values(context), **names}
        title_values["message"] = base.split("\n", 1)[0]
        return CommitTexts(
            commit_message=commit,
            pr_title=self._text(self.pr_title_template, **title_values),
            pr_body=self._pr_body(base if context else commit, context, names),
        )

    @staticmethod
    def _context_values(context: DeployContext | None) -> dict[str, str | None]:
        return dict(context.values()) if context is not None else {}

    @staticmethod
    def _governed(
        base: str, context: DeployContext | None, *, co_author: bool, placed: bool
    ) -> str:
        """``base`` plus the mandatory context block and trailers (autoair.governance)."""
        if context is None:
            return base
        from autoair.governance import commit_message

        return commit_message(base, context, co_author=co_author, template_has_context=placed)

    def _pr_body(
        self, message: str, context: DeployContext | None, names: dict[str, str | None]
    ) -> str:
        if context is None:
            text = self._text(self.pr_body_template, message=message, **names)
            return re.sub(r"\n{3,}", "\n\n", text).strip()
        from autoair.governance import pr_body

        values = {**context.values(), **{k: v or "" for k, v in names.items()}}
        values["message"] = message
        return pr_body(self.pr_body_template, context, values)

    def _with_retries(
        self,
        prepare: Callable[[], tuple[str | None, PullRequest | None]],
        commit: Callable[[str | None], tuple[str | None, bool]],
        *,
        dag_id: str,
        environment: str | None,
    ) -> tuple[str | None, bool, PullRequest | None]:
        for _attempt in range(self.max_attempts):
            try:
                head, pr = prepare()
                sha, changed = commit(head)
            except _Retry:
                continue
            return sha, changed, pr
        raise ConflictError(
            self.get(dag_id, environment=environment),
            "the branch kept moving while saving; reload and retry",
        )

    def _ensure_pr(self, branch: str, target: str, title: str, body: str) -> PullRequest | None:
        if not self.provider.supports_pull_requests:
            return None
        existing = self.provider.find_open_pr(branch)
        if existing is not None:
            return existing
        return self.provider.open_pr(branch, target, title, body.strip(), options=self.pr_options)

    # ----------------------------------------------------------------- environments
    @_fresh_credentials
    def promote(
        self,
        dag_id: str,
        from_env: str,
        to_env: str,
        *,
        render: FileRenderer | None = None,
        author: str | None = None,
        context: DeployContext | None = None,
        co_author: bool = False,
    ) -> SaveResult:
        """Open a PR copying ``dag_id`` from the ``from_env`` branch to ``to_env``'s.

        With ``render`` the DAG file(s) are re-rendered for ``to_env`` (environment
        overlays differ); otherwise the generated files are copied verbatim.
        """
        check_dag_id(dag_id)
        source = self._branch_for(from_env)
        target = self._branch_for(to_env)
        if source == target:
            raise StorageError("source and target environments use the same branch")
        source_head = self.provider.head(source)
        stored = self._read_spec(source_head, dag_id) if source_head else None
        if stored is None or source_head is None:
            raise NotFoundError(f"{dag_id!r} does not exist in environment {from_env!r}")
        if render is not None:
            files = render(stored.spec, to_env)
        else:
            state = self._read_state(source_head, dag_id)
            files = {}
            for name in list(state.files) if state else [f"{dag_id}.py"]:
                content = self._read(source_head, self._path(name))
                if content is not None:
                    files[name] = content
        check_files(dag_id, files)
        names = {"dag_id": dag_id, "from_env": from_env, "to_env": to_env}
        branch = check_branch(
            self._text(self.promote_branch_template, branch_prefix=self.branch_prefix, **names)
        )
        title = self._text(
            self.promote_message_template, author=author, **self._context_values(context), **names
        )
        message = self._governed(
            title,
            context,
            co_author=co_author,
            placed="{context}" in self.promote_message_template,
        )
        sha, changed, pr = self._with_retries(
            lambda: self._work_branch(branch, target),
            lambda head: self._commit(
                dag_id,
                stored.spec,
                files,
                branch=branch,
                head=head,
                base_etag=None,
                force=False,
                author=author,
                message=message,
                environment=to_env,
                as_service=co_author,
            ),
            dag_id=dag_id,
            environment=to_env,
        )
        if pr is None:
            body = self._pr_body(
                title if context else message,
                context,
                {"dag_id": dag_id, "environment": to_env, "author": author},
            )
            pr = self._ensure_pr(
                branch, target, title.split("\n", 1)[0], f"{body}\n\nSource commit: {source_head}"
            )
        path = self._describe(branch, self._path(main_file(dag_id, files)))
        return SaveResult(
            path=path,
            etag=stored.etag,
            version=sha,
            pr_url=pr.url if pr else None,
            branch=branch,
            message=(f"promotion committed to {branch}" if changed else "already promoted")
            + (f"; pull request {pr.url}" if pr else ""),
        )
