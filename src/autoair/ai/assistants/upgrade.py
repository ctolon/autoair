"""Airflow upgrade assistant: find Airflow 2 / pre-3.x idioms and propose the upgrade.

Per DAG: the deterministic Airflow 3 rules of :mod:`autoair.ai.rules` (removed template
variables, deprecated operators…), ruff's ``AIR3`` migration rules on the generated code
(task bodies and scripts included) and - optionally - one LLM pass (``UPGRADE_SYSTEM``)
that adds what they missed as findings with JSON patches. The patches are applied to a
proposed spec that the UI shows as a spec diff and a generated-code diff; nothing is saved
until the user applies (and deploys) it.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, Field

from autoair.ai.findings import Finding, apply_findings, dedupe, finding_id, sort_findings
from autoair.ai.problems import SpecProblem
from autoair.ai.usage import Usage
from autoair.spec import DagSpec


class UpgradeRequest(BaseModel):
    spec: DagSpec | None = Field(default=None, description="Analyse this spec (the editor's).")
    dag_ids: list[str] = Field(default_factory=list, max_length=500, description="Saved DAGs.")
    all: bool = Field(default=False, description="Every saved AutoAir DAG.")
    use_ai: bool = Field(default=True, description="Add an LLM pass (single DAG only).")
    model: str | None = Field(default=None, max_length=200)


class RuffDiagnostic(BaseModel):
    code: str
    message: str
    line: int | None = None
    fix: str | None = Field(default=None, description="ruff's suggested fix, when it has one.")


class DagUpgrade(BaseModel):
    dag_id: str
    summary: str = ""
    findings: list[Finding] = Field(default_factory=list)
    ruff: list[RuffDiagnostic] = Field(default_factory=list)
    ruff_ran: bool = False
    base_spec: DagSpec | None = None
    proposed_spec: DagSpec | None = None
    proposed_ok: bool | None = None
    problems: list[SpecProblem] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)
    code_before: str | None = None
    code_after: str | None = None
    ai_used: bool = False
    error: str | None = None


class UpgradeReport(BaseModel):
    dags: list[DagUpgrade] = Field(default_factory=list)
    model: str | None = None
    usage: Usage | None = None
    notes: list[str] = Field(default_factory=list)


def ruff_air(
    code: str, rules: Sequence[str], timeout: float = 20
) -> tuple[bool, list[RuffDiagnostic]]:
    """(ran, diagnostics) of ruff's Airflow migration rules on ``code``."""
    from autoair.codegen.format import _ruff_command

    cmd = _ruff_command()
    if cmd is None or not rules:
        return False, []
    try:
        proc = subprocess.run(
            [
                *cmd,
                "check",
                "--isolated",
                "--no-cache",
                "--preview",
                "--select",
                ",".join(rules),
                "--output-format",
                "json",
                "--stdin-filename",
                "dag.py",
                "-",
            ],
            input=code,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        data = json.loads(proc.stdout or "[]")
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return False, []
    out = []
    for d in data:
        fix = d.get("fix") or {}
        out.append(
            RuffDiagnostic(
                code=str(d.get("code") or "?"),
                message=str(d.get("message") or ""),
                line=(d.get("location") or {}).get("row"),
                fix=fix.get("message") if isinstance(fix, dict) else None,
            )
        )
    return True, out


def upgrade_rules(spec: DagSpec, catalog: Any) -> list[Finding]:
    """The deterministic findings that concern Airflow 3 migration."""
    from autoair.ai.rules import review_rules

    return [
        f
        for f in review_rules(spec, catalog)
        if f.category == "airflow3" or (f.rule or "").startswith("deprecated")
    ]


def _ruff_findings(diagnostics: Sequence[RuffDiagnostic]) -> list[Finding]:
    return [
        Finding(
            id=finding_id("ruff", d.code, d.line, d.message),
            severity="medium",
            category="airflow3",
            title=f"{d.code}: {d.message[:150]}",
            message=(d.fix or d.message) + (f" (generated code line {d.line})" if d.line else ""),
            rule=d.code,
            source="rule",
        )
        for d in diagnostics
    ]


def upgrade_dag(
    spec: DagSpec, services: Any, *, llm: Any = None, rules: Sequence[str] = ("AIR3",)
) -> DagUpgrade:
    from autoair.ai import prompts
    from autoair.ai.assist import (
        _ask_findings,
        _brief_findings,
        _request,
        compact_spec,
        operator_brief,
    )
    from autoair.codegen import render
    from autoair.validation import policy_from_settings

    out = DagUpgrade(dag_id=spec.dag_id, base_spec=spec)
    try:
        out.code_before = render(spec, services.registry)
    except Exception as exc:
        out.error = f"code generation failed: {exc}"
    if out.code_before:
        out.ruff_ran, out.ruff = ruff_air(out.code_before, rules)
    findings = [*upgrade_rules(spec, services.catalog), *_ruff_findings(out.ruff)]
    if llm is not None:
        diagnostics = "\n".join(f"- {d.code} line {d.line}: {d.message}" for d in out.ruff)
        summary, extra = _ask_findings(
            llm,
            prompts.get("UPGRADE_SYSTEM"),
            _request(
                compact_spec(spec),
                extra=[
                    operator_brief(spec, services.catalog),
                    "Generated code:\n```python\n" + (out.code_before or "")[:20_000] + "\n```",
                    "ruff AIR3 diagnostics:\n" + (diagnostics or "(none)"),
                    "Already reported (do not repeat):\n" + _brief_findings(findings),
                ],
            ),
            {n.id for n in spec.nodes},
        )
        findings += extra
        out.summary = summary
        out.ai_used = True
    out.findings = sort_findings(dedupe(findings))
    if not out.summary:
        out.summary = (
            f"{len(out.findings)} upgrade finding(s)."
            if out.findings
            else "No Airflow 2 idioms found: this DAG already follows Airflow 3."
        )
    if any(f.patch for f in out.findings):
        applied = apply_findings(
            spec,
            out.findings,
            catalog=services.catalog,
            registry=services.registry,
            policy=policy_from_settings(services.settings),
        )
        out.proposed_spec, out.proposed_ok = applied.spec, applied.ok
        out.problems, out.skipped = applied.problems, applied.skipped
        if applied.spec is not None:
            try:
                out.code_after = render(applied.spec, services.registry)
            except Exception as exc:
                out.error = f"code generation of the proposal failed: {exc}"
    return out


__all__ = [
    "DagUpgrade",
    "RuffDiagnostic",
    "UpgradeReport",
    "UpgradeRequest",
    "ruff_air",
    "upgrade_dag",
    "upgrade_rules",
]
