"""Deploy governance: the context every commit, pull request and audit record carries.

Every change AutoAir writes (deploy, delete, promotion) is labelled with *who* made it and
*what* was checked: the user's Airflow identity, AutoAir version, DAG, environment, the spec
etag before/after, the validation summary, approvals and a link back to the DAG. In git it
becomes

* a mandatory **context block** appended to the commit message and the pull request
  description (users add their own text above it; they cannot remove it), and
* **git trailers** (``X-AutoAir-User``, ``Approved-by``, ``Co-authored-by`` …) in the last
  paragraph of the commit message, machine-readable with ``git interpret-trailers``.

The commit *author* is either the service identity (the token's owner, the user named in the
trailers) or the user (``storage.git.commit_author``).
"""

from __future__ import annotations

import re
import string
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from autoair import __version__

CONTEXT_HEADER = "AutoAir deploy context"
_SAFE_LINE = re.compile(r"[\r\n\x00-\x08\x0b-\x1f\x7f]+")
# Placeholders every commit/PR template may use (besides each template's own).
CONTEXT_PLACEHOLDERS = frozenset(
    {
        "user",
        "user_name",
        "user_email",
        "autoair_version",
        "etag_before",
        "etag_after",
        "validation",
        "approvals",
        "link",
        "context",
        "description",
        "target",
    }
)
CommitAuthor = Literal["auto", "service", "user"]


def one_line(value: object, limit: int = 300) -> str:
    """``value`` on one line (no CR/LF/control characters: a user-controlled value must not
    forge extra trailers or context lines), shortened to ``limit`` characters."""
    text = _SAFE_LINE.sub(" ", str(value)).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


@dataclass(frozen=True)
class UserIdentity:
    """The caller as Airflow knows them (username; display name and e-mail when the token or
    the auth manager's user carries them)."""

    username: str | None
    display_name: str | None = None
    email: str | None = None

    @property
    def label(self) -> str:
        name = self.username or "unknown"
        extra = " ".join(
            p for p in (self.display_name, f"<{self.email}>" if self.email else None) if p
        )
        return f"{name} ({extra})" if extra and self.display_name != name else name

    def git_author(self) -> str | None:
        """``Name <email>`` for a commit authored as the user (``None``: anonymous)."""
        if not self.username and not self.email:
            return None
        name = one_line(self.display_name or self.username or self.email or "", 120)
        if self.email:
            return f"{name} <{one_line(self.email, 200)}>"
        return name


@dataclass(frozen=True)
class ValidationSummary:
    ok: bool
    errors: int = 0
    warnings: int = 0
    infos: int = 0
    import_check: bool | None = None

    @classmethod
    def of(cls, report: Any) -> ValidationSummary:
        problems = list(getattr(report, "problems", []) or [])

        def count(severity: str) -> int:
            return sum(1 for p in problems if getattr(p, "severity", None) == severity)

        check = getattr(report, "import_check", None)
        return cls(
            ok=bool(getattr(report, "ok", False)),
            errors=count("error"),
            warnings=count("warning"),
            infos=count("info"),
            import_check=None if check is None else bool(getattr(check, "ok", False)),
        )

    def text(self) -> str:
        state = "passed" if self.ok else "FAILED"
        parts = [f"{self.errors} errors", f"{self.warnings} warnings", f"{self.infos} infos"]
        if self.import_check is not None:
            parts.append("import check " + ("ok" if self.import_check else "failed"))
        else:
            parts.append("no import check")
        return f"{state} ({', '.join(parts)})"


