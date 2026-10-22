"""Human approval of individual agent tool calls (no ``pydantic_ai`` import here).

Which calls wait for a decision is a policy (``ai.approvals``): a tool's class
(:attr:`autoair.ai.tools.AgentTool.kind`, ``write`` by default), explicit name patterns, and
exemptions, per agent. :mod:`autoair.ai.agent` turns the selected tools into pydantic-ai
deferred tools: when the model calls one, the run stops with the pending calls instead of
running them.

The paused run is stored here as a :class:`ToolApproval` document (collection
``ai_tool_approvals``, key ``<user hash>.<id>``): the pending calls (arguments redacted for
display), the run's pydantic-ai messages and what the agent needs to rebuild its tools. Only
the user who started the run can find it. Deciding it claims the document with a
compare-and-set (``pending`` -> ``resuming``), so when two replicas get the same decision
only one resumes the run and an approved tool runs at most once; the other gets
:class:`ApprovalBusy`. A pending approval expires after ``ai.approvals.ttl_seconds``.

The troubleshooting ``hitl`` mode for Airflow actions is a different, later step: those
actions are proposed in the finished analysis and executed by the API, not by the agent.
Tool approval pauses the agent itself. Both use the same claim-then-record pattern and
claim clock (:func:`autoair.ai.assistants.airflow_actions.claim_expired`); unlike an Airflow
action, a stale approval claim is never taken over.
"""

from __future__ import annotations

import datetime
import fnmatch
import hashlib
import json
import os
import re
import socket
import threading
import uuid
from collections.abc import Callable, Iterable, Sequence
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from autoair.ai.config import ApprovalAgent, ToolApprovalRule, ToolApprovalSettings
from autoair.persistence.documents import DocumentConflict, DocumentStore

ApprovalStatus = Literal["pending", "resuming", "resolved", "expired", "cancelled", "failed"]
OPEN_STATUSES: frozenset[str] = frozenset({"pending", "resuming"})
MASK = "***"
#: Argument names whose values are never shown, whatever they look like.
_SECRET_KEY = re.compile(
    r"(?i)(password|passwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|"
    r"authorization|credential|cookie)"
)
#: Compare-and-set retries of :meth:`ApprovalStore.mutate` (only concurrent writers of the
#: same approval cause one).
_MUTATE_ATTEMPTS = 20
#: Characters of a call's arguments the full-arguments view returns: above the largest
#: tool input (an imported DAG file, 200 000 characters) with room for JSON escaping.
FULL_ARGUMENT_CHARS = 1_000_000


# --------------------------------------------------------------------------- policy


#: Tools whose every call waits for a decision whatever the rule says (exemptions and
#: classes do not apply): a stored memory reaches every later prompt of its scope, so a
#: prompt-injected ``remember`` would persist the injection.
ALWAYS_APPROVE: frozenset[str] = frozenset({"remember"})


def _matches(name: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, p) for p in patterns)


def needs_approval(
    name: str, kind: str, rule: ToolApprovalRule, *, force_writes: bool = False
) -> bool:
    """Whether a call of tool ``name`` (class ``kind``) waits for a decision under ``rule``.
    ``force_writes``: write (and execute) tools always do (a troubleshooting analysis in
    ``hitl`` mode). An ``execute`` tool (runs Python on the server) is gated by the
    ``write`` class too, so a rule written before that class existed still gates it.
    :data:`ALWAYS_APPROVE` tools always wait."""
    if name in ALWAYS_APPROVE or _matches(name, rule.tools):
        return True
    if force_writes and kind in ("write", "execute"):
        return True
    if _matches(name, rule.exempt):
        return False
    if kind == "execute":
        return "execute" in rule.classes or "write" in rule.classes
    return kind in rule.classes


def approval_rule(settings: ToolApprovalSettings, agent: ApprovalAgent) -> ToolApprovalRule:
    return settings.rule(agent)


