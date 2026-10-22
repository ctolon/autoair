"""Read-only tools of the admin assistant.

Configuration (secrets always masked; the effective values only with ``admin:config`` and
``AUTOAIR_EXPOSE_CONFIG``), the settings reference, feature flags, IAM (roles, permissions,
who holds them, why a user cannot do something), replicas and the state database, AI
usage, the audit trail, the catalog/provider inventory and which DAGs use an operator,
deploy targets, the proposal queue and health. The only "write" is
``propose_config_snippet``: a YAML/env snippet returned as text for the admin to apply.
"""

from __future__ import annotations

import datetime
import fnmatch
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, Field

from autoair.ai.assistants.dev_tools import TurnState, memory_tools
from autoair.ai.assistants.threads import Proposal
from autoair.ai.tools import AgentTool
from autoair.iam import model as P

#: What common actions need: permissions, and the feature flag that must be on.
ACTIONS: dict[str, tuple[tuple[str, ...], str | None]] = {
    "deploy": ((P.SPEC_WRITE, P.DEPLOY), "deploy"),
    "save": ((P.SPEC_WRITE,), "deploy"),
    "delete": ((P.SPEC_DELETE,), "deploy"),
    "promote": ((P.PROMOTE,), "deploy"),
    "direct_push": ((P.DEPLOY, P.DEPLOY_DIRECT_PUSH), "deploy"),
    "review": ((P.REVIEW,), None),
    "trigger": ((P.RUN_TRIGGER,), "runs"),
    "clear": ((P.RUN_MANAGE,), "runs"),
    "backfill": ((P.RUN_MANAGE,), "runs"),
    "test": ((P.RUN_TEST, P.CODE_EXECUTE), "test_runs"),
    "import": ((P.SPEC_WRITE, P.CODE_EXECUTE), "imports"),
    "use_ai": ((P.AI_USE,), "ai"),
    "admin_ai": ((P.AI_ADMIN,), "ai"),
    "templates": ((P.TEMPLATE_WRITE,), "templates"),
    "admin_panel": ((P.ADMIN_PANEL,), None),
    "configure": ((P.ADMIN_CONFIG,), None),
    "read": ((P.BUILDER_USE, P.SPEC_READ), None),
}


class NoInput(BaseModel):
    pass


class ConfigInput(BaseModel):
    query: str | None = Field(
        default=None, max_length=200, description="Words or a key prefix, e.g. 'storage.git'."
    )
    limit: int = Field(default=40, ge=1, le=200)


class PermissionInput(BaseModel):
    permission: str = Field(description="AutoAir permission, e.g. deploy or spec:write.")


class AccessInput(BaseModel):
    user: str = Field(max_length=200)
    action: str = Field(
        description="What the user tries: " + ", ".join(sorted(ACTIONS)) + " (or a permission)."
    )


class UsageInput(BaseModel):
    days: int = Field(default=30, ge=1, le=3660)


class AuditInput(BaseModel):
    user: str | None = None
    action: str | None = Field(default=None, description="e.g. deploy, permission_denied.")
    dag_id: str | None = None
    since: str | None = Field(default=None, description="ISO date/time (inclusive).")
    until: str | None = Field(default=None, description="ISO date/time (exclusive).")
    days: int | None = Field(default=None, ge=1, le=3660, description="Shortcut: last N days.")
    limit: int = Field(default=50, ge=1, le=300)


class OperatorUseInput(BaseModel):
    operator: str = Field(
        description="Operator class name or fnmatch pattern, e.g. BashOperator or "
        "*.providers.amazon.*"
    )


class InventoryInput(BaseModel):
    query: str | None = Field(default=None, max_length=200, description="Filter by name.")
    limit: int = Field(default=60, ge=1, le=500)


class SnippetInput(BaseModel):
    title: str = Field(max_length=200)
    format: Literal["yaml", "env", "json"] = "yaml"
    content: str = Field(min_length=1, max_length=20_000)
    explanation: str = Field(default="", max_length=4_000)


def _iso(value: str | None) -> str | None:
    if not value:
        return None
    try:
        if len(value) == 10:
            parsed = datetime.datetime.fromisoformat(value).replace(tzinfo=datetime.timezone.utc)
        else:
            parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"not an ISO date/time: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    return parsed.astimezone(datetime.timezone.utc).isoformat()


def _matches(text: str, query: str | None) -> bool:
    if not query:
        return True
    words = query.lower().split()
    hay = text.lower()
    return all(w in hay for w in words)


