"""Spec validation: structure, graph, catalog/params, security policy, and import check."""

from __future__ import annotations

import ast
import fnmatch
import functools
import importlib.util
import json
import re
from collections import defaultdict, deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field, ValidationError

from autoair._compat import airflow_version
from autoair.catalog import OperatorCatalog
from autoair.codegen import CodegenError, render
from autoair.codegen.body import normalize_task_body
from autoair.codegen.builder import effective_edges, node_values
from autoair.codegen.features import format_version, required_features
from autoair.policy_rules import OpaOptions, PolicyRule
from autoair.registry import HandlerRegistry, default_registry, handler_problems
from autoair.sections import (
    DEFAULT_PRIVILEGED_OPERATORS,
    DEFAULT_SENSITIVE_ASSIGNMENT,
    DEFAULT_SENSITIVE_PARAM,
    DEFAULT_SENSITIVE_PARAM_EXCLUDE,
    PolicyRules,
)
from autoair.spec import (
    AssetAndTimeScheduleSpec,
    AssetCombination,
    AssetExpr,
    AssetLeaf,
    AssetOrTimeScheduleSpec,
    AssetSchedule,
    BlueprintNode,
    CallbackRef,
    CustomNode,
    DagSpec,
    FileAsset,
    NoteNode,
    OperatorNode,
    PresetSchedule,
    PythonRef,
    PythonTaskNode,
    TaskGroupNode,
    TaskNode,
    iter_python_refs,
    iter_tagged,
    iter_xcom_refs,
)
from autoair.spec.models import AssetRef
from autoair.telemetry import span
from autoair.validation.blueprints import blueprint_problems
from autoair.validation.dagbag_check import (
    ImportCheckResult,
    ImportCheckUnavailable,
    check_source,
)
from autoair.validation.jinja import check_template, looks_like_template
from autoair.validation.llm import llm_problems
from autoair.validation.pylint import lint_python_tasks
from autoair.validation.shell import shell_problems

if TYPE_CHECKING:
    from autoair.blueprints.catalog import BlueprintCatalog

Severity = Literal["error", "warning", "info"]

# Operators that execute arbitrary code/commands; deploying them needs the same
# privilege as python_task nodes. Built-in default of AUTOAIR_POLICY__PRIVILEGED_OPERATORS
# (extend with AUTOAIR_POLICY__PRIVILEGED_OPERATORS_EXTRA); use Policy.is_privileged().
PRIVILEGED_OPERATORS = frozenset(DEFAULT_PRIVILEGED_OPERATORS)


class Problem(BaseModel):
    severity: Severity
    code: str
    message: str
    node_id: str | None = None
    field: str | None = None
    line: int | None = None
    column: int | None = None
    doc_url: str | None = Field(
        default=None, description="Documentation of the rule (e.g. ruff/ShellCheck rule page)."
    )


class ValidationReport(BaseModel):
    ok: bool
    problems: list[Problem]
    import_check: ImportCheckResult | None = None


@dataclass(frozen=True)
class Policy:
    """Organisation rules enforced on every validation/deploy."""

    allow_python_code: bool = True
    allow_privileged_operators: bool = True
    require_owner: bool = False
    require_description: bool = False
    min_retries: int | None = None
    required_tags: Sequence[str] = ()
    forbidden_operators: Sequence[str] = ()  # fnmatch patterns over operator FQNs
    max_tasks: int | None = None
    dag_id_prefix: str | None = None
    team_dag_id_prefix: bool = False
    python_ref_allow: Sequence[str] = ("*",)  # fnmatch patterns over referenced object paths
    # AUTOAIR_POLICY__* (autoair.sections.PolicyRules)
    privileged_operators: Sequence[str] = tuple(DEFAULT_PRIVILEGED_OPERATORS)  # fnmatch
    dag_id_patterns: Sequence[str] = ()  # every one must fullmatch
    task_id_patterns: Sequence[str] = ()
    secret_param_patterns: Sequence[str] = (DEFAULT_SENSITIVE_PARAM,)
    secret_param_exclude_patterns: Sequence[str] = (DEFAULT_SENSITIVE_PARAM_EXCLUDE,)
    secret_assignment_patterns: Sequence[str] = (DEFAULT_SENSITIVE_ASSIGNMENT,)
    jinja_names: Sequence[str] = ()
    # policy.rules / policy.opa_* (autoair.policy_rules)
    rules: Sequence[PolicyRule] = ()
    opa: OpaOptions | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def is_privileged(self, operator: str) -> bool:
        return any(fnmatch.fnmatchcase(operator, p) for p in self.privileged_operators)


@functools.lru_cache(maxsize=64)
def _regexes(patterns: tuple[str, ...], flags: int = 0) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(p, flags) for p in patterns if p)


def _matches(patterns: Sequence[str], text: str, flags: int = 0) -> bool:
    return any(r.search(text) for r in _regexes(tuple(patterns), flags))


DEFAULT_POLICY = Policy()


def policy_from_settings(s: Any) -> Policy:
    """The organisation policy configured in :class:`~autoair.settings.Settings` (shared by the
    API, the MCP server and the CLI so every entry point enforces the same rules)."""
    rules = getattr(s, "policy", None) or PolicyRules()
    features = getattr(s, "features", None)
    python_code = bool(getattr(features, "python_code", True))

    def both(base: str, extra: Sequence[str]) -> tuple[str, ...]:
        return tuple(p for p in (base, *extra) if p)

    return Policy(
        allow_python_code=s.allow_python_code and python_code,
        allow_privileged_operators=s.allow_privileged_operators,
        require_owner=s.require_owner,
        require_description=s.require_description,
        min_retries=s.min_retries,
        required_tags=tuple(s.required_tags),
        forbidden_operators=tuple(s.forbidden_operators),
        max_tasks=s.max_tasks,
        dag_id_prefix=s.dag_id_prefix,
        team_dag_id_prefix=s.team_dag_id_prefix,
        python_ref_allow=tuple(s.python_ref_allow),
        privileged_operators=rules.privileged(),
        dag_id_patterns=both(rules.dag_id_pattern, rules.dag_id_patterns_extra),
        task_id_patterns=both(rules.task_id_pattern, rules.task_id_patterns_extra),
        secret_param_patterns=both(rules.secret_param_pattern, rules.secret_param_patterns_extra),
        secret_param_exclude_patterns=both(
            rules.secret_param_exclude_pattern, rules.secret_param_exclude_patterns_extra
        ),
        secret_assignment_patterns=both(
            rules.secret_assignment_pattern, rules.secret_assignment_patterns_extra
        ),
        jinja_names=tuple(rules.jinja_names_extra),
        rules=tuple(rules.rules),
        opa=rules.opa(),
    )


MESSAGING_SCHEME_PACKAGES = {
    "sqs": "airflow.providers.amazon",
    "kinesis": "airflow.providers.amazon",
    "kafka": "airflow.providers.apache.kafka",
    "redis+pubsub": "airflow.providers.redis",
    "google+pubsub": "airflow.providers.google",
    "azure+servicebus": "airflow.providers.microsoft.azure",
    "ibmmq": "airflow.providers.ibm.mq",
}


