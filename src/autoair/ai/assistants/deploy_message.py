"""Commit / pull-request messages generated from the spec diff of a deploy.

The deployed spec (the deploy target's current version) is diffed with the spec about to
be deployed (:func:`autoair.merge.diff_specs`); the structured diff - never the full
spec - goes to the model with ``DEPLOY_MESSAGE_SYSTEM``. Without a model a deterministic
message is built from the same diff, so the button always produces something useful.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from autoair.ai.usage import Usage
from autoair.merge import SpecDiff, diff_specs
from autoair.spec import DagSpec


class DeployMessageRequest(BaseModel):
    spec: DagSpec
    environment: str | None = Field(default=None, max_length=100)
    use_ai: bool = True
    model: str | None = Field(default=None, max_length=200)


class DeployMessage(BaseModel):
    title: str
    description: str
    ai_used: bool = False
    new_dag: bool = False
    model: str | None = None
    usage: Usage | None = None


class _Answer(BaseModel):
    title: str
    description: str


def diff_brief(diff: SpecDiff, limit: int = 12_000) -> str:
    lines: list[str] = []
    for f in diff.changed_fields:
        lines.append(f"DAG {f.path}: {json.dumps(f.before)} -> {json.dumps(f.after)}")
    for n in diff.added_nodes:
        lines.append(f"added node {n.get('id')} ({n.get('kind')}, {n.get('operator') or ''})")
    for n in diff.removed_nodes:
        lines.append(f"removed node {n.get('id')}")
    for c in diff.changed_nodes:
        if c.layout_only:
            continue
        for ch in c.changes:
            before = json.dumps(ch.before, default=str)[:300]
            after = json.dumps(ch.after, default=str)[:300]
            lines.append(f"node {c.id} {ch.path}: {before} -> {after}")
    for e in diff.added_edges:
        lines.append(f"added edge {e.source} -> {e.target}")
    for e in diff.removed_edges:
        lines.append(f"removed edge {e.source} -> {e.target}")
    text = "\n".join(lines) or "(no functional change)"
    return text[:limit]


def fallback_message(dag_id: str, diff: SpecDiff | None) -> DeployMessage:
    if diff is None:
        return DeployMessage(
            title=f"Add DAG {dag_id}"[:72],
            description=f"## Summary\n- New DAG `{dag_id}` created in AutoAir.",
            new_dag=True,
        )
    changes: list[str] = []
    for n in diff.added_nodes:
        changes.append(f"- Add task `{n.get('id')}`")
    for n in diff.removed_nodes:
        changes.append(f"- Remove task `{n.get('id')}`")
    for c in diff.changed_nodes:
        if not c.layout_only:
            fields = ", ".join(sorted({ch.path.split(".")[0] for ch in c.changes}))
            changes.append(f"- Change `{c.id}`: {fields}")
    for f in diff.changed_fields:
        changes.append(f"- Change DAG setting `{f.path}`")
    if diff.added_edges or diff.removed_edges:
        changes.append(
            f"- Dependencies: +{len(diff.added_edges)} / -{len(diff.removed_edges)} edges"
        )
    verbs = []
    if diff.added_nodes:
        verbs.append(f"add {len(diff.added_nodes)} task(s)")
    if diff.removed_nodes:
        verbs.append(f"remove {len(diff.removed_nodes)} task(s)")
    if any(not c.layout_only for c in diff.changed_nodes) or diff.changed_fields:
        verbs.append("update settings")
    title = f"Update {dag_id}: " + ", ".join(verbs) if verbs else f"Update {dag_id}"
    body = "## Changes\n" + ("\n".join(changes[:40]) or "- Layout only")
    return DeployMessage(title=title[:72], description=body)


def deployed_spec(services: Any, dag_id: str) -> DagSpec | None:
    from autoair.mcp.store import store_for

    try:
        return store_for(services).get(dag_id)
    except Exception:
        return None


async def deploy_message(
    services: Any, body: DeployMessageRequest, llm: Any = None
) -> DeployMessage:
    import asyncio

    from autoair.ai import prompts
    from autoair.ai.llm import LLMError
    from autoair.ai.usage import track_usage

    old = await asyncio.to_thread(deployed_spec, services, body.spec.dag_id)
    diff = diff_specs(old, body.spec) if old is not None else None
    fallback = fallback_message(body.spec.dag_id, diff)
    if llm is None or not body.use_ai:
        return fallback
    content = [
        f"DAG id: {body.spec.dag_id}",
        f"Environment: {body.environment or 'default'}",
        "New DAG (no deployed version)." if diff is None else "Diff:\n" + diff_brief(diff),
    ]
    if diff is None:
        from autoair.ai.assist import compact_spec

        content.append("The new DAG:\n" + compact_spec(body.spec))
    with track_usage() as meter:
        try:
            answer = await llm.acomplete_structured(
                prompts.get("DEPLOY_MESSAGE_SYSTEM"),
                "\n\n".join(content),
                _Answer,
                max_tokens=1_500,
            )
        except LLMError:
            return fallback
    return DeployMessage(
        title=answer.title.strip().splitlines()[0][:72] if answer.title.strip() else fallback.title,
        description=answer.description.strip() or fallback.description,
        ai_used=True,
        new_dag=diff is None,
        model=str(getattr(llm, "model", "")) or None,
        usage=meter.total,
    )


__all__ = [
    "DeployMessage",
    "DeployMessageRequest",
    "deploy_message",
    "diff_brief",
    "fallback_message",
]