def gated_tools(
    tools: Sequence[Any],
    settings: ToolApprovalSettings,
    agent: ApprovalAgent,
    *,
    force_writes: bool = False,
) -> tuple[list[Any], frozenset[str], list[str]]:
    """Split ``tools`` for one run: (tools offered, names needing approval, notes).

    With ``ai.approvals.enabled`` off the tools that would need approval are withdrawn (they
    never run unapproved) and named in the notes."""
    rule = settings.rule(agent)
    gated = {
        t.name
        for t in tools
        if needs_approval(t.name, getattr(t, "kind", "read"), rule, force_writes=force_writes)
    }
    if settings.enabled or not gated:
        return list(tools), frozenset(gated), []
    kept = [t for t in tools if t.name not in gated]
    note = (
        f"{len(gated)} tool(s) not offered: they need approval and ai.approvals is off "
        f"({', '.join(sorted(gated)[:10])})"
    )
    return kept, frozenset(), [note]


def withdraw_gated(
    tools: Sequence[Any], settings: ToolApprovalSettings, agent: ApprovalAgent
) -> list[Any]:
    """For runs that cannot pause (one-shot requests): drop the tools needing approval."""
    rule = settings.rule(agent)
    return [t for t in tools if not needs_approval(t.name, getattr(t, "kind", "read"), rule)]


# --------------------------------------------------------------------------- models


def now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def iso(at: datetime.datetime) -> str:
    return at.isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_iso(value: str | None) -> datetime.datetime | None:
    if not value:
        return None
    try:
        at = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return at if at.tzinfo else at.replace(tzinfo=datetime.timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex[:16]


def replica_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}"


class PendingToolCall(BaseModel):
    """One tool call waiting for a decision, as the approval card shows it."""

    id: str = Field(description="The model's tool call id.")
    tool: str
    kind: Literal["read", "write", "execute"] = "write"
    description: str = Field(default="", description="The tool's description (shortened).")
    arguments: Any = Field(
        default=None,
        description="Arguments with secrets masked and long values cut (display only).",
    )
    truncated: bool = Field(default=False, description="Arguments were cut for display.")
    omitted_chars: int = Field(
        default=0,
        description="About how many characters of the (masked) arguments the cut left out; "
        "the full arguments come from GET /ai/approvals/{id}/calls/{call_id}/arguments.",
    )
    arguments_sha256: str | None = Field(
        default=None,
        description="SHA-256 of the exact arguments the tool will run with (canonical JSON). "
        "An approval sends it back; the run resumes only when the stored call still has "
        "these arguments.",
    )


class ToolDecision(BaseModel):
    id: str = Field(max_length=200, description="Tool call id.")
    approved: bool
    reason: str | None = Field(default=None, max_length=10_000)
    arguments_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        description="The arguments_sha256 of the call as shown; required to approve.",
    )


class ToolApprovalView(BaseModel):
    """A paused run's approval as chat messages and analyses carry it."""

    id: str
    agent: ApprovalAgent
    status: ApprovalStatus = "pending"
    created_at: str
    expires_at: str
    calls: list[PendingToolCall] = Field(default_factory=list)
    decisions: list[ToolDecision] = Field(default_factory=list)
    decided_by: str | None = None
    decided_at: str | None = None
    error: str | None = Field(default=None, description="Why a resumed run failed.")


class ToolApproval(ToolApprovalView):
    """The stored document: the view plus everything needed to resume the run."""

    user: str
    claimed_by: str | None = None
    claimed_at: str | None = None
    #: Where the run belongs: {"thread_id", "message_id"} or {"analysis_id"}.
    ref: dict[str, str] = Field(default_factory=dict)
    #: Agent-specific context to rebuild the run (request, spec, MCP selections…).
    context: dict[str, Any] = Field(default_factory=dict)
    #: The paused run's pydantic-ai messages (JSON), ending with the pending tool calls.
    messages: list[dict[str, Any]] = Field(default_factory=list)
    #: Model requests and tool steps the run used before it paused (budgets continue).
    requests: int = 0
    steps: int = 0
    model: str | None = None

    def view(self) -> ToolApprovalView:
        return ToolApprovalView.model_validate(
            self.model_dump(include=set(ToolApprovalView.model_fields))
        )

    def expired(self, at: datetime.datetime | None = None) -> bool:
        expires = parse_iso(self.expires_at)
        return expires is None or (at or now()) >= expires