@dataclass(frozen=True)
class TemplateLock:
    """What an operator template (blueprint) enforces on the tasks created from it.

    ``locked``/``required`` map field paths (``retries``, ``params.bash_command``,
    ``arguments.x``…) to the template's value; ``kind``/``operator`` identify the task type.
    """

    title: str
    version: str | None
    kind: str
    operator: str | None = None
    locked: Mapping[str, Any] = field(default_factory=dict)
    required: Sequence[str] = ()


TemplateLookup = Callable[[str], "TemplateLock | None"]


def validate_spec(
    spec: DagSpec,
    *,
    catalog: OperatorCatalog | None = None,
    registry: HandlerRegistry | None = None,
    policy: Policy = DEFAULT_POLICY,
    import_check: bool = False,
    target_version: tuple[int, int, int] | None = None,
    import_timeout: float = 60,
    templates: TemplateLookup | None = None,
    environment: str | None = None,
    context: Mapping[str, Any] | None = None,
    blueprints: BlueprintCatalog | None = None,
    source: str | None = None,
) -> ValidationReport:
    """``templates`` resolves the operator templates tasks were created from
    (``template_ref``); without it, locked template fields are not checked.

    ``environment`` is the deployment environment the spec is validated for (policy rules
    with ``match.environments`` apply only when it is known); ``context`` adds facts (user,
    action…) to the OPA input. ``blueprints`` is the blueprint catalog (the allowlist and
    config schemas of blueprint steps); without it, blueprint steps are checked structurally
    only. ``source``: the DAG file a source-preserving deploy writes
    instead of the generated one -- it is import-checked and scanned (secrets, operators)
    in place of the generated file."""
    with span("validate", dag_id=spec.dag_id, import_check=import_check):
        return _validate(
            spec,
            catalog,
            registry or default_registry(),
            policy,
            import_check,
            target_version,
            import_timeout,
            templates,
            environment,
            context,
            blueprints,
            source,
        )


def _validate(
    spec: DagSpec,
    catalog: OperatorCatalog | None,
    registry: HandlerRegistry,
    policy: Policy,
    import_check: bool,
    target_version: tuple[int, int, int] | None,
    import_timeout: float = 60,
    templates: TemplateLookup | None = None,
    environment: str | None = None,
    context: Mapping[str, Any] | None = None,
    blueprints: BlueprintCatalog | None = None,
    preserved: str | None = None,
) -> ValidationReport:
    if spec.raw_code is not None:
        from autoair.validation.raw import validate_raw

        return validate_raw(
            spec,
            policy=policy,
            import_check=import_check,
            import_timeout=import_timeout,
            target_version=target_version,
            environment=environment,
            context=context,
        )
    problems: list[Problem] = []
    problems += _structure(spec)
    problems += _graph(spec)
    problems += _nodes(spec, catalog, registry, policy)
    problems += _schedule(spec)
    problems += _behaviours(spec, catalog)
    problems += _templates(spec, catalog, policy)
    problems += _loops(spec, policy)
    problems += blueprint_problems(
        spec, blueprints if blueprints is not None else getattr(catalog, "blueprints", None)
    )
    problems += _policy(spec, policy)
    problems += _python_refs(spec, policy)
    problems += _inline_code(spec, policy)
    problems += _hardcoded_secrets(spec, policy)
    problems += shell_problems(spec)
    problems += llm_problems(spec)
    problems += _versions(spec, target_version or airflow_version())
    if templates is not None:
        problems += template_problems(spec, templates)
    problems += _organisation_rules(spec, policy, environment, target_version, context)
    if preserved is not None:
        from autoair.validation.raw import preserved_file_problems

        problems += preserved_file_problems(preserved, policy)

    result: ImportCheckResult | None = None
    if not any(p.severity == "error" for p in problems):
        try:
            source = preserved
            if source is None:
                source = render(spec, registry, fmt=False, airflow_version=target_version)
        except CodegenError as exc:
            problems.append(
                Problem(severity="error", code="codegen", message=str(exc), node_id=exc.node_id)
            )
        else:
            if import_check:
                try:
                    result = check_source(
                        source, filename=f"{spec.dag_id}.py", timeout=import_timeout
                    )
                except ImportCheckUnavailable as exc:
                    problems.append(
                        Problem(severity=exc.severity, code="import_service", message=str(exc))
                    )
                    result = None
            if result is not None:
                for err in result.import_errors:
                    problems.append(Problem(severity="error", code="import", message=err))
                if result.ok and spec.dag_id not in result.dag_ids:
                    problems.append(
                        Problem(
                            severity="error",
                            code="import",
                            message="generated file did not define the DAG",
                        )
                    )
    ok = not any(p.severity == "error" for p in problems)
    return ValidationReport(ok=ok, problems=problems, import_check=result)


# --------------------------------------------------------------------------- operator templates

MISSING_VALUE: Any = object()  # template_field_value: the path does not exist


def template_field_value(node: BaseModel, path: str) -> Any:
    """The JSON value at ``path`` (``retries``, ``params.x``) of a node, or ``MISSING_VALUE``."""
    data = node.model_dump(mode="json")
    head, _, rest = path.partition(".")
    value = data.get(head, MISSING_VALUE)
    if rest:
        value = value.get(rest, MISSING_VALUE) if isinstance(value, dict) else MISSING_VALUE
    return value


def _problem_field(path: str) -> str:
    # Operator parameters are reported by name (like the catalog's `param` problems).
    return path.removeprefix("params.")


def template_problems(spec: DagSpec, lookup: TemplateLookup) -> list[Problem]:
    """Locked fields changed / required inputs left empty in tasks created from an operator
    template (``template_ref``)."""
    out: list[Problem] = []
    for node in spec.nodes:
        ref = getattr(node, "template_ref", None)
        if ref is None:
            continue
        try:
            lock = lookup(ref.id)
        except Exception:
            lock = None
        if lock is None:
            out.append(
                _err(
                    "template_missing",
                    f"operator template {ref.id!r} no longer exists: its locked values are "
                    "not enforced",
                    node.id,
                    severity="warning",
                )
            )
            continue
        name = f"{lock.title!r}" + (f" (v{lock.version})" if lock.version else "")
        kind_ok = node.kind == lock.kind and (
            lock.operator is None or getattr(node, "operator", None) == lock.operator
        )
        if not kind_ok:
            out.append(
                _err(
                    "template_locked",
                    f"the task type is locked by operator template {name}"
                    + (f": {lock.operator}" if lock.operator else ""),
                    node.id,
                    "operator" if node.kind == "operator" else None,
                )
            )
            continue
        for path, expected in lock.locked.items():
            if template_field_value(node, path) != expected:
                out.append(
                    _err(
                        "template_locked",
                        f"{path} is locked by operator template {name} "
                        f"(expected {json.dumps(expected, default=str)[:200]})",
                        node.id,
                        _problem_field(path),
                    )
                )
        for path in lock.required:
            value = template_field_value(node, path)
            if value is MISSING_VALUE or value is None or value == "" or value == [] or value == {}:
                out.append(
                    _err(
                        "template_required",
                        f"{path} is required by operator template {name}",
                        node.id,
                        _problem_field(path),
                    )
                )
        if ref.version and lock.version and ref.version != lock.version:
            out.append(
                _err(
                    "template_updated",
                    f"operator template {name} was updated since this task was inserted "
                    f"(v{ref.version})",
                    node.id,
                    severity="info",
                )
            )
    return out


