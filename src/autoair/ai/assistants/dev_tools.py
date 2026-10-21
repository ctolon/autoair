"""Tools of the developer assistant.

On top of the shared read-only toolbox (catalog, validation, codegen, deploy dry-run, the
user's runs and logs through Airflow with the user's own token) and the research tools,
the developer assistant can read the DAG open in the editor, propose a change as a
reviewable diff, write validated code with the writers and use the team memory.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field

from autoair.ai.assist import _RawFinding, _RawFindings, to_findings
from autoair.ai.assistants.sources import SourceCollector
from autoair.ai.assistants.threads import Proposal
from autoair.ai.findings import apply_findings
from autoair.ai.problems import check_spec
from autoair.ai.tools import AgentTool
from autoair.spec import DagSpec
from autoair.validation import policy_from_settings

#: Memory tag of each assistant's own scope (developer memories carry no tag).
ADMIN_MEMORY_TAG = "assistant:admin"


@dataclass
class TurnState:
    """What the tools of one assistant turn produce besides their output."""

    sources: SourceCollector = field(default_factory=SourceCollector)
    proposals: list[Proposal] = field(default_factory=list)


class NoInput(BaseModel):
    pass


class ProposeSpecInput(BaseModel):
    title: str = Field(max_length=200, description="Short title of the change.")
    summary: str = Field(default="", max_length=4_000, description="Why, in 1-3 sentences.")
    findings: list[_RawFinding] = Field(
        default_factory=list,
        description="Changes as findings with JSON patches against the open DAG (preferred).",
    )
    spec: dict[str, Any] | None = Field(
        default=None, description="A complete new DagSpec (only for large rewrites / new DAGs)."
    )


class WriteCodeInput(BaseModel):
    kind: Literal["task_body", "inline_function", "bash", "sql", "custom_operator"]
    description: str = Field(min_length=1, max_length=8_000)
    node_id: str | None = Field(default=None, description="Node of the open DAG it is for.")
    existing: str | None = Field(default=None, max_length=100_000)
    function_name: str | None = None
    arguments: list[str] = Field(default_factory=list, max_length=30)
    dialect: str | None = Field(default=None, description="SQL dialect.")


class ConventionsInput(BaseModel):
    query: str | None = Field(default=None, max_length=300, description="Filter by words.")


class RememberInput(BaseModel):
    text: str = Field(min_length=3, max_length=1_000)
    scope: Literal["user", "team"] = Field(
        default="user", description="user: only for this user; team: the whole team."
    )


def spec_proposal(
    services: Any,
    base: DagSpec | None,
    args: ProposeSpecInput,
) -> tuple[Proposal, dict[str, Any]]:
    """Validate a proposed change; returns the proposal and the tool's answer."""
    policy = policy_from_settings(services.settings)
    if args.spec is not None:
        check = check_spec(
            args.spec, catalog=services.catalog, registry=services.registry, policy=policy
        )
        ids = {str(n.get("id")) for n in args.spec.get("nodes") or [] if isinstance(n, dict)}
        findings = to_findings(_RawFindings(findings=args.findings), ids)
        proposal = Proposal(
            kind="spec_patch",
            title=args.title,
            summary=args.summary,
            findings=findings,
            base_spec=base,
            proposed_spec=check.spec,
            proposed_ok=check.ok,
            problems=check.problems,
        )
    else:
        if base is None:
            raise ValueError("no DAG is open: send a complete `spec` instead of findings")
        if not args.findings:
            raise ValueError("send findings with patches or a complete spec")
        findings = to_findings(_RawFindings(findings=args.findings), {n.id for n in base.nodes})
        if not any(f.patch for f in findings):
            raise ValueError("none of the findings carries a valid patch")
        applied = apply_findings(
            base, findings, catalog=services.catalog, registry=services.registry, policy=policy
        )
        proposal = Proposal(
            kind="spec_patch",
            title=args.title,
            summary=args.summary,
            findings=findings,
            base_spec=base,
            proposed_spec=applied.spec,
            proposed_ok=applied.ok,
            problems=applied.problems,
            skipped=applied.skipped,
        )
    answer = {
        "proposal_id": proposal.id,
        "valid": proposal.proposed_ok,
        "problems": [p.model_dump(exclude_none=True) for p in proposal.problems[:30]],
        "skipped_findings": proposal.skipped,
        "note": "The user reviews the diff and decides; fix any problems and propose again.",
    }
    return proposal, answer