class ApprovalNotFound(LookupError):
    pass


class ArgumentsMismatch(ValueError):
    """An approval does not match the exact arguments the tool would run with."""

    status_code = 409


class ApprovalBusy(Exception):
    """The approval cannot be decided: it is being resumed, already decided, cancelled or
    failed (``state``)."""

    status_code = 409

    def __init__(self, state: str, by: str | None = None) -> None:
        super().__init__(f"the tool approval is {state}" + (f" (claimed by {by})" if by else ""))
        self.state = state
        self.by = by


class ApprovalExpired(Exception):
    status_code = 410

    def __init__(self) -> None:
        super().__init__("the tool approval expired; ask again")


# --------------------------------------------------------------------------- redaction


def redact_arguments(value: Any, scrub: Callable[[str], str], max_chars: int) -> tuple[Any, bool]:
    """``value`` for display: secret-named fields masked, strings scrubbed, the whole cut
    to about ``max_chars`` characters of JSON. Returns (value, truncated)."""
    from autoair import secret_mask

    budget = [max(0, max_chars)]
    truncated = [False]

    def take(text: str) -> str:
        text = scrub(secret_mask.mask(text))
        if len(text) > budget[0]:
            truncated[0] = True
            text = text[: budget[0]] + "…"
        budget[0] = max(0, budget[0] - len(text))
        return text

    def walk(item: Any, depth: int) -> Any:
        if depth > 12:
            truncated[0] = True
            return "…"
        if isinstance(item, str):
            return take(item)
        if isinstance(item, dict):
            out: dict[str, Any] = {}
            for key, val in list(item.items())[:200]:
                name = str(key)[:200]
                if budget[0] <= 0:
                    truncated[0] = True
                    break
                out[name] = MASK if _SECRET_KEY.search(name) else walk(val, depth + 1)
            if len(item) > 200:
                truncated[0] = True
            return out
        if isinstance(item, (list, tuple)):
            items = []
            for val in list(item)[:200]:
                if budget[0] <= 0:
                    truncated[0] = True
                    break
                items.append(walk(val, depth + 1))
            if len(item) > 200:
                truncated[0] = True
            return items
        if item is None or isinstance(item, (bool, int, float)):
            budget[0] -= len(str(item))
            return item
        return take(str(item))

    return walk(value, 0), truncated[0]


def parse_arguments(raw: Any) -> Any:
    """Tool call arguments as the model sent them (a JSON string is decoded when valid)."""
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return raw
    return raw


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def arguments_digest(raw: Any) -> str:
    """SHA-256 of a tool call's exact arguments (canonical JSON of the decoded value), the
    value an approval is bound to."""
    return hashlib.sha256(_canonical(parse_arguments(raw)).encode()).hexdigest()


def omitted_chars(raw: Any, shown: Any, scrub: Callable[[str], str]) -> int:
    """About how many characters of the masked arguments a display cut left out."""
    full, _ = redact_arguments(parse_arguments(raw), scrub, FULL_ARGUMENT_CHARS)
    return max(0, len(_canonical(full)) - len(_canonical(shown)))


_NO_CALL = object()


def stored_call_arguments(messages: Sequence[Any], call_id: str) -> Any:
    """The raw arguments of tool call ``call_id`` in a paused run's stored pydantic-ai
    messages (JSON dicts), i.e. what the resumed run executes; ``_NO_CALL`` if absent."""
    found: Any = _NO_CALL
    for message in messages:
        parts = message.get("parts") if isinstance(message, dict) else None
        for part in parts or []:
            if (
                isinstance(part, dict)
                and part.get("part_kind") == "tool-call"
                and part.get("tool_call_id") == call_id
            ):
                found = part.get("args")
    return found