# --------------------------------------------------------------------------- structure


def _err(
    code: str,
    message: str,
    node_id: str | None = None,
    field: str | None = None,
    severity: Severity = "error",
) -> Problem:
    return Problem(severity=severity, code=code, message=message, node_id=node_id, field=field)


def effective_task_id(
    node: TaskNode | TaskGroupNode | BlueprintNode, nodes: dict[str, object]
) -> str:
    """Task/group id including parent group prefixes, as Airflow computes it."""
    if isinstance(node, TaskGroupNode):
        own = node.group_id
    elif isinstance(node, BlueprintNode):
        own = node.step_id
    else:
        own = node.task_id
    parts = [own]
    parent_id = node.parent_group
    seen: set[str] = set()
    while parent_id and parent_id not in seen:
        seen.add(parent_id)
        parent = nodes.get(parent_id)
        if not isinstance(parent, TaskGroupNode):
            break
        if parent.prefix_group_id:
            parts.append(parent.group_id)
        parent_id = parent.parent_group
    return ".".join(reversed(parts))


def _structure(spec: DagSpec) -> list[Problem]:
    problems: list[Problem] = []
    nodes: dict[str, object] = {}
    for n in spec.nodes:
        if n.id in nodes:
            problems.append(_err("duplicate_node", f"duplicate node id {n.id!r}", n.id))
        nodes[n.id] = n
    seen_ids: dict[str, str] = {}
    for n in spec.nodes:
        if isinstance(n, NoteNode):
            continue
        if n.parent_group is not None:
            parent = nodes.get(n.parent_group)
            if not isinstance(parent, TaskGroupNode):
                problems.append(
                    _err(
                        "bad_parent",
                        f"parent_group {n.parent_group!r} is not a task group",
                        n.id,
                        "parent_group",
                    )
                )
                continue
        eff = effective_task_id(n, nodes)
        if eff in seen_ids:
            problems.append(
                _err(
                    "duplicate_task_id",
                    f"id {eff!r} is used by more than one task/group",
                    n.id,
                    "task_id",
                )
            )
        seen_ids[eff] = n.id
    # group nesting cycles
    for g in spec.group_nodes():
        cur, seen = g.parent_group, {g.id}
        while cur:
            if cur in seen:
                problems.append(_err("group_cycle", "task group nesting forms a cycle", g.id))
                break
            seen.add(cur)
            parent = nodes.get(cur)
            cur = parent.parent_group if isinstance(parent, TaskGroupNode) else None
    fn_names: dict[str, str] = {}
    for n in spec.nodes:
        if isinstance(n, PythonTaskNode):
            if n.function_name in fn_names:
                problems.append(
                    _err(
                        "duplicate_function",
                        f"function name {n.function_name!r} is used twice",
                        n.id,
                        "function_name",
                    )
                )
            fn_names[n.function_name] = n.id
    if spec.start_date and spec.end_date and spec.end_date <= spec.start_date:
        problems.append(_err("dates", "end_date must be after start_date", field="end_date"))
    if spec.schedule.type not in ("none",) and spec.start_date is None:
        problems.append(
            _err(
                "start_date",
                "scheduled DAGs should define a start_date",
                field="start_date",
                severity="warning",
            )
        )
    return problems


# --------------------------------------------------------------------------- graph


def _graph(spec: DagSpec) -> list[Problem]:
    problems: list[Problem] = []
    nodes = spec.node_map()
    adj: dict[str, set[str]] = defaultdict(set)
    seen_edges: set[tuple[str, str]] = set()
    for e in spec.edges:
        bad = [x for x in (e.source, e.target) if x not in nodes]
        if bad:
            problems.append(_err("edge_unknown_node", f"edge references unknown node(s) {bad}"))
            continue
        if e.source == e.target:
            problems.append(_err("self_loop", "a node cannot depend on itself", e.source))
            continue
        if isinstance(nodes[e.source], NoteNode) or isinstance(nodes[e.target], NoteNode):
            problems.append(_err("note_edge", "notes cannot be connected", e.source))
            continue
        if (e.source, e.target) in seen_edges:
            problems.append(
                _err(
                    "duplicate_edge",
                    f"duplicate edge {e.source} → {e.target}",
                    e.source,
                    severity="warning",
                )
            )
        seen_edges.add((e.source, e.target))
        if _is_ancestor_group(nodes, e.source, e.target) or _is_ancestor_group(
            nodes, e.target, e.source
        ):
            problems.append(
                _err("group_edge", "cannot connect a task group with its own member", e.target)
            )
            continue
        adj[e.source].add(e.target)
    # XCom references imply dependencies too.
    for t in spec.task_nodes():
        for ref in _xcom_refs(t):
            target = nodes.get(ref)
            if target is None:
                problems.append(
                    _err("xcom_unknown", f"XCom reference to unknown node {ref!r}", t.id)
                )
            elif isinstance(target, TaskGroupNode):
                problems.append(_err("xcom_group", "XCom must reference a task, not a group", t.id))
            elif isinstance(target, BlueprintNode):
                problems.append(
                    _err("xcom_blueprint", "XCom must reference a task, not a blueprint step", t.id)
                )
            elif isinstance(target, NoteNode):
                problems.append(_err("xcom_note", "XCom must reference a task", t.id))
            elif ref == t.id:
                problems.append(_err("xcom_self", "a task cannot consume its own output", t.id))
            elif getattr(target, "disabled", False) and not t.disabled:
                problems.append(
                    _err(
                        "xcom_disabled",
                        f"uses the output of disabled task {target.task_id!r}",
                        t.id,
                    )
                )
            else:
                adj[ref].add(t.id)
    cycle = _find_cycle(_expand_groups(spec, adj))
    if cycle:
        problems.append(_err("cycle", "dependency cycle: " + " → ".join(cycle), cycle[0]))
    elif spec.group_nodes() and not any(p.severity == "error" for p in problems):
        problems += _task_group_cycles(spec)
    return problems


# At most this many cycles are named; Airflow's own warning lists the first few as well.
_MAX_LISTED_GROUP_CYCLES = 5


