"""Natural language -> validated :class:`DagSpec` ("generate / edit with AI").

Pipeline:

1. **Select operators** - the catalog is ranked by keyword relevance to the prompt and
   a compact list (FQN + one-line summary) is sent to the model, which picks the
   operators it needs (structured output, enum-constrained to real FQNs).
2. **Draft the spec** - the DagSpec JSON schema (stable, cached system prompt) plus the
   selected operators' parameter schemas; in edit mode the existing spec is included
   and the model returns the modified document.
3. **Validate & repair** - Pydantic + :func:`validate_spec`; problems (with JSON
   pointers) are fed back to the model up to ``max_repairs`` times.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, Field

from autoair.ai import prompts
from autoair.ai.llm import LLM, ChatMessage, LLMError
from autoair.ai.problems import SpecProblem, check_spec, format_problems
from autoair.catalog import OperatorCatalog, OperatorSummary
from autoair.registry import HandlerRegistry, default_registry
from autoair.spec import SPEC_VERSION, DagSpec, OperatorNode
from autoair.validation import DEFAULT_POLICY, Policy, Problem

CORE_OPERATORS = (
    "airflow.providers.standard.operators.empty.EmptyOperator",
    "airflow.providers.standard.operators.bash.BashOperator",
)
MAX_CANDIDATES = 40
SKIP_SELECTION_BELOW = 6

_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "create",
        "dag",
        "daily",
        "do",
        "every",
        "for",
        "from",
        "get",
        "i",
        "in",
        "into",
        "is",
        "it",
        "me",
        "my",
        "new",
        "of",
        "on",
        "or",
        "please",
        "run",
        "runs",
        "should",
        "that",
        "the",
        "then",
        "to",
        "using",
        "want",
        "with",
        "when",
        "which",
        "will",
        "workflow",
        "pipeline",
        "task",
        "tasks",
        "each",
        "after",
        "before",
    ]
)


class GenerationResult(BaseModel):
    spec: DagSpec | None = Field(description="The generated spec (best effort when not ok).")
    ok: bool = Field(description="True when the spec passed validation (no errors).")
    problems: list[SpecProblem] = Field(default_factory=list)
    attempts: int = Field(description="Number of spec drafts requested from the model.")
    explanation: str = ""
    selected_operators: list[str] = Field(default_factory=list)
    model: str | None = None


# --------------------------------------------------------------------------- step 1


def _tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", text.lower()) if len(t) > 1 and t not in _STOPWORDS]


def _camel_words(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", name).lower()


def rank_operators(
    prompt: str, summaries: Sequence[OperatorSummary], limit: int = MAX_CANDIDATES
) -> list[OperatorSummary]:
    """Cheap lexical relevance ranking; core operators are always included."""
    usable = [s for s in summaries if s.builder_supported and not s.deprecated]
    if len(usable) <= limit:
        return usable
    tokens = set(_tokens(prompt))
    scored: list[tuple[float, str, OperatorSummary]] = []
    for s in usable:
        fields = (
            (_camel_words(s.class_name), 3.0),
            ((s.integration or "").lower(), 2.0),
            (s.provider.lower(), 1.5),
            (s.module.lower(), 1.0),
            (s.summary.lower(), 1.0),
        )
        score = 0.0
        for token in tokens:
            for text, weight in fields:
                if token in text:
                    score += weight
        if s.fqn in CORE_OPERATORS:
            score += 100
        scored.append((-score, s.fqn, s))
    scored.sort(key=lambda t: (t[0], t[1]))
    return [s for _, _, s in scored[:limit]]


_SELECT_SYSTEM = """You help design Apache Airflow 3 DAGs in AutoAir, a no-code DAG builder.
Given a user request and a list of available operators, choose the operators (by fully
qualified class name) that the DAG will need. Prefer specific provider operators over
generic shell/Python tasks when one fits. Pick only from the listed names; choose few."""


def select_operators(
    prompt: str,
    candidates: Sequence[OperatorSummary],
    llm: LLM,
    *,
    required: Sequence[str] = (),
) -> list[str]:
    fqns = [c.fqn for c in candidates]
    if len(candidates) <= SKIP_SELECTION_BELOW:
        return sorted(set(fqns) | set(required))
    listing = "\n".join(
        f"- {c.fqn}: {c.summary[:140] or c.class_name} [{c.category}]" for c in candidates
    )
    schema = {
        "type": "object",
        "properties": {
            "operators": {"type": "array", "items": {"type": "string", "enum": fqns}},
            "notes": {"type": "string"},
        },
        "required": ["operators", "notes"],
        "additionalProperties": False,
    }
    answer = llm.complete_json(
        _SELECT_SYSTEM,
        [{"role": "user", "content": f"Available operators:\n{listing}\n\nRequest:\n{prompt}"}],
        schema,
        max_tokens=4_000,
    )
    raw = answer.get("operators")
    picked = [f for f in raw if isinstance(f, str) and f in fqns] if isinstance(raw, list) else []
    return sorted(set(picked) | set(required))


# --------------------------------------------------------------------------- step 2

_SPEC_RULES = """You are AutoAir's DAG author. AutoAir stores Apache Airflow 3 DAGs as JSON
documents ("DagSpec", schema below) which it turns into Python DAG files.