def full_arguments(
    approval: ToolApproval, call_id: str, scrub: Callable[[str], str]
) -> tuple[Any, str]:
    """A pending call's complete arguments for review: secrets masked like the card, but
    not cut (up to :data:`FULL_ARGUMENT_CHARS`), with the digest an approval sends back.
    Raises :class:`ApprovalNotFound` for an unknown call or a closed approval."""
    if approval.status not in OPEN_STATUSES or call_id not in {c.id for c in approval.calls}:
        raise ApprovalNotFound(call_id)
    raw = stored_call_arguments(approval.messages, call_id)
    if raw is _NO_CALL:
        raise ApprovalNotFound(call_id)
    value, _ = redact_arguments(parse_arguments(raw), scrub, FULL_ARGUMENT_CHARS)
    return value, arguments_digest(raw)


def check_bound_arguments(approval: ToolApproval, decisions: Sequence[ToolDecision]) -> None:
    """Every approved call must carry the digest the user was shown, and the stored call
    (what the resumed run executes) must still have exactly those arguments; raises
    :class:`ArgumentsMismatch` otherwise. Denials need no digest (nothing runs)."""
    calls = {c.id: c for c in approval.calls}
    for decision in decisions:
        if not decision.approved:
            continue
        call = calls[decision.id]
        if call.arguments_sha256 is None:
            raise ArgumentsMismatch(
                f"the call {call.tool} cannot be verified (it was paused by an older "
                "version); deny it and ask again"
            )
        if decision.arguments_sha256 != call.arguments_sha256:
            raise ArgumentsMismatch(
                f"the approval of {call.tool} does not match its arguments (reload the "
                "approval and review them again)"
            )
        raw = stored_call_arguments(approval.messages, call.id)
        if raw is _NO_CALL or arguments_digest(raw) != call.arguments_sha256:
            raise ArgumentsMismatch(
                f"the stored arguments of {call.tool} changed after they were shown; "
                "the run is not resumed"
            )


# --------------------------------------------------------------------------- store


def _user_key(user: str) -> str:
    return hashlib.sha256(user.encode()).hexdigest()[:24]