def _task_group_cycles(spec: DagSpec) -> list[Problem]:
    """Task groups that depend on each other in a cycle when each group is one unit.

    Airflow 3.4 warns about these (``TaskGroupCycleDeprecationWarning``, a DAG warning in the
    UI) and 3.5 plans to reject them at parse time, although the task graph is acyclic: a
    dependency into or out of any task of a group counts for the whole group, so a path that
    leaves a group and comes back into it is a cycle (``a >> bridge >> b`` with ``a`` and ``b``
    in one group). Same projection as Airflow's ``TaskGroup._find_dependency_cycles``: each
    edge counts in the deepest group holding both ends, between the children of that group
    that contain them. Edges are the generated ones (disabled tasks bridged) plus XComs.
    """
    nodes = spec.node_map()

    def chain(node_id: str) -> list[str | None]:
        """``[root, outermost group, ..., node]``: the node and every group above it."""
        out: list[str | None] = [node_id]
        cur = getattr(nodes.get(node_id), "parent_group", None)
        seen: set[str] = set()
        while cur and cur not in seen and isinstance(nodes.get(cur), TaskGroupNode):
            seen.add(cur)
            out.append(cur)
            cur = nodes[cur].parent_group
        out.append(None)
        return out[::-1]

    pairs = {(e.source, e.target) for e in effective_edges(spec)}
    for t in spec.task_nodes(include_disabled=False):
        for ref in _xcom_refs(t):
            if isinstance(nodes.get(ref), TaskNode) and not getattr(nodes[ref], "disabled", False):
                pairs.add((ref, t.id))
    # Projected graph per group (None = the DAG's root group): child unit -> child units.
    projected: dict[str | None, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for src, dst in pairs:
        a, b = chain(src), chain(dst)
        depth = 0
        while depth < min(len(a), len(b)) and a[depth] == b[depth]:
            depth += 1
        if depth >= len(a) or depth >= len(b):
            continue  # one end contains the other (reported as group_edge)
        ua, ub = a[depth], b[depth]
        if ua is not None and ub is not None:
            projected[a[depth - 1]][ua].add(ub)
    cycles: list[list[str]] = []
    for graph in projected.values():
        cycles += [c for c in _strong_components(graph) if len(c) > 1]
    if not cycles:
        return []

    def name(node_id: str) -> str:
        n = nodes[node_id]
        kind = "group" if isinstance(n, TaskGroupNode) else "task"
        return f"{kind} {effective_task_id(n, nodes)!r}"  # type: ignore[arg-type]

    order = {n.id: i for i, n in enumerate(spec.nodes)}
    cycles = [sorted(c, key=order.__getitem__) for c in cycles]
    cycles.sort(key=lambda c: order[c[0]])
    listed = "; ".join(", ".join(name(m) for m in c) for c in cycles[:_MAX_LISTED_GROUP_CYCLES])
    more = len(cycles) - _MAX_LISTED_GROUP_CYCLES
    first_group = next((m for m in cycles[0] if isinstance(nodes[m], TaskGroupNode)), cycles[0][0])
    return [
        _err(
            "task_group_cycle",
            f"task groups depend on each other in a cycle: {listed}"
            + (f" ({more} more)" if more > 0 else "")
            + ". A dependency into or out of any task of a group counts for the whole group; "
            "Airflow 3.4 deprecates this and Airflow 3.5 is planned to reject it. Move tasks "
            "between groups so that each group depends on the others in one direction only",
            first_group,
            severity="warning",
        )
    ]


def _strong_components(graph: Mapping[str, set[str]]) -> list[list[str]]:
    """Strongly connected components (iterative Kosaraju, so large graphs do not recurse)."""
    vertices = set(graph) | {v for targets in graph.values() for v in targets}
    reverse: dict[str, set[str]] = defaultdict(set)
    for src, targets in graph.items():
        for dst in targets:
            reverse[dst].add(src)
    finished: list[str] = []
    seen: set[str] = set()
    for start in sorted(vertices):
        if start in seen:
            continue
        seen.add(start)
        stack: list[tuple[str, list[str]]] = [(start, sorted(graph.get(start, ())))]
        while stack:
            vertex, pending = stack[-1]
            if pending:
                nxt = pending.pop()
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append((nxt, sorted(graph.get(nxt, ()))))
            else:
                finished.append(vertex)
                stack.pop()
    components: list[list[str]] = []
    assigned: set[str] = set()
    for start in reversed(finished):
        if start in assigned:
            continue
        component, todo = [], [start]
        assigned.add(start)
        while todo:
            vertex = todo.pop()
            component.append(vertex)
            for prev in reverse.get(vertex, ()):
                if prev not in assigned:
                    assigned.add(prev)
                    todo.append(prev)
        components.append(component)
    return components


def _is_ancestor_group(nodes: Mapping[str, object], group_id: str, node_id: str) -> bool:
    node = nodes.get(node_id)
    seen: set[str] = set()
    while node is not None and getattr(node, "parent_group", None):
        parent_id: str = node.parent_group  # type: ignore[attr-defined]
        if parent_id == group_id:
            return True
        if parent_id in seen:
            return False
        seen.add(parent_id)
        node = nodes.get(parent_id)
    return False


def _expand_groups(spec: DagSpec, adj: dict[str, set[str]]) -> dict[str, set[str]]:
    """Replace group endpoints by their member tasks (Airflow semantics: roots/leaves)."""
    members: dict[str, list[str]] = defaultdict(list)
    nodes = spec.node_map()
    for t in spec.task_nodes():
        cur = t.parent_group
        while cur:
            members[cur].append(t.id)
            parent = nodes.get(cur)
            cur = parent.parent_group if isinstance(parent, TaskGroupNode) else None

    def expand(n: str) -> list[str]:
        return members.get(n, []) if isinstance(nodes.get(n), TaskGroupNode) else [n]

    out: dict[str, set[str]] = defaultdict(set)
    for src, targets in adj.items():
        for tgt in targets:
            for a in expand(src):
                for b in expand(tgt):
                    if a != b:
                        out[a].add(b)
    return out


def _find_cycle(adj: dict[str, set[str]]) -> list[str] | None:
    indeg: dict[str, int] = defaultdict(int)
    for s, ts in adj.items():
        indeg.setdefault(s, 0)
        for t in ts:
            indeg[t] += 1
    queue = deque(n for n, d in indeg.items() if d == 0)
    visited = 0
    while queue:
        n = queue.popleft()
        visited += 1
        for t in adj.get(n, ()):
            indeg[t] -= 1
            if indeg[t] == 0:
                queue.append(t)
    if visited == len(indeg):
        return None
    # Recover one cycle among remaining nodes for the message.
    remaining = {n for n, d in indeg.items() if d > 0}
    start = next(iter(sorted(remaining)))
    path, seen = [start], {start}
    cur = start
    while True:
        nxt = next((t for t in sorted(adj.get(cur, ())) if t in remaining), None)
        if nxt is None:
            return path
        if nxt in seen:
            return [*path[path.index(nxt) :], nxt]
        path.append(nxt)
        seen.add(nxt)
        cur = nxt


def _xcom_refs(node: TaskNode) -> list[str]:
    return [r.node for v in node_values(node) for r in iter_xcom_refs(v)]


# --------------------------------------------------------------------------- nodes


_COMMON_FIELDS = {
    "task_id",
    "retries",
    "retry_delay",
    "execution_timeout",
    "pool",
    "pool_slots",
    "queue",
    "owner",
    "doc_md",
    "outlets",
    "trigger_rule",
    "task_group",
    "dag",
    "inlets",
    "retry_policy",
    "priority_weight",
    "weight_rule",
    "executor",
    "executor_config",
    "on_success_callback",
    "on_failure_callback",
    "on_retry_callback",
    "on_execute_callback",
    "on_skipped_callback",
}


def _nodes(
    spec: DagSpec, catalog: OperatorCatalog | None, registry: HandlerRegistry, policy: Policy
) -> list[Problem]:
    problems: list[Problem] = []
    # Parse every Python body first so all of them are linted by ONE ruff run.
    # Keyed by object identity: duplicate node ids are reported, not merged.
    syntax: dict[int, SyntaxError] = {}
    parsed: list[PythonTaskNode] = []
    for n in spec.task_nodes():
        if isinstance(n, PythonTaskNode):
            try:
                ast.parse(normalize_task_body(n.code) or "pass")
            except SyntaxError as exc:
                syntax[id(n)] = exc
            else:
                parsed.append(n)
    lint = {id(n): issues for n, issues in zip(parsed, lint_python_tasks(parsed), strict=True)}
    for n in spec.task_nodes():
        if n.mapping and n.mapping.expand_kwargs is not None:
            ek = n.mapping.expand_kwargs
            if not (isinstance(ek, list) or (isinstance(ek, dict) and ek.get("$type") == "xcom")):
                problems.append(
                    _err(
                        "mapping",
                        "expand_kwargs must be a list of dicts or an XCom reference",
                        n.id,
                        "mapping.expand_kwargs",
                    )
                )
        if n.mapping:
            for name, value in n.mapping.expand.items():
                if not (
                    isinstance(value, list)
                    or (isinstance(value, dict) and value.get("$type") == "xcom")
                ):
                    problems.append(
                        _err(
                            "mapping",
                            f"expand value for {name!r} must be a list or an XCom reference",
                            n.id,
                            f"mapping.{name}",
                        )
                    )
        if isinstance(n, OperatorNode):
            for k in n.params:
                if k in _COMMON_FIELDS:
                    problems.append(
                        _err(
                            "reserved_param",
                            f"{k!r} is set via the task settings, not operator params",
                            n.id,
                            k,
                        )
                    )
            if policy.is_privileged(n.operator) and not policy.allow_privileged_operators:
                problems.append(
                    _err(
                        "policy",
                        f"{n.operator.rsplit('.', 1)[-1]} is disabled by policy",
                        n.id,
                        "operator",
                    )
                )
            if any(fnmatch.fnmatchcase(n.operator, pat) for pat in policy.forbidden_operators):
                problems.append(
                    _err(
                        "policy",
                        f"{n.operator.rsplit('.', 1)[-1]} is forbidden by policy",
                        n.id,
                        "operator",
                    )
                )
            if catalog is not None:
                mapped: Any = n.mapping.expand.keys() if n.mapping else ()
                if n.mapping and n.mapping.expand_kwargs is not None:
                    mapped = list(_catalog_param_names(catalog, n.operator))
                for field, msg in catalog.validate_params(n.operator, n.params, mapped):
                    problems.append(_err("param", msg, n.id, field))
        elif isinstance(n, PythonTaskNode):
            if not policy.allow_python_code:
                problems.append(
                    _err("policy", "Python code tasks are disabled by policy", n.id, "code")
                )
            bad = syntax.get(id(n))
            if bad is not None:
                problems.append(_err("syntax", f"line {bad.lineno}: {bad.msg}", n.id, "code"))
            else:
                for issue in lint.get(id(n), []):
                    problems.append(
                        Problem(
                            severity="error" if issue.severity == "error" else "warning",
                            code=f"lint:{issue.code}",
                            message=f"line {issue.line}: {issue.message}",
                            node_id=n.id,
                            field="code",
                            line=issue.line,
                            column=issue.column,
                            doc_url=issue.url,
                        )
                    )
            problems += _python_decorator(spec, n)
            for name in [*n.arguments, *n.extra_parameters]:
                if name in {"self", "context"}:
                    problems.append(
                        _err(
                            "reserved_arg",
                            f"argument name {name!r} is reserved",
                            n.id,
                            f"arguments.{name}",
                        )
                    )
        elif isinstance(n, CustomNode):
            problems += _custom(n, registry)
    return problems


def _catalog_param_names(catalog: OperatorCatalog, fqn: str) -> list[str]:
    detail = catalog.get(fqn)
    if detail is None:
        return []
    return list(detail.params_schema.get("properties", {}))


def _returned_strings(code: str) -> set[str]:
    """String constants a function can *return* (branch targets), best effort."""
    try:
        tree = ast.parse(normalize_task_body(code) or "pass")
    except SyntaxError:
        return set()

    def collect(expr: ast.expr) -> set[str]:
        if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
            return {expr.value}
        if isinstance(expr, ast.IfExp):
            return collect(expr.body) | collect(expr.orelse)
        if isinstance(expr, (ast.List, ast.Tuple, ast.Set)):
            return {s for e in expr.elts for s in collect(e)}
        return set()

    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Return) and node.value is not None:
            found |= collect(node.value)
    return found