def admin_tools(
    services: Any,
    *,
    state: TurnState,
    can: Callable[[str], bool],
    user: str | None,
    memory: bool = True,
) -> list[AgentTool]:
    from autoair.config_reference import MASK, iter_options, masked, value_at
    from autoair.iam.deps import iam_settings
    from autoair.iam.model import (
        PERMISSION_INFO,
        ROLE_DESCRIPTIONS,
        permissions_of,
        role_table,
    )

    settings = services.settings
    src = state.sources

    def settings_reference(args: ConfigInput) -> Any:
        out = []
        for opt in iter_options(type(settings)):
            text = f"{opt.key} {opt.env} {opt.description}"
            if not (_matches(text, args.query) or opt.key.startswith(args.query or "\0")):
                continue
            n = src.add("config", opt.key, f"setting:{opt.key}", opt.description)
            out.append(
                {
                    "cite": n,
                    "key": opt.key,
                    "env": opt.env,
                    "type": opt.type,
                    "default": opt.default_text,
                    "description": opt.description,
                    "secret": opt.secret,
                }
            )
            if len(out) >= args.limit:
                break
        return out or "no matching settings"

    def configuration(args: ConfigInput) -> Any:
        expose = settings.expose_config
        if expose == "false":
            return (
                "The effective configuration is not exposed (AUTOAIR_EXPOSE_CONFIG=false). "
                "Use search_settings_reference for keys and defaults."
            )
        out = []
        for opt in iter_options(type(settings)):
            if opt.secret and expose == "non-sensitive-only":
                continue
            text = f"{opt.key} {opt.env}"
            if not (_matches(text, args.query) or opt.key.startswith(args.query or "\0")):
                continue
            value = value_at(settings, opt.key)
            if opt.secret:
                shown: Any = MASK if value not in (None, "", {}, []) else None
            else:
                shown = masked(value)
            n = src.add("config", opt.key, f"setting:{opt.key}")
            out.append(
                {
                    "cite": n,
                    "key": opt.key,
                    "value": shown,
                    "default": opt.default_text,
                    "source": settings.origin_of(opt.key),
                }
            )
            if len(out) >= args.limit:
                break
        return out or "no matching settings"

    def features(_: NoInput) -> Any:
        n = src.add("config", "feature flags", "setting:features")
        return {"cite": n, "features": settings.effective_features()}

    def iam_overview(_: NoInput) -> Any:
        iam = iam_settings(services)
        table = role_table(iam.roles)
        n = src.add("config", "IAM roles", "setting:iam")
        return {
            "cite": n,
            "strategies": list(iam.strategies),
            "role_mapping": iam.role_mapping,
            "default_roles": list(iam.default_roles),
            "require_approval": iam.require_approval,
            "direct_push_roles": list(iam.direct_push_roles),
            "roles": {
                name: {
                    "permissions": sorted(perms),
                    "description": ROLE_DESCRIPTIONS.get(name, "custom role"),
                }
                for name, perms in sorted(table.items())
            },
            "permissions": {k: v.description for k, v in PERMISSION_INFO.items()},
        }

    def _seen() -> list[Any]:
        from autoair.admin import seen_users_for

        return seen_users_for(services).all()

    def who_has(args: PermissionInput) -> Any:
        iam = iam_settings(services)
        table = role_table(iam.roles)
        roles = sorted(r for r, perms in table.items() if args.permission in perms)
        users = [
            {"user": u.user, "roles": u.roles, "last_seen": u.last_seen}
            for u in _seen()
            if set(u.roles) & set(roles)
        ]
        return {
            "permission": args.permission,
            "known": args.permission in PERMISSION_INFO,
            "roles": roles,
            "users": users[:200],
            "note": "Users are those seen by AutoAir with the roles they had then.",
        }

    def explain_access(args: AccessInput) -> Any:
        iam = iam_settings(services)
        table = role_table(iam.roles)
        action = args.action.strip().lower().replace(" ", "_")
        if action in ACTIONS:
            needed, feature = ACTIONS[action]
        else:
            needed, feature = (args.action.strip(),), None
        seen = next((u for u in _seen() if u.user == args.user), None)
        roles = list(seen.roles) if seen else []
        held = permissions_of(roles, table)
        missing = [p for p in needed if p not in held]
        grants = {p: sorted(r for r, perms in table.items() if p in perms) for p in missing}
        feature_on = settings.effective_features().get(feature, True) if feature else None
        denials = services.audit.query(limit=20, user=args.user, action="permission_denied")
        refs = []
        for rec in denials:
            refs.append(
                {
                    "cite": src.add(
                        "audit",
                        f"{rec.timestamp[:19]} permission_denied",
                        f"audit:{rec.timestamp}:{rec.action}",
                        str(rec.details.get("permission", "")),
                    ),
                    "timestamp": rec.timestamp,
                    "permission": rec.details.get("permission"),
                    "path": rec.details.get("path"),
                }
            )
        governance: dict[str, Any] = {}
        if action in ("deploy", "direct_push"):
            git = getattr(settings.storage, "git", None)
            governance = {
                "require_approval": iam.require_approval,
                "direct_push_roles": list(iam.direct_push_roles),
                "deploy_target": str(getattr(services.target, "kind", "")),
                "git_mode": getattr(git, "mode", None),
            }
        return {
            "user": args.user,
            "seen": seen is not None,
            "roles": roles,
            "needed": list(needed),
            "missing": missing,
            "roles_granting_missing": grants,
            "feature": feature,
            "feature_enabled": feature_on,
            "governance": governance,
            "recent_denials": refs,
            "note": "Roles are as last seen by AutoAir; with the airflow_permissions or "
            "custom_view strategies they also depend on Airflow's own role assignments.",
        }

    def replicas(_: NoInput) -> Any:
        from autoair.api.routers.admin import _database_status, _replicas

        db = _database_status(services).model_dump(mode="json", exclude_none=True)
        n = src.add("config", "database status", "admin:database")
        return {
            "cite": n,
            "replicas": [r.model_dump(mode="json") for r in _replicas(services)],
            "database": db,
        }

    def ai_usage(args: UsageInput) -> Any:
        from autoair.admin import USAGE_SCAN_LIMIT, aggregate_usage, usage_rows
        from autoair.ai.audit_log import audit_for

        since = (
            datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=args.days)
        ).isoformat()
        log = audit_for(services)
        usage = getattr(log.repository, "usage", None)
        rows = (
            usage_rows(usage(since))
            if callable(usage)
            else aggregate_usage(log.recent(USAGE_SCAN_LIMIT, None, since))
        )
        n = src.add("audit", f"AI usage, last {args.days} days", f"ai-usage:{args.days}d")
        return {"cite": n, "rows": [r.model_dump(mode="json") for r in rows[:100]]}

    def search_audit(args: AuditInput) -> Any:
        since = _iso(args.since)
        if args.days and not since:
            since = (
                datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=args.days)
            ).isoformat()
        records = services.audit.query(
            limit=args.limit,
            user=args.user or None,
            action=args.action or None,
            dag_id=args.dag_id or None,
            since=since,
            until=_iso(args.until),
        )
        out = []
        for rec in records:
            ref = f"audit:{rec.timestamp}:{rec.action}"
            label = f"{rec.timestamp[:19]} {rec.action} {rec.dag_id or ''}".strip()
            out.append(
                {
                    "cite": src.add("audit", label, ref, str(rec.details)[:200]),
                    "timestamp": rec.timestamp,
                    "action": rec.action,
                    "user": rec.user,
                    "dag_id": rec.dag_id,
                    "details": {k: v for k, v in rec.details.items() if k != "diff"},
                }
            )
        return out or "no matching audit records"

    def inventory(args: InventoryInput) -> Any:
        stats = services.catalog.stats()
        operators = []
        for op in services.catalog.summaries():
            text = f"{op.fqn} {op.provider}"
            if _matches(text, args.query):
                operators.append(
                    {
                        "fqn": op.fqn,
                        "provider": op.provider,
                        "deprecated": op.deprecated,
                    }
                )
            if len(operators) >= args.limit:
                break
        n = src.add("source", "operator catalog", "catalog")
        return {
            "cite": n,
            "stats": stats.model_dump(mode="json"),
            "operators": operators,
        }

    def dags_using(args: OperatorUseInput) -> Any:
        from autoair.mcp.store import store_for

        store = store_for(services)
        pattern = args.operator
        out = []
        for stored in store.list():
            spec = store.get(stored.dag_id)
            if spec is None:
                continue
            for node in spec.nodes:
                fqn = getattr(node, "operator", None)
                if not fqn:
                    continue
                short = str(fqn).rsplit(".", 1)[-1]
                if fnmatch.fnmatchcase(str(fqn), pattern) or fnmatch.fnmatchcase(short, pattern):
                    out.append(
                        {
                            "cite": src.add("spec", spec.dag_id, f"spec:{spec.dag_id}"),
                            "dag_id": spec.dag_id,
                            "node_id": node.id,
                            "operator": fqn,
                        }
                    )
        return out or f"no saved AutoAir DAG uses {pattern}"

    def deploy_targets(_: NoInput) -> Any:
        target = services.target
        try:
            status = target.status().model_dump(mode="json", exclude_none=True)
        except Exception as exc:
            status = {"error": str(exc)}
        n = src.add("config", "deploy target", "setting:storage")
        return {
            "cite": n,
            "default": {
                "kind": str(getattr(target, "kind", "")),
                "source": services.storage_source,
                "status": status,
                "settings": masked(settings.storage.model_dump(mode="json")),
            },
            "teams": {
                team: masked(cfg.model_dump(mode="json"))
                for team, cfg in settings.team_storage.items()
            },
            "environments": list(getattr(settings, "environments", []) or []),
        }

    def proposals(_: NoInput) -> Any:
        from autoair.api.routers.admin import proposals_overview

        data = proposals_overview(services).model_dump(mode="json")
        n = src.add("audit", "proposal queue", "admin:proposals")
        return {"cite": n, **data}

    def health(_: NoInput) -> Any:
        checks: dict[str, str] = {}
        try:
            stats = services.catalog.stats()
            checks["catalog"] = f"ok ({stats.operators} operators)"
        except Exception as exc:
            checks["catalog"] = f"error: {exc}"
        try:
            status = services.target.status()
            checks["storage"] = f"ok ({status.kind})" if status.writable else "read-only"
        except Exception as exc:
            checks["storage"] = f"error: {exc}"
        if services.database is not None:
            checks["database"] = "ok" if services.database.ping() else "unreachable"
        if settings.mode != "local":
            from autoair.api.routers.ops import _airflow_health

            checks["airflow"] = _airflow_health(services)
        return {"ready": all(v.startswith("ok") for v in checks.values()), "checks": checks}

    def propose_snippet(args: SnippetInput) -> Any:
        from autoair.ai.rules import scrub_secrets

        content = scrub_secrets(args.content)
        proposal = Proposal(
            kind="config_snippet",
            title=args.title,
            summary=args.explanation,
            format=args.format,
            snippet=content,
        )
        state.proposals.append(proposal)
        return {
            "proposal_id": proposal.id,
            "note": "Shown to the admin as text; nothing was changed.",
        }

    tools = [
        AgentTool(
            "search_settings_reference",
            "AutoAir settings reference: keys, environment variables, types, defaults and "
            "descriptions (no values). Use it to name settings correctly.",
            ConfigInput,
            settings_reference,
        ),
        AgentTool(
            "get_feature_flags",
            "Effective feature flags (features.*).",
            NoInput,
            features,
        ),
        AgentTool(
            "get_iam_overview",
            "IAM: strategies, role mapping, roles with their permissions, governance "
            "(four-eyes approval, direct-push roles), permission descriptions.",
            NoInput,
            iam_overview,
        ),
        AgentTool(
            "who_has_permission",
            "Roles granting a permission and the users seen with those roles.",
            PermissionInput,
            who_has,
        ),
        AgentTool(
            "explain_user_access",
            "Why a user can or cannot do something: needed permissions, the user's roles, "
            "what is missing and which roles grant it, the feature flag, governance and "
            "recent permission denials.",
            AccessInput,
            explain_access,
        ),
        AgentTool(
            "get_replicas_and_database",
            "Live replicas (heartbeats), state backend, schema version, pending migrations "
            "and table sizes.",
            NoInput,
            replicas,
        ),
        AgentTool(
            "get_ai_usage",
            "AI requests, tokens and cost per user and model over the last N days.",
            UsageInput,
            ai_usage,
        ),
        AgentTool(
            "search_audit",
            "Search the audit trail (newest first) by user, action, DAG and time window; "
            "use days=7 for 'last week'.",
            AuditInput,
            search_audit,
        ),
        AgentTool(
            "get_catalog_inventory",
            "Operator catalog statistics and operators/providers (filterable).",
            InventoryInput,
            inventory,
        ),
        AgentTool(
            "find_dags_using_operator",
            "Saved AutoAir DAGs (and nodes) that use an operator (class name or pattern).",
            OperatorUseInput,
            dags_using,
        ),
        AgentTool(
            "get_deploy_targets",
            "Default and per-team deploy targets, their status and settings (masked).",
            NoInput,
            deploy_targets,
        ),
        AgentTool(
            "get_proposals_queue",
            "Pending deploy proposals awaiting review and recent review verdicts.",
            NoInput,
            proposals,
        ),
        AgentTool(
            "get_health",
            "Readiness checks: catalog, deploy target, database, Airflow.",
            NoInput,
            health,
        ),
        AgentTool(
            "propose_config_snippet",
            "Propose a configuration change as a YAML (config file) or env (AUTOAIR_*) "
            "snippet shown to the admin as text. It changes nothing. Placeholders for secrets.",
            SnippetInput,
            propose_snippet,
        ),
    ]
    if can(P.ADMIN_CONFIG):
        tools.insert(
            1,
            AgentTool(
                "get_configuration",
                "Effective configuration values with their source (secrets masked; honours "
                "AUTOAIR_EXPOSE_CONFIG). Filter with a key prefix or words.",
                ConfigInput,
                configuration,
            ),
        )
    if memory:
        tools += memory_tools(services, state=state, user=user, admin=True, can=can)
    return tools


__all__ = ["ACTIONS", "admin_tools"]