Rules:
- Return the COMPLETE DagSpec document (spec_version 3) - never a diff.
- Every node needs a unique `id` (letters, digits, `_`, `-`). Task nodes also need a
  `task_id`; use the same value as `id` unless there is a reason not to.
- Use `kind: "operator"` nodes with an `operator` FQN taken from the operator list in the
  request. `params` may only contain parameters from that operator's parameter schema,
  and must include its required parameters. Do not put task settings (retries, pool,
  trigger_rule, doc_md, ...) into `params`; they are node fields.
- Use `kind: "python_task"` only when no operator fits; `code` is the function BODY.
- Dependencies are `edges` (`source` runs before `target`, by node id).
- Pass outputs with XCom references: {"$type": "xcom", "node": "<node id>"}.
- Jinja templates (e.g. "{{ ds }}") are allowed in templated string params.
- Schedules are objects, e.g. {"type": "cron", "expression": "0 6 * * *"},
  {"type": "preset", "preset": "@daily"} or {"type": "none"}.
- Datetimes are ISO 8601 strings with a timezone (e.g. "2025-01-01T00:00:00Z").
- Omit `position`; the editor lays the graph out automatically.
- Never invent connection ids or secrets: use descriptive placeholder connection ids
  (e.g. "postgres_default") and mention them in the explanation.

Answer with a JSON object {"explanation": "<short summary of the DAG / of the changes,
including assumptions and connections the user must create>", "spec": <DagSpec>}."""


def _spec_system(registry: HandlerRegistry) -> str:
    kinds = registry.describe()
    custom = (
        '\n\nCustom node kinds (`kind: "custom"`, `handler` = name, `config` per schema):\n'
        + json.dumps(kinds, sort_keys=True)
        if kinds
        else ""
    )
    rules = prompts.rules_block()
    return (
        _SPEC_RULES
        + custom
        + "\n\nDagSpec JSON schema:\n"
        + json.dumps(DagSpec.model_json_schema(), sort_keys=True)
        + (f"\n\n{rules}" if rules else "")
    )


_ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "explanation": {"type": "string"},
        "spec": {"type": "object", "description": "A DagSpec document (see the schema above)."},
    },
    "required": ["explanation", "spec"],
}


def _operator_context(catalog: OperatorCatalog, fqns: Sequence[str]) -> str:
    parts: list[str] = []
    for fqn in fqns:
        detail = catalog.get(fqn)
        if detail is None:
            continue
        parts.append(
            f"### {fqn}\n{detail.summary}\n"
            f"Templated fields: {', '.join(detail.template_fields) or '-'}\n"
            f"Parameter schema: {json.dumps(detail.params_schema, sort_keys=True)}"
        )
    return "\n\n".join(parts) or "(no catalog operators selected - use python_task nodes)"


def _normalize(data: dict[str, Any], existing: DagSpec | None) -> dict[str, Any]:
    """Cheap fix-ups for common omissions so repairs focus on real problems."""
    data.setdefault("spec_version", SPEC_VERSION)
    positions = (
        {n.id: n.position.model_dump(exclude_none=True) for n in existing.nodes} if existing else {}
    )
    nodes = data.get("nodes")
    if isinstance(nodes, list):
        for node in nodes:
            if not isinstance(node, dict):
                continue
            if "id" not in node:
                for key in ("task_id", "group_id"):
                    if isinstance(node.get(key), str):
                        node["id"] = re.sub(r"[^A-Za-z0-9_-]", "_", node[key])
                        break
            kind = node.get("kind")
            if (
                kind in ("operator", "python_task", "custom")
                and "task_id" not in node
                and isinstance(node.get("id"), str)
            ):
                node["task_id"] = node["id"]
            if kind == "task_group" and "group_id" not in node and isinstance(node.get("id"), str):
                node["group_id"] = node["id"]
            node_id = node.get("id")
            if "position" not in node and isinstance(node_id, str) and node_id in positions:
                node["position"] = positions[node_id]
    return data