def _python_decorator(spec: DagSpec, n: PythonTaskNode) -> list[Problem]:
    problems: list[Problem] = []
    if n.decorator in ("branch", "branch_virtualenv", "branch_external_python"):
        nodes = spec.node_map()
        downstream = {
            effective_task_id(nodes[e.target], nodes)  # type: ignore[arg-type]
            for e in spec.edges
            if e.source == n.id and e.target in nodes and not isinstance(nodes[e.target], NoteNode)
        }
        if not downstream:
            problems.append(
                _err("branch", "a branch task needs at least one downstream task", n.id)
            )
        unknown = {s for s in _returned_strings(n.code) if s not in downstream}
        if downstream and unknown:
            problems.append(
                _err(
                    "branch",
                    f"branch returns {sorted(unknown)} which are not directly downstream "
                    f"(expected one of {sorted(downstream)})",
                    n.id,
                    "code",
                    severity="warning",
                )
            )
    if (
        n.decorator in ("virtualenv", "branch_virtualenv")
        and "requirements" not in n.decorator_kwargs
    ):
        problems.append(
            _err(
                "decorator",
                "@task.virtualenv usually needs `requirements`",
                n.id,
                "decorator_kwargs",
                severity="info",
            )
        )
    if (
        n.decorator in ("external_python", "branch_external_python")
        and "python" not in n.decorator_kwargs
    ):
        problems.append(
            _err(
                "decorator",
                f"@task.{n.decorator} needs `python` (interpreter path)",
                n.id,
                "decorator_kwargs",
            )
        )
    if n.decorator in ("docker", "kubernetes") and "image" not in n.decorator_kwargs:
        problems.append(
            _err("decorator", f"@task.{n.decorator} needs `image`", n.id, "decorator_kwargs")
        )
    return problems