def developer_tools(
    services: Any,
    *,
    state: TurnState,
    spec: DagSpec | None,
    llm: Any,
    user: str | None,
    gateway_factory: Callable[[], Any] | None,
    research: bool = True,
    memory: bool = True,
) -> list[AgentTool]:
    from autoair.ai.toolbox import build_toolbox

    tools = build_toolbox(services, gateway_factory=gateway_factory)
    if research:
        from autoair.ai.research.tools import research_tools

        tools += research_tools(services, llm=llm)

    def current_spec(_: NoInput) -> Any:
        if spec is None:
            return {"open_dag": None, "note": "No DAG is open in the editor."}
        n = state.sources.add("spec", spec.dag_id, f"editor:{spec.dag_id}")
        return {
            "cite": n,
            "spec": spec.model_dump(mode="json", exclude_none=True, by_alias=True),
        }

    def propose(args: ProposeSpecInput) -> Any:
        proposal, answer = spec_proposal(services, spec, args)
        # A re-proposal replaces the earlier one with the same title.
        state.proposals[:] = [p for p in state.proposals if p.title != proposal.title]
        state.proposals.append(proposal)
        return answer

    async def write_code(args: WriteCodeInput) -> Any:
        from autoair.ai.guardrails import pipeline_for
        from autoair.ai.writers import WriteRequest, run_writer

        request = WriteRequest(
            kind=args.kind,
            description=args.description,
            spec=spec,
            node_id=args.node_id,
            existing=args.existing,
            function_name=args.function_name,
            arguments=args.arguments,
            dialect=args.dialect,
        )
        result = await run_writer(
            request, llm=llm, services=services, guardrails=pipeline_for(services)
        )
        return {
            "ok": result.ok,
            "language": result.language,
            "code": result.code,
            "explanation": result.explanation,
            "requirements": result.requirements,
            "connections": result.connections,
            "issues": [i.model_dump(exclude_none=True) for i in result.issues[:20]],
        }

    tools += [
        AgentTool(
            "get_current_spec",
            "The DagSpec open in the user's editor (unsaved changes included).",
            NoInput,
            current_spec,
        ),
        AgentTool(
            "propose_spec_change",
            "Propose a change to the open DAG as findings with JSON patches (or a complete "
            "DagSpec). Validates the result and returns problems; the user reviews the diff "
            "and applies it - nothing is saved.",
            ProposeSpecInput,
            propose,
        ),
        AgentTool(
            "write_code",
            "Write validated code with a specialised writer: task_body (TaskFlow body), "
            "inline_function, bash, sql or custom_operator. Returns code + lint issues.",
            WriteCodeInput,
            write_code,
        ),
    ]
    if memory:
        tools += memory_tools(services, state=state, user=user, admin=False)
    return tools


def memory_tools(
    services: Any, *, state: TurnState, user: str | None, admin: bool
) -> list[AgentTool]:
    """Conventions / notes of the assistant's own memory scope."""
    from autoair.ai.memory import memory_for

    team = getattr(services.settings, "team", None)

    def visible(text: str | None) -> list[Any]:
        items, _ = memory_for(services).list(visible_to=(team, user), query=text, limit=200)
        if admin:
            return [i for i in items if ADMIN_MEMORY_TAG in i.tags]
        return [i for i in items if ADMIN_MEMORY_TAG not in i.tags]

    def conventions(args: ConventionsInput) -> Any:
        out = []
        for item in visible(args.query)[:60]:
            n = state.sources.add("doc", f"memory: {item.text[:60]}", f"memory:{item.id}")
            out.append({"cite": n, "scope": item.scope, "text": item.text, "kind": item.kind})
        return out or "no memories stored yet"

    def remember(args: RememberInput) -> Any:
        if args.scope == "team" and not team:
            raise ValueError("this server has no team; store it for the user instead")
        owner = team if args.scope == "team" else user
        if not owner:
            raise ValueError("an anonymous user cannot store memories")
        item = memory_for(services).add(
            args.text,
            scope=args.scope,
            owner=owner,
            kind="preference" if args.scope == "user" else "convention",
            tags=[ADMIN_MEMORY_TAG] if admin else [],
            created_by=user,
        )
        return {"stored": item.id, "scope": item.scope}

    return [
        AgentTool(
            "list_conventions",
            "Team conventions and the user's stored preferences (memory) for this assistant.",
            ConventionsInput,
            conventions,
        ),
        AgentTool(
            "remember",
            "Store a convention/preference the user explicitly asked you to remember.",
            RememberInput,
            remember,
        ),
    ]


__all__ = [
    "ADMIN_MEMORY_TAG",
    "ProposeSpecInput",
    "TurnState",
    "developer_tools",
    "memory_tools",
    "spec_proposal",
]