@dataclass
class DeployContext:
    """Everything a commit / pull request / audit record says about one change."""

    action: Literal["deploy", "delete", "promote"]
    dag_id: str
    user: UserIdentity
    environment: str | None = None
    etag_before: str | None = None
    etag_after: str | None = None
    validation: ValidationSummary | None = None
    approvals: list[str] = field(default_factory=list)
    reviewers: list[str] = field(default_factory=list)
    proposed_by: str | None = None
    link: str | None = None
    target: str | None = None
    git_mode: str | None = None
    description: str | None = None
    autoair_version: str = __version__
    extra: dict[str, str] = field(default_factory=dict)

    # ----------------------------------------------------------------- rendering
    def lines(self) -> list[str]:
        """The context block's ``Key: value`` lines."""
        env = f" (environment {one_line(self.environment, 100)})" if self.environment else ""
        out = [
            f"Action: {self.action}",
            f"DAG: {one_line(self.dag_id)}{env}",
            f"User: {one_line(self.user.label)}",
        ]
        if self.etag_before or self.etag_after:
            out.append(
                f"Spec version: {_short(self.etag_before) or '(new)'} -> "
                f"{_short(self.etag_after) or '(deleted)'}"
            )
        if self.validation is not None:
            out.append(f"Validation: {self.validation.text()}")
        if self.approvals:
            out.append("Approved by: " + ", ".join(one_line(a, 100) for a in self.approvals))
        if self.reviewers:
            out.append("Reviewed by: " + ", ".join(one_line(r, 100) for r in self.reviewers))
        if self.proposed_by:
            out.append(f"Proposed by: {one_line(self.proposed_by, 100)}")
        if self.target:
            mode = f" ({self.git_mode})" if self.git_mode else ""
            out.append(f"Target: {one_line(self.target)}{mode}")
        for key, value in self.extra.items():
            out.append(f"{one_line(key, 60)}: {one_line(value)}")
        out.append(f"AutoAir: {self.autoair_version}")
        if self.link:
            out.append(f"Link: {one_line(self.link, 500)}")
        return out

    def block(self) -> str:
        """The mandatory context block (plain text: readable in commits and Markdown)."""
        return "\n".join([f"--- {CONTEXT_HEADER} ---", *self.lines()])

    def trailers(self, *, co_author: bool) -> list[str]:
        """Git trailers. ``co_author``: the commit is authored by the service identity, so
        the user is credited with ``Co-authored-by`` (when an e-mail is known)."""
        out = [f"X-AutoAir-User: {one_line(self.user.username or 'unknown', 200)}"]
        if self.user.display_name:
            out.append(f"X-AutoAir-User-Name: {one_line(self.user.display_name, 200)}")
        out.append(f"X-AutoAir-Dag: {one_line(self.dag_id)}")
        if self.environment:
            out.append(f"X-AutoAir-Environment: {one_line(self.environment, 100)}")
        if self.etag_after:
            out.append(f"X-AutoAir-Spec-Etag: {one_line(self.etag_after, 128)}")
        if self.etag_before:
            out.append(f"X-AutoAir-Base-Etag: {one_line(self.etag_before, 128)}")
        if self.validation is not None:
            out.append(f"X-AutoAir-Validation: {'passed' if self.validation.ok else 'failed'}")
        out.append(f"X-AutoAir-Version: {self.autoair_version}")
        out.extend(f"Approved-by: {one_line(a, 200)}" for a in self.approvals)
        if co_author and self.user.email:
            author = self.user.git_author()
            if author:
                out.append(f"Co-authored-by: {author}")
        return out

    def values(self, **more: str | None) -> dict[str, str]:
        """Template placeholders (``{user}``, ``{context}`` …)."""
        base = {
            "user": self.user.username or "",
            "user_name": self.user.display_name or self.user.username or "",
            "user_email": self.user.email or "",
            "autoair_version": self.autoair_version,
            "etag_before": self.etag_before or "",
            "etag_after": self.etag_after or "",
            "validation": self.validation.text() if self.validation else "",
            "approvals": ", ".join(self.approvals),
            "link": self.link or "",
            "context": self.block(),
            "description": (self.description or "").strip(),
            "target": self.target or "",
        }
        base.update({k: v or "" for k, v in more.items()})
        return base

    def audit(self) -> dict[str, Any]:
        """The context as audit details."""
        return {
            "user_name": self.user.display_name,
            "user_email": self.user.email,
            "autoair_version": self.autoair_version,
            "environment": self.environment,
            "etag_before": self.etag_before,
            "etag_after": self.etag_after,
            "validation": (
                None
                if self.validation is None
                else {
                    "ok": self.validation.ok,
                    "errors": self.validation.errors,
                    "warnings": self.validation.warnings,
                    "infos": self.validation.infos,
                    "import_check": self.validation.import_check,
                }
            ),
            "approved_by": list(self.approvals),
            "reviewed_by": list(self.reviewers),
            "proposed_by": self.proposed_by,
            "link": self.link,
            "target": self.target,
            "git_mode": self.git_mode,
        }