def _iter_assets(expr: AssetExpr) -> list[AssetRef]:
    if isinstance(expr, AssetLeaf):
        return [expr.asset]
    if isinstance(expr, AssetCombination):
        return [a for item in expr.items for a in _iter_assets(item)]
    return []


def _has_and(expr: AssetExpr) -> bool:
    if isinstance(expr, AssetCombination):
        return (expr.op == "all" and len(expr.items) > 1) or any(_has_and(i) for i in expr.items)
    return False


def _schedule(spec: DagSpec) -> list[Problem]:
    problems: list[Problem] = []
    sched = spec.schedule
    if (
        isinstance(sched, (AssetOrTimeScheduleSpec, AssetAndTimeScheduleSpec))
        and isinstance(sched.time, PresetSchedule)
        and sched.time.preset in ("@once", "@continuous")
    ):
        problems.append(
            _err(
                "schedule", f"{sched.time.preset} cannot be combined with assets", field="schedule"
            )
        )
    if (
        isinstance(sched, AssetSchedule)
        and sched.batch_asset_events is False
        and _has_and(sched.expression)
    ):
        problems.append(
            _err(
                "schedule",
                "batch_asset_events=False is not allowed with AND asset conditions",
                field="schedule",
            )
        )
    assets: list[AssetRef] = []
    expr = getattr(sched, "expression", None)
    if expr is not None and not isinstance(expr, str):
        assets = _iter_assets(expr)
    for node in spec.task_nodes():
        assets += node.outlets + node.inlets
    for asset in assets:
        for w in asset.watchers:
            package = MESSAGING_SCHEME_PACKAGES.get(w.scheme)
            if importlib.util.find_spec("airflow.providers.common.messaging") is None:
                problems.append(
                    _err(
                        "watcher",
                        "event-driven scheduling needs apache-airflow-providers-common-messaging",
                        field="schedule",
                    )
                )
            elif package and not _module_available(package):
                problems.append(
                    _err(
                        "watcher",
                        f"scheme {w.scheme!r} needs the provider package for {package}",
                        field="schedule",
                    )
                )
    uses_lineage = spec.lineage != "inherit" or any(
        t.lineage != "inherit" for t in spec.task_nodes(include_disabled=False)
    )
    if uses_lineage and not _module_available("airflow.providers.openlineage"):
        problems.append(
            _err(
                "lineage",
                "lineage settings need apache-airflow-providers-openlineage",
                field="lineage",
            )
        )
    if (
        spec.start_date
        and getattr(sched, "type", None) in ("cron", "interval", "multi_cron")
        and spec.catchup
        and spec.max_active_runs is None
    ):
        problems.append(
            _err(
                "catchup",
                "catchup=True without max_active_runs may create many runs at once",
                field="catchup",
                severity="info",
            )
        )
    return problems


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _callback_problems(
    refs: list[CallbackRef], where: str, node_id: str | None, catalog: OperatorCatalog | None
) -> list[Problem]:
    problems: list[Problem] = []
    notifier_get = getattr(catalog, "get_notifier", None) if catalog is not None else None
    validate = getattr(catalog, "validate_notifier_params", None) if catalog is not None else None
    for ref in refs:
        if notifier_get is not None and notifier_get(ref.notifier) is None:
            problems.append(
                _err("callback", f"notifier {ref.notifier!r} is not in the catalog", node_id, where)
            )
        elif validate is not None:
            for fld, msg in validate(ref.notifier, ref.params):
                problems.append(_err("callback", msg, node_id, f"{where}.{fld}" if fld else where))
    return problems


def _behaviours(spec: DagSpec, catalog: OperatorCatalog | None) -> list[Problem]:
    problems: list[Problem] = []
    for name in ("on_retry", "on_execute", "on_skipped"):
        if getattr(spec.callbacks, name):
            problems.append(
                _err(
                    "callback",
                    f"DAG-level {name} callbacks are not supported",
                    field=f"callbacks.{name}",
                )
            )
    for name in ("on_success", "on_failure"):
        problems += _callback_problems(
            getattr(spec.callbacks, name), f"callbacks.{name}", None, catalog
        )
    for i, d in enumerate(spec.deadlines):
        problems += _callback_problems([d.callback], f"deadlines.{i}.callback", None, catalog)
    edges = effective_edges(spec)
    nodes = spec.node_map()
    for n in spec.task_nodes(include_disabled=False):
        for name in ("on_success", "on_failure", "on_retry", "on_execute", "on_skipped"):
            problems += _callback_problems(
                getattr(n.callbacks, name), f"callbacks.{name}", n.id, catalog
            )
        if n.is_teardown and n.trigger_rule == "always":
            problems.append(
                _err(
                    "teardown",
                    "teardown tasks cannot use trigger rule 'always'",
                    n.id,
                    "trigger_rule",
                )
            )
        if n.is_teardown and not any(
            e.target == n.id and getattr(nodes.get(e.source), "is_setup", False) for e in edges
        ):
            problems.append(
                _err(
                    "teardown",
                    "teardown has no upstream setup task; connect it to the setup it cleans up",
                    n.id,
                    severity="warning",
                )
            )
        if n.retry_policy and n.retries == 0:
            problems.append(
                _err(
                    "retry_policy",
                    "retry policy has no effect with retries=0",
                    n.id,
                    "retry_policy",
                    severity="warning",
                )
            )
    return problems


def _templates(
    spec: DagSpec, catalog: OperatorCatalog | None, policy: Policy = DEFAULT_POLICY
) -> list[Problem]:
    problems: list[Problem] = []
    declared = list(spec.params)
    time_available = spec.schedule.type not in ("assets", "partitioned_assets")
    for n in spec.task_nodes(include_disabled=False):
        templated: set[str] | None = None
        values: dict[str, Any] = {}
        if isinstance(n, OperatorNode):
            values = dict(n.params)
            if catalog is not None:
                detail = catalog.get(n.operator)
                templated = set(detail.template_fields) if detail else None
        elif isinstance(n, PythonTaskNode):
            values = dict(n.arguments)
        for name, value in values.items():
            if templated is not None and name not in templated:
                if isinstance(value, str) and looks_like_template(value):
                    problems.append(
                        _err(
                            "template",
                            f"{name!r} is not a templated field; "
                            "the Jinja expression will not be rendered",
                            n.id,
                            name,
                            severity="warning",
                        )
                    )
                continue
            for text in _strings(value):
                if not looks_like_template(text):
                    continue
                for issue in check_template(
                    text,
                    declared_params=declared,
                    time_available=time_available,
                    extra_names=policy.jinja_names,
                ):
                    sev: Severity = "error" if issue.severity == "error" else "warning"
                    problems.append(_err("template", issue.message, n.id, name, severity=sev))
    return problems


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        if "$type" in value:
            return []
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