class ApprovalStore:
    """Approvals per user on a :class:`DocumentStore` (key ``<user hash>.<id>``)."""

    def __init__(self, documents: DocumentStore, settings: ToolApprovalSettings) -> None:
        self.documents = documents
        self.settings = settings
        self._lock = threading.Lock()

    def _key(self, user: str, approval_id: str) -> str:
        if not approval_id.isalnum() or len(approval_id) > 40:
            raise ApprovalNotFound(approval_id)
        return f"{_user_key(user)}.{approval_id}"

    @staticmethod
    def _parse(data: bytes, user: str, approval_id: str) -> ToolApproval:
        try:
            approval = ToolApproval.model_validate_json(data)
        except (ValidationError, ValueError) as exc:
            raise ApprovalNotFound(approval_id) from exc
        if approval.user != user:
            raise ApprovalNotFound(approval_id)
        return approval

    @staticmethod
    def _dump(approval: ToolApproval) -> bytes:
        return json.dumps(approval.model_dump(mode="json", exclude_none=True)).encode()

    def _settle(self, approval: ToolApproval, at: datetime.datetime) -> bool:
        """Apply time-based transitions (expired / failed claim); True if changed."""
        if approval.status == "pending" and approval.expired(at):
            approval.status = "expired"
            return True
        if approval.status == "resuming":
            from autoair.ai.assistants.airflow_actions import claim_expired

            if claim_expired(approval.claimed_at, self.settings.claim_timeout_seconds, at):
                approval.status = "failed"
                approval.error = "the replica resuming the run stopped before it finished"
                return True
        return False

    def get(self, user: str, approval_id: str) -> ToolApproval:
        doc = self.documents.get(self._key(user, approval_id))
        if doc is None:
            raise ApprovalNotFound(approval_id)
        approval = self._parse(doc.data, user, approval_id)
        self._settle(approval, now())
        return approval

    def create(self, approval: ToolApproval) -> ToolApproval:
        """Store a new pending approval (after pruning the user's old ones)."""
        self.prune(approval.user)
        with self._lock:
            self.documents.put_if_absent(
                self._key(approval.user, approval.id), self._dump(approval)
            )
        return approval

    def mutate(
        self, user: str, approval_id: str, fn: Callable[[ToolApproval], None]
    ) -> ToolApproval:
        """Apply ``fn`` to the stored approval and write it back with compare-and-set on the
        document version (re-read and re-applied on a conflict); ``fn`` may raise to abort."""
        key = self._key(user, approval_id)
        for _ in range(_MUTATE_ATTEMPTS):
            doc = self.documents.get(key)
            if doc is None:
                raise ApprovalNotFound(approval_id)
            approval = self._parse(doc.data, user, approval_id)
            fn(approval)
            data = self._dump(approval)
            if data == doc.data:
                return approval
            try:
                self.documents.put(key, data, expected_version=doc.version)
            except DocumentConflict:
                continue
            return approval
        raise DocumentConflict(f"tool approval {approval_id} keeps changing; try again")

    def claim(
        self,
        user: str,
        approval_id: str,
        decisions: Sequence[ToolDecision],
        *,
        by: str | None = None,
        at: datetime.datetime | None = None,
    ) -> ToolApproval:
        """Record the decisions and mark the approval ``resuming`` for this replica.

        Raises :class:`ApprovalExpired` (it is then stored as expired),
        :class:`ApprovalBusy` when it is not pending, :class:`ArgumentsMismatch` when an
        approval is not bound to the exact stored arguments (:func:`check_bound_arguments`),
        ``ValueError`` when the decisions do not cover exactly the pending calls and
        :class:`ApprovalNotFound`."""
        current = at or now()
        claimer = by or replica_id()
        expired = [False]

        def take(approval: ToolApproval) -> None:
            expired[0] = False
            self._settle(approval, current)
            if approval.status == "expired":
                expired[0] = True
                return
            if approval.status != "pending":
                raise ApprovalBusy(approval.status, approval.claimed_by)
            pending = {c.id for c in approval.calls}
            given = [d.id for d in decisions]
            if len(set(given)) != len(given) or set(given) != pending:
                raise ValueError("decide every pending tool call exactly once")
            check_bound_arguments(approval, decisions)
            approval.status = "resuming"
            approval.claimed_by = claimer
            approval.claimed_at = iso(current)
            approval.decisions = list(decisions)
            approval.decided_by = user
            approval.decided_at = iso(current)

        approval = self.mutate(user, approval_id, take)
        if expired[0]:
            raise ApprovalExpired()
        return approval

    def release(self, user: str, approval_id: str, by: str) -> None:
        """Undo this replica's claim when the run could not start (nothing ran)."""

        def undo(approval: ToolApproval) -> None:
            if approval.status == "resuming" and approval.claimed_by == by:
                approval.status = "pending"
                approval.claimed_by = approval.claimed_at = None
                approval.decisions = []
                approval.decided_by = approval.decided_at = None

        try:
            self.mutate(user, approval_id, undo)
        except ApprovalNotFound:
            return

    def finish(
        self,
        user: str,
        approval_id: str,
        status: Literal["resolved", "failed"],
        *,
        error: str | None = None,
    ) -> ToolApproval:
        """Record the outcome of a claimed approval (the resumed run finished or failed).
        The run's messages are dropped: they now live with the thread / analysis."""

        def record(approval: ToolApproval) -> None:
            approval.status = status
            approval.error = error[:500] if error else None
            approval.messages = []
            approval.context = {}

        return self.mutate(user, approval_id, record)

    def cancel(self, user: str, approval_id: str) -> ToolApproval | None:
        """Cancel a pending approval (the conversation moved on). Raises
        :class:`ApprovalBusy` while it is being resumed; ``None`` when it does not exist."""

        def drop(approval: ToolApproval) -> None:
            self._settle(approval, now())
            if approval.status == "resuming":
                raise ApprovalBusy("resuming", approval.claimed_by)
            if approval.status == "pending":
                approval.status = "cancelled"
                approval.messages = []
                approval.context = {}

        try:
            return self.mutate(user, approval_id, drop)
        except ApprovalNotFound:
            return None

    def _all(self, user: str) -> list[tuple[str, ToolApproval]]:
        prefix = f"{_user_key(user)}."
        out: list[tuple[str, ToolApproval]] = []
        keys = self.documents.keys()
        for key in keys:
            if not key.startswith(prefix):
                continue
            doc = self.documents.get(key)
            if doc is None:
                continue
            try:
                approval = ToolApproval.model_validate_json(doc.data)
            except (ValidationError, ValueError):
                continue
            if approval.user == user:
                out.append((key, approval))
        return out

    def pending(self, user: str, limit: int = 50) -> list[ToolApprovalView]:
        """The user's open approvals (newest first)."""
        at = now()
        out = []
        for _, approval in self._all(user):
            self._settle(approval, at)
            if approval.status in OPEN_STATUSES:
                out.append(approval.view())
        out.sort(key=lambda a: a.created_at, reverse=True)
        return out[:limit]

    def prune(self, user: str) -> None:
        """Delete the user's closed approvals past retention and cancel the oldest pending
        ones beyond ``max_pending_per_user`` (making room for one more)."""
        at = now()
        keep = datetime.timedelta(hours=self.settings.retention_hours)
        open_items: list[ToolApproval] = []
        for key, approval in self._all(user):
            self._settle(approval, at)
            if approval.status in OPEN_STATUSES:
                if approval.status == "pending":
                    open_items.append(approval)
                continue
            created = parse_iso(approval.created_at) or at
            if at - created > keep:
                self.documents.delete(key)
        open_items.sort(key=lambda a: a.created_at)
        excess = len(open_items) - (self.settings.max_pending_per_user - 1)
        for approval in open_items[: max(0, excess)]:
            try:
                self.cancel(user, approval.id)
            except ApprovalBusy:
                continue