def generate_spec(
    prompt: str,
    *,
    catalog: OperatorCatalog,
    registry: HandlerRegistry | None = None,
    llm: LLM,
    existing: DagSpec | None = None,
    max_repairs: int = 3,
    policy: Policy = DEFAULT_POLICY,
) -> GenerationResult:
    """Generate (or, with ``existing``, edit) a DagSpec from a natural-language prompt.

    Raises :class:`LLMError` only when the model fails before any draft was produced.
    """
    registry = registry or default_registry()
    summaries = catalog.summaries()
    used = sorted(
        {n.operator for n in existing.task_nodes() if isinstance(n, OperatorNode)}
        if existing
        else set()
    )
    used = [f for f in used if f in catalog]
    candidates = rank_operators(prompt, summaries)
    selected = select_operators(prompt, candidates, llm, required=used) if candidates else used

    request = [f"Operators you may use:\n\n{_operator_context(catalog, selected)}"]
    if existing is not None:
        request.append(
            "Current DAG spec to modify (keep node ids stable for unchanged tasks):\n"
            + existing.model_dump_json(exclude_none=True, by_alias=True)
        )
        request.append(f"Requested change:\n{prompt}")
    else:
        request.append(f"Build this DAG:\n{prompt}")
    messages: list[ChatMessage] = [{"role": "user", "content": "\n\n".join(request)}]
    system = _spec_system(registry)

    best: GenerationResult | None = None
    attempts = 0
    for attempt in range(max(0, max_repairs) + 1):
        try:
            answer = llm.complete_json(system, messages, _ANSWER_SCHEMA)
        except LLMError:
            if best is None:
                raise
            break
        attempts += 1
        explanation = str(answer.get("explanation") or "")
        raw_spec = answer.get("spec")
        if not isinstance(raw_spec, dict):
            # Some models answer with the bare spec.
            raw_spec = answer if "nodes" in answer or "dag_id" in answer else {}
            raw_spec = {k: v for k, v in raw_spec.items() if k != "explanation"}
        raw_spec = _normalize(raw_spec, existing)
        check = check_spec(raw_spec, catalog=catalog, registry=registry, policy=policy)
        result = GenerationResult(
            spec=check.spec,
            ok=check.ok,
            problems=check.problems,
            attempts=attempts,
            explanation=explanation,
            selected_operators=list(selected),
            model=llm.model,
        )
        if best is None or result.spec is not None or best.spec is None:
            best = result  # latest draft wins unless it lost a previously parsed spec
        if check.ok:
            return result
        if attempt == max_repairs:
            break
        errors = [p for p in check.problems if p.severity == "error"]
        messages.append(
            {
                "role": "assistant",
                "content": json.dumps({"explanation": explanation, "spec": raw_spec}),
            }
        )
        messages.append(
            {
                "role": "user",
                "content": "AutoAir validation rejected that spec:\n"
                + format_problems(errors)
                + "\n\nFix every problem and answer again with the complete JSON object "
                '{"explanation": ..., "spec": ...}.',
            }
        )
    assert best is not None
    best.attempts = attempts
    return best


# --------------------------------------------------------------------------- prose helpers


def _spec_json(spec: DagSpec) -> str:
    return spec.model_dump_json(exclude_none=True, by_alias=True, exclude={"environments"})


def explain_problems(spec: DagSpec, problems: Sequence[Problem], llm: LLM) -> str:
    if not problems:
        return "No problems found."
    content = f"DAG spec:\n{_spec_json(spec)}\n\nProblems:\n{format_problems(list(problems))}"
    return llm.complete_text(
        prompts.get("EXPLAIN_PROBLEMS_SYSTEM"), [{"role": "user", "content": content}]
    )


def describe_dag(spec: DagSpec, llm: LLM) -> str:
    return llm.complete_text(
        prompts.get("DESCRIBE_SYSTEM"), [{"role": "user", "content": _spec_json(spec)}]
    )