def _loops(spec: DagSpec, policy: Policy = DEFAULT_POLICY) -> list[Problem]:
    """Task loop rules (Airflow 3.4): one terminal task, no nested groups, no outside wiring
    into the body (connect to the group instead)."""
    problems: list[Problem] = []
    nodes = spec.node_map()
    loops = {g.id: g for g in spec.group_nodes() if g.loop is not None}
    for n in spec.task_nodes(include_disabled=False):
        if isinstance(n, PythonTaskNode):
            if n.uses_loop_context and n.parent_group not in loops:
                problems.append(
                    _err(
                        "loop",
                        "`loop` context is only available inside a loop group",
                        n.id,
                        "uses_loop_context",
                        severity="warning",
                    )
                )
            if n.decorator == "stub" and "queue" not in n.decorator_kwargs:
                problems.append(
                    _err(
                        "decorator",
                        "@task.stub needs a `queue` routed to a language SDK coordinator",
                        n.id,
                        "decorator_kwargs",
                    )
                )
            if n.is_result and n.decorator not in ("task",):
                problems.append(
                    _err("result", "@result applies to plain @task functions", n.id, "is_result")
                )
    if not loops:
        return problems
    edges = effective_edges(spec)
    for gid, group in loops.items():
        assert group.loop is not None
        members = {t.id for t in spec.task_nodes(include_disabled=False) if t.parent_group == gid}
        if any(g.parent_group == gid for g in spec.group_nodes()):
            problems.append(_err("loop", "loop groups cannot contain nested task groups", gid))
        if not members:
            problems.append(_err("loop", "a loop group needs at least one task", gid))
            continue
        teardown = {m for m in members if getattr(nodes[m], "is_teardown", False)}
        terminal = [
            m
            for m in members - teardown
            if not any(e.source == m and e.target in members for e in edges)
        ]
        if len(terminal) != 1:
            names = sorted(getattr(nodes[m], "task_id", m) for m in terminal)
            problems.append(
                _err(
                    "loop",
                    "the loop body must end in exactly one terminal task "
                    f"(found {names}); combine branches into one task",
                    gid,
                )
            )
        for e in edges:
            if (e.source in members) != (e.target in members) and gid not in (e.source, e.target):
                problems.append(
                    _err(
                        "loop",
                        "connect outside tasks to the loop group, not to tasks inside it",
                        e.target if e.target in members else e.source,
                    )
                )
        for t in spec.task_nodes(include_disabled=False):
            if t.id in members:
                continue
            if any(ref in members for ref in _xcom_refs(t)):
                problems.append(
                    _err("loop", "tasks outside a loop cannot read XComs of tasks inside it", t.id)
                )
        if group.loop.until_code and not policy.allow_python_code:
            # The until condition is Python emitted into the DAG, like a @task body.
            problems.append(
                _err(
                    "policy",
                    "loop until-code is Python and needs allow_python_code",
                    gid,
                    "loop.until_code",
                )
            )
        if group.loop.until_code:
            try:
                ast.parse(group.loop.until_code)
            except SyntaxError as exc:
                problems.append(
                    _err("syntax", f"until: line {exc.lineno}: {exc.msg}", gid, "loop.until_code")
                )
    return problems


def _python_refs(spec: DagSpec, policy: Policy) -> list[Problem]:
    """``{"$type": "import"}`` values: valid path, allowed by policy, resolvable statically."""
    problems: list[Problem] = []
    owners: list[tuple[str | None, Any]] = [
        (None, spec.model_dump(by_alias=True, exclude={"nodes"}))
    ]
    owners += [(n.id, n.model_dump(by_alias=True)) for n in spec.nodes]
    for node_id, data in owners:
        for raw in iter_python_refs(data):
            try:
                ref = PythonRef.model_validate(raw)
            except ValidationError:
                problems.append(
                    _err("python_ref", f"invalid Python reference {raw.get('path')!r}", node_id)
                )
                continue
            if not policy.allow_python_code:
                problems.append(
                    _err(
                        "policy",
                        f"Python reference {ref.path} needs Python code to be allowed "
                        "(allow_python_code)",
                        node_id,
                    )
                )
                continue
            if not any(fnmatch.fnmatchcase(ref.path, pat) for pat in policy.python_ref_allow):
                problems.append(
                    _err("policy", f"Python reference {ref.path} is not allowed by policy", node_id)
                )
                continue
            missing = _unresolvable(ref)
            if missing:
                problems.append(_err("python_ref", missing, node_id, severity="warning"))
    return problems


# The built-in secret-assignment pattern (AUTOAIR_POLICY__SECRET_ASSIGNMENT_PATTERN default).
_SECRET_ASSIGN = re.compile(DEFAULT_SENSITIVE_ASSIGNMENT)


def _hardcoded_secrets(spec: DagSpec, policy: Policy = DEFAULT_POLICY) -> list[Problem]:
    """Literal passwords/tokens in DAG files are readable by everyone with repo access."""
    problems: list[Problem] = []

    def assigns_secret(text: str) -> bool:
        return _matches(policy.secret_assignment_patterns, text)

    def secret_key(key: str) -> bool:
        return _matches(policy.secret_param_patterns, key, re.I) and not _matches(
            policy.secret_param_exclude_patterns, key, re.I
        )

    def walk(value: Any, key: str, node_id: str | None, field: str) -> None:
        if isinstance(value, dict):
            if "$type" in value:
                text = value.get("source") or value.get("content")
                if (
                    value["$type"] in ("function", "file")
                    and isinstance(text, str)
                    and assigns_secret(text)
                ):
                    problems.append(
                        _err(
                            "secret",
                            "the inline code assigns what looks like a hard-coded secret; "
                            "read it from a Connection or a Variable instead",
                            node_id,
                            field.split(".", 1)[0] or None,
                            severity="warning",
                        )
                    )
                return
            for k, v in value.items():
                walk(v, str(k), node_id, f"{field}.{k}" if field else str(k))
        elif isinstance(value, list):
            for v in value:
                walk(v, key, node_id, field)
        elif isinstance(value, str) and len(value) >= 6 and "{{" not in value and secret_key(key):
            problems.append(
                _err(
                    "secret",
                    f"{key!r} looks like a hard-coded secret: DAG files are readable by anyone "
                    "with access to the repository. Use an Airflow Connection or "
                    "{{ var.value.<name> }} / {{ conn.<id>.password }} instead.",
                    node_id,
                    field.split(".", 1)[0] or None,
                    severity="warning",
                )
            )

    for n in spec.task_nodes():
        if isinstance(n, OperatorNode):
            walk(n.params, "", n.id, "")
        elif isinstance(n, PythonTaskNode):
            walk(n.arguments, "", n.id, "")
            if assigns_secret(n.code or ""):
                problems.append(
                    _err(
                        "secret",
                        "the code assigns what looks like a hard-coded secret; read it from a "
                        "Connection (BaseHook.get_connection) or a Variable instead",
                        n.id,
                        "code",
                        severity="warning",
                    )
                )
    return problems


