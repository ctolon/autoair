"""Project AI rules: admin-managed Markdown injected into every authoring/writing prompt.

Rules are kept per scope - ``global`` and ``team:<name>`` - in the ``ai_rules`` collection
of the persistence layer. The prompts module asks :func:`effective_rules` (through the
provider :func:`install` registers) for the global rules plus those of the caller's team
(``settings.team``), bounded by ``ai.rules.max_chars`` and cached for
``ai.rules.cache_seconds``.
"""

from __future__ import annotations

import json
import re
import threading
import time
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from autoair.ai.assistants.threads import now_iso
from autoair.persistence.documents import DocumentStore

SCOPE_RE = re.compile(r"^(global|team:[A-Za-z0-9][A-Za-z0-9_.-]{0,99})$")
MAX_RULES_CHARS = 50_000


class ProjectRules(BaseModel):
    scope: str = Field(description="global or team:<name>.")
    content: str = Field(default="", max_length=MAX_RULES_CHARS)
    enabled: bool = True
    updated_by: str | None = None
    updated_at: str | None = None


class RulesUpdate(BaseModel):
    content: str = Field(max_length=MAX_RULES_CHARS)
    enabled: bool = True


class RulesStore:
    def __init__(self, documents: DocumentStore) -> None:
        self.documents = documents

    @staticmethod
    def check(scope: str) -> str:
        if not SCOPE_RE.fullmatch(scope):
            raise ValueError("scope must be 'global' or 'team:<name>'")
        return scope

    def get(self, scope: str) -> ProjectRules | None:
        doc = self.documents.get(self.check(scope))
        if doc is None:
            return None
        try:
            return ProjectRules.model_validate_json(doc.data)
        except (ValidationError, ValueError):
            return None

    def all(self) -> list[ProjectRules]:
        out = []
        keys = self.documents.keys()
        for key in keys:
            if SCOPE_RE.fullmatch(key):
                rules = self.get(key)
                if rules is not None:
                    out.append(rules)
        return sorted(out, key=lambda r: (r.scope != "global", r.scope))

    def put(self, scope: str, update: RulesUpdate, user: str | None) -> ProjectRules:
        rules = ProjectRules(
            scope=self.check(scope),
            content=update.content.strip(),
            enabled=update.enabled,
            updated_by=user,
            updated_at=now_iso(),
        )
        self.documents.put(scope, json.dumps(rules.model_dump(mode="json")).encode())
        _CACHE.clear()
        return rules

    def delete(self, scope: str) -> bool:
        removed = self.documents.delete(self.check(scope))
        _CACHE.clear()
        return removed


_STORES: dict[str, RulesStore] = {}
_CACHE: dict[tuple[str, str | None], tuple[float, str]] = {}
_LOCK = threading.Lock()


def rules_store_for(services: Any) -> RulesStore:
    from autoair.ai.config import ai_settings_of
    from autoair.persistence import document_store, is_sql

    settings = services.settings
    url = ai_settings_of(settings).rules.store_url or str(settings.state_dir / "ai-rules")
    key = f"{'sql' if is_sql(settings) else 'file'}:{url}"
    with _LOCK:
        store = _STORES.get(key)
        if store is None:
            store = _STORES[key] = RulesStore(document_store(settings, "ai_rules", file_url=url))
        return store


def effective_rules(services: Any, team: str | None = None) -> str:
    """Global + team rules as Markdown ('' when none / disabled)."""
    from autoair.ai.config import ai_settings_of

    cfg = ai_settings_of(services.settings).rules
    if not cfg.enabled:
        return ""
    team = team or getattr(services.settings, "team", None)
    store = rules_store_for(services)
    cache_key = (str(id(store)), team)
    now = time.monotonic()
    cached = _CACHE.get(cache_key)
    if cached is not None and cached[0] > now:
        return cached[1]
    blocks: list[str] = []
    scopes = ["global", *([f"team:{team}"] if team else [])]
    for scope in scopes:
        try:
            rules = store.get(scope)
        except ValueError:
            continue
        if rules is not None and rules.enabled and rules.content.strip():
            label = "All teams" if scope == "global" else f"Team {scope[5:]}"
            blocks.append(f"### {label}\n{rules.content.strip()}")
    text = "\n\n".join(blocks)
    if len(text) > cfg.max_chars:
        text = text[: cfg.max_chars] + "\n… [rules truncated]"
    _CACHE[cache_key] = (now + cfg.cache_seconds, text)
    return text


def install(services: Any) -> None:
    """Make the prompts module inject this installation's rules."""
    from autoair.ai import prompts

    def provider(team: str | None) -> str:
        return effective_rules(services, team)

    prompts.set_rules_provider(provider)


__all__ = [
    "ProjectRules",
    "RulesStore",
    "RulesUpdate",
    "effective_rules",
    "install",
    "rules_store_for",
]