def _short(etag: str | None) -> str:
    return (etag or "")[:12]


class _Fmt(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return ""


def render(template: str, values: Mapping[str, str]) -> str:
    """``template`` with ``values`` (unknown placeholders render empty)."""
    return string.Formatter().vformat(template, (), _Fmt(values))


def _tidy(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def commit_message(
    base: str, context: DeployContext, *, co_author: bool, template_has_context: bool = False
) -> str:
    """``base`` (the user's message or the rendered template) + the context block (unless the
    template already placed it) + the trailers paragraph."""
    parts = [base.strip()]
    if not template_has_context:
        parts.append(context.block())
    parts.append("\n".join(context.trailers(co_author=co_author)))
    return _tidy("\n\n".join(p for p in parts if p))


def pr_body(template: str, context: DeployContext, values: Mapping[str, str]) -> str:
    """The pull request description: the configured template, with the context block
    appended when the template does not place ``{context}`` itself."""
    body = render(template, values)
    if "{context}" not in template:
        body = f"{body}\n\n{context.block()}"
    return _tidy(body)


def dag_link(public_url: str | None, dag_id: str, *, mode: str) -> str | None:
    """Link back to the DAG in the AutoAir UI (``server.public_url``)."""
    if not public_url:
        return None
    from urllib.parse import quote

    base = public_url.rstrip("/")
    if mode == "plugin":
        return f"{base}#dag={quote(dag_id, safe='')}"
    if not base.endswith("/ui"):
        base += "/ui"
    return f"{base}/?dag={quote(dag_id, safe='')}"


def identity_from_claims(claims: Mapping[str, Any], username: str | None) -> UserIdentity:
    """Display name and e-mail from standard JWT claims (OIDC ``name``/``given_name``…)."""
    name = claims.get("name")
    if not isinstance(name, str) or not name.strip():
        given, family = claims.get("given_name"), claims.get("family_name")
        name = " ".join(p for p in (given, family) if isinstance(p, str) and p) or None
    email = claims.get("email")
    return UserIdentity(
        username=username,
        display_name=one_line(name, 200) if isinstance(name, str) and name else None,
        email=one_line(email, 200) if isinstance(email, str) and "@" in email else None,
    )


def identity_from_user(user: Any, username: str | None) -> UserIdentity:
    """Display name and e-mail of an auth-manager user (FAB: first/last name, email)."""
    first = getattr(user, "first_name", None)
    last = getattr(user, "last_name", None)
    name = " ".join(p for p in (first, last) if isinstance(p, str) and p) or None
    email = getattr(user, "email", None)
    return UserIdentity(
        username=username,
        display_name=one_line(name, 200) if name else None,
        email=one_line(email, 200) if isinstance(email, str) and "@" in email else None,
    )


def merge_identity(*identities: UserIdentity) -> UserIdentity:
    username = next((i.username for i in identities if i.username), None)
    display = next((i.display_name for i in identities if i.display_name), None)
    email = next((i.email for i in identities if i.email), None)
    return UserIdentity(username=username, display_name=display, email=email)


def reviewers_of(reviews: Iterable[Any], etag: str, exclude: str | None = None) -> list[str]:
    """Users who reviewed version ``etag`` (any verdict), excluding the author."""
    seen: dict[str, None] = {}
    for review in reviews:
        user = getattr(review, "user", None)
        if user and getattr(review, "etag", None) == etag and user != exclude:
            seen.setdefault(str(user), None)
    return list(seen)


__all__ = [
    "CONTEXT_PLACEHOLDERS",
    "DeployContext",
    "UserIdentity",
    "ValidationSummary",
    "commit_message",
    "dag_link",
    "identity_from_claims",
    "identity_from_user",
    "merge_identity",
    "one_line",
    "pr_body",
    "render",
    "reviewers_of",
]