def _inline_code(spec: DagSpec, policy: Policy) -> list[Problem]:
    """Functions and scripts written in the builder: privileged, and must parse."""
    from autoair.codegen.context import CodegenError, parse_inline_function

    problems: list[Problem] = []
    owners: list[tuple[str | None, Any]] = [
        (None, spec.model_dump(by_alias=True, exclude={"nodes"})),
        *((n.id, n.model_dump(by_alias=True)) for n in spec.nodes),
    ]
    for node_id, data in owners:
        for raw in iter_tagged(data, "function"):
            if not policy.allow_python_code:
                problems.append(
                    _err("policy", "inline Python functions need allow_python_code", node_id)
                )
                continue
            try:
                parse_inline_function(str(raw.get("source", "")))
            except CodegenError as exc:
                problems.append(_err("python_function", str(exc), node_id))
        for raw in iter_tagged(data, "file"):
            try:
                asset = FileAsset.model_validate(raw)
            except ValidationError as exc:
                msg = exc.errors()[0]["msg"] if exc.errors() else str(exc)
                problems.append(_err("script_file", f"invalid script file: {msg}", node_id))
                continue
            if not policy.allow_python_code:
                problems.append(
                    _err("policy", f"script file {asset.name} needs allow_python_code", node_id)
                )
            elif asset.name.endswith(".py"):
                try:
                    ast.parse(asset.content)
                except SyntaxError as exc:
                    problems.append(
                        _err(
                            "script_file",
                            f"{asset.name}: line {exc.lineno}: {exc.msg}",
                            node_id,
                            severity="warning",
                        )
                    )
    return problems


def _unresolvable(ref: PythonRef) -> str | None:
    """Static check (no import): the module exists and defines (or imports) the name."""
    from autoair.catalog.static import find_module_file

    path = find_module_file(ref.module)
    if path is None:
        return f"cannot find module {ref.module!r} for {ref.path} (not installed / not on path?)"
    try:
        tree = ast.parse(path.read_bytes())
    except (OSError, SyntaxError, ValueError):
        return None
    names: set[str] = set()
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(stmt.name)
        elif isinstance(stmt, ast.Assign):
            names.update(t.id for t in stmt.targets if isinstance(t, ast.Name))
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            names.add(stmt.target.id)
        elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
            names.update((a.asname or a.name).split(".")[0] for a in stmt.names)
    if "__getattr__" in names or any(  # lazy modules / star imports: cannot tell statically
        isinstance(stmt, ast.ImportFrom) and any(a.name == "*" for a in stmt.names)
        for stmt in tree.body
    ):
        return None
    if ref.name not in names:
        return f"{ref.module!r} does not define {ref.name!r}"
    return None


def _policy(spec: DagSpec, policy: Policy) -> list[Problem]:
    problems: list[Problem] = []
    tasks = spec.task_nodes(include_disabled=False)
    if policy.require_owner and not spec.default_args.owner and any(not t.owner for t in tasks):
        problems.append(
            _err("policy", "an owner is required (default_args.owner)", field="default_args.owner")
        )
    if policy.require_description and not (spec.description or spec.doc_md):
        problems.append(_err("policy", "a description is required", field="description"))
    if policy.min_retries is not None:
        default = spec.default_args.retries
        for t in tasks:
            retries = t.retries if t.retries is not None else default
            if (retries or 0) < policy.min_retries:
                problems.append(
                    _err("policy", f"retries must be ≥ {policy.min_retries}", t.id, "retries")
                )
    missing = [tag for tag in policy.required_tags if tag not in spec.tags]
    if missing:
        problems.append(_err("policy", f"required tags missing: {missing}", field="tags"))
    if policy.max_tasks is not None and len(tasks) > policy.max_tasks:
        problems.append(_err("policy", f"at most {policy.max_tasks} tasks are allowed"))
    if policy.team_dag_id_prefix and spec.team and not spec.dag_id.startswith(f"{spec.team}_"):
        problems.append(
            _err("policy", f"dag_id must start with the team prefix '{spec.team}_'", field="dag_id")
        )
    if policy.dag_id_prefix and not spec.dag_id.startswith(policy.dag_id_prefix):
        problems.append(
            _err("policy", f"dag_id must start with {policy.dag_id_prefix!r}", field="dag_id")
        )
    for regex in _regexes(tuple(policy.dag_id_patterns)):
        if not regex.fullmatch(spec.dag_id):
            problems.append(_err("policy", f"dag_id must match {regex.pattern!r}", field="dag_id"))
    if policy.task_id_patterns:
        nodes: dict[str, object] = {n.id: n for n in spec.nodes}
        for t in tasks:
            task_id = effective_task_id(t, nodes).rsplit(".", 1)[-1]
            for regex in _regexes(tuple(policy.task_id_patterns)):
                if not regex.fullmatch(task_id):
                    problems.append(
                        _err("policy", f"task_id must match {regex.pattern!r}", t.id, "task_id")
                    )
    return problems


def _organisation_rules(
    spec: DagSpec,
    policy: Policy,
    environment: str | None,
    target_version: tuple[int, int, int] | None,
    context: Mapping[str, Any] | None,
) -> list[Problem]:
    """``policy.rules`` and the optional OPA decision (docs/policy.md)."""
    problems: list[Problem] = []
    if policy.rules:
        from autoair.validation.policy_rules import evaluate_rules

        problems += evaluate_rules(spec, policy.rules, environment)
    if policy.opa is not None:
        from autoair import __version__
        from autoair.validation.opa import opa_problems

        version = target_version or airflow_version()
        facts: dict[str, Any] = {
            "environment": environment,
            "team": spec.team,
            "autoair_version": __version__,
            "airflow_version": format_version(version) if version else None,
            **(context or {}),
        }
        problems += opa_problems(spec, facts, policy.opa)
    return problems


def _versions(spec: DagSpec, version: tuple[int, int, int] | None) -> list[Problem]:
    if version is None:
        return []
    return [
        _err(
            "airflow_version",
            f"{f.title} requires Airflow ≥ {format_version(f.min_version)} "
            f"(target is {format_version(version)})",
            f.node_id,
        )
        for f in required_features(spec)
        if version < f.min_version
    ]


def _custom(n: CustomNode, registry: HandlerRegistry) -> list[Problem]:
    if n.handler not in registry:
        return [
            _err(
                "unknown_handler", f"no extension provides node kind {n.handler!r}", n.id, "handler"
            )
        ]
    if n.mapping:
        return [_err("mapping", "custom nodes do not support dynamic mapping", n.id)]
    handler = registry.get(n.handler)
    try:
        config = handler.config_model.model_validate(n.config)
    except Exception as exc:  # pydantic.ValidationError
        return [
            _err(
                "config", str(exc).splitlines()[0] if str(exc) else "invalid config", n.id, "config"
            )
        ]
    return [_err("config", msg, n.id, "config") for msg in handler_problems(handler, n, config)]


__all__ = [
    "MISSING_VALUE",
    "PRIVILEGED_OPERATORS",
    "Policy",
    "Problem",
    "TemplateLock",
    "TemplateLookup",
    "ValidationReport",
    "effective_task_id",
    "template_field_value",
    "template_problems",
    "validate_spec",
]