def refresh_views(store: ApprovalStore, user: str, views: Iterable[ToolApprovalView]) -> None:
    """Bring the open approval views a thread or analysis carries up to date in place (they
    are snapshots; the approval may have expired, failed or been decided elsewhere)."""
    for view in views:
        if view.status not in OPEN_STATUSES:
            continue
        try:
            current = store.get(user, view.id)
        except ApprovalNotFound:
            view.status = "expired"
            continue
        for name in ("status", "decisions", "decided_by", "decided_at", "error"):
            setattr(view, name, getattr(current, name))


_STORES: dict[str, ApprovalStore] = {}
_STORES_LOCK = threading.Lock()


def approval_store_for(services: Any) -> ApprovalStore:
    from autoair.ai.config import ai_settings_of
    from autoair.persistence import document_store, is_sql

    settings = services.settings
    cfg = ai_settings_of(settings).approvals
    url = cfg.store_url or str(settings.state_dir / "ai-approvals")
    backend = settings.database.model_dump_json() if is_sql(settings) else "file"
    identity = f"{backend}:{url}:{cfg.model_dump_json()}"
    key = hashlib.sha256(identity.encode()).hexdigest()
    with _STORES_LOCK:
        store = _STORES.get(key)
        if store is None:
            store = _STORES[key] = ApprovalStore(
                document_store(settings, "ai_tool_approvals", file_url=url), cfg
            )
        return store


def new_approval(
    *,
    user: str,
    agent: ApprovalAgent,
    calls: list[PendingToolCall],
    settings: ToolApprovalSettings,
    **fields: Any,
) -> ToolApproval:
    created = now()
    return ToolApproval(
        id=new_id(),
        user=user,
        agent=agent,
        created_at=iso(created),
        expires_at=iso(created + datetime.timedelta(seconds=settings.ttl_seconds)),
        calls=calls,
        **fields,
    )


__all__ = [
    "ALWAYS_APPROVE",
    "OPEN_STATUSES",
    "ApprovalBusy",
    "ApprovalExpired",
    "ApprovalNotFound",
    "ApprovalStatus",
    "ApprovalStore",
    "ArgumentsMismatch",
    "PendingToolCall",
    "ToolApproval",
    "ToolApprovalView",
    "ToolDecision",
    "approval_rule",
    "approval_store_for",
    "arguments_digest",
    "check_bound_arguments",
    "full_arguments",
    "gated_tools",
    "needs_approval",
    "new_approval",
    "omitted_chars",
    "parse_arguments",
    "redact_arguments",
    "refresh_views",
    "stored_call_arguments",
    "withdraw_gated",
]
