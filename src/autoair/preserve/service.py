"""What a deploy writes for a Python DAG: the generated file, or the existing file patched.

:func:`plan_source` is called by the deploy route, the source preview route and the MCP
tools with the spec to deploy and the file currently in the target (if any). It

1. classifies that file (:func:`classify`): *absent*, *clean* (exactly what AutoAir wrote,
   or the hand-written file the spec was imported from), *ejected* (changed since) or
   *foreign* (a hand-written file AutoAir never wrote);
2. resolves the source policy (:func:`resolve_policy`: the spec's ``source.policy``,
   ``auto`` -> preserve for hand-written DAGs, the admin switches and allowed policies);
3. for ``generate`` returns the generated file -- or *blocks* when that would overwrite an
   ejected or foreign file (``preserve.ejected_files``);
4. for ``preserve`` / ``strict-preserve`` picks the file to patch (the target's, or the
   hand-written ``source.code`` when it is newer), imports it in a sandboxed subprocess,
   generates the code of both the imported and the intended spec, patches the difference
   into the file (:mod:`autoair.preserve.patcher`) and verifies the result with a round
   trip: the patched file must import as exactly the intended spec, otherwise the patch is
   rejected. When the target file was changed outside AutoAir, the external change and the
   user's change are merged first (three-way, :func:`autoair.merge.merge_specs`).

A refused patch never falls back to regenerating the file: the plan says why and offers the
explicit alternatives (switch to ``generate``, keep the DAG as raw code).
"""

from __future__ import annotations

import contextlib
import hashlib
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, PrivateAttr

from autoair.preserve import fingerprint
from autoair.preserve.config import ConcretePolicy, PreserveSettings
from autoair.preserve.patcher import PatchIssue, patch_source, unified_diff
from autoair.spec import DagSource, DagSpec
from autoair.storage.base import ExistingFile, is_owned, sha256_text

TargetState = Literal["absent", "clean", "hand_written", "ejected", "foreign"]
PlanAction = Literal["generate", "preserve", "unchanged", "blocked", "refused", "not_applicable"]
PlanOption = Literal["preserve", "overwrite", "generate", "raw"]
Resolution = Literal["preserve", "overwrite"]


class SourceIssue(BaseModel):
    code: str = Field(description="unmappable, unsupported, risky, conflict, roundtrip, ...")
    message: str
    task_id: str | None = None


class SourcePlan(BaseModel):
    """What a deploy writes as the DAG file and why (deploy result, source preview)."""

    applicable: bool = Field(
        default=True,
        description="False for output modes other than python and for raw code DAGs (the "
        "source policy does not apply to them).",
    )
    requested: str = Field(default="auto", description="The spec's source.policy.")
    policy: ConcretePolicy = "generate"
    action: PlanAction = "generate"
    target_state: TargetState = "absent"
    target_file: str | None = None
    base: Literal["target", "source"] | None = Field(
        default=None,
        description="The file a preserve deploy patched: the target's file or the "
        "hand-written source.code of the spec.",
    )
    diff: str = Field(default="", description="Unified diff of the file in the target -> new.")
    diff_truncated: bool = False
    operations: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    refusals: list[SourceIssue] = Field(default_factory=list)
    options: list[PlanOption] = Field(
        default_factory=list,
        description="What the user can do next: preserve (apply the change to the file in "
        "the target), overwrite (needs confirmation, 'deploy:overwrite_ejected' and "
        "preserve.allow_force_overwrite), "
        "generate (switch the DAG's source policy), raw (keep the file as a raw code DAG).",
    )
    message: str = ""
    target_sha: str | None = Field(
        default=None,
        description="sha256 of the DAG file in the target the plan was made from (empty: no "
        "file). Send it back as source_target_sha to deploy exactly what was previewed.",
    )
    content_hidden: bool = Field(
        default=False,
        description="The file in the target is a hand-written file AutoAir never wrote: its "
        "content (diff, file, current code) is not shown.",
    )
    content: str | None = Field(
        default=None,
        description="Source preview only: the complete DAG file the deploy would write (the "
        "code mode shows it for DAGs that keep their hand-written file).",
    )
    current_code: str | None = Field(
        default=None,
        description="The DAG file the change could not be applied to (the target's file or "
        "the spec's hand-written code), when the plan offers to keep it as a raw code DAG.",
    )
    # What the deploy writes: kept off the API (the diff shows the change).
    _source: str | None = PrivateAttr(default=None)
    _expected_sha: str | None = PrivateAttr(default=None)
    _overwrite: bool = PrivateAttr(default=False)
    _stored_spec: DagSpec | None = PrivateAttr(default=None)
    _uses_source_code: bool = PrivateAttr(default=False)
    _hand_written: bool = PrivateAttr(default=False)

    @property
    def hand_written(self) -> bool:
        """The written file is a hand-written one (not a self-verifying generated file)."""
        return self._hand_written

    @hand_written.setter
    def hand_written(self, value: bool) -> None:
        self._hand_written = value

    @property
    def source(self) -> str | None:
        """The content of the DAG file to write (None: nothing is written)."""
        return self._source

    @source.setter
    def source(self, value: str | None) -> None:
        self._source = value

    @property
    def expected_sha(self) -> str | None:
        """The write is conditional on the target's file still having this hash."""
        return self._expected_sha

    @expected_sha.setter
    def expected_sha(self, value: str | None) -> None:
        self._expected_sha = value

    @property
    def overwrite(self) -> bool:
        """A confirmed overwrite of a file changed outside AutoAir."""
        return self._overwrite

    @overwrite.setter
    def overwrite(self, value: bool) -> None:
        self._overwrite = value

    @property
    def stored_spec(self) -> DagSpec | None:
        """The spec to store instead of the one sent (merged with outside changes)."""
        return self._stored_spec

    @stored_spec.setter
    def stored_spec(self, value: DagSpec | None) -> None:
        self._stored_spec = value

    @property
    def uses_source_code(self) -> bool:
        """The written file is the hand-written code sent with the spec (code:raw)."""
        return self._uses_source_code

    @uses_source_code.setter
    def uses_source_code(self, value: bool) -> None:
        self._uses_source_code = value


def classify(existing: ExistingFile | None, spec: DagSpec) -> TargetState:
    if existing is None:
        return "absent"
    sha = sha256_text(existing.content)
    if existing.recorded_sha is not None and sha == existing.recorded_sha:
        # What AutoAir wrote last; a hand-written file it keeps is never regenerated
        # without the overwrite permission.
        return "hand_written" if existing.hand_written else "clean"
    fp = fingerprint.state(existing.content)
    if fp == "valid":
        return "clean"
    if fp == "mismatch" or existing.recorded_sha is not None:
        return "ejected"
    if not is_owned(existing.content):
        return "foreign"
    return "clean"  # an AutoAir file from before fingerprints, without target state


def applicable(spec: DagSpec) -> bool:
    return spec.output_mode == "python" and spec.raw_code is None


def resolve_policy(
    spec: DagSpec,
    cfg: PreserveSettings,
    *,
    preserve_enabled: bool,
    force_strict: bool,
) -> tuple[ConcretePolicy, str | None]:
    """``(policy, problem)``: the effective policy; ``problem`` when the spec asks for a
    policy the organisation does not allow."""
    requested = spec.source.policy if spec.source else "auto"
    if requested == "auto":
        hand_written = spec.source is not None and spec.source.origin == "python"
        order: list[ConcretePolicy] = (
            ["preserve", "strict-preserve", "generate"]
            if hand_written
            else ["generate", "preserve", "strict-preserve"]
        )
        if not preserve_enabled:
            order = ["generate"]
        for candidate in order:
            if candidate == "preserve" and force_strict:
                candidate = "strict-preserve"
            if candidate in cfg.allowed_policies:
                return candidate, None
        return order[0], "no allowed source policy applies (preserve.allowed_policies)"
    policy: ConcretePolicy = requested
    if policy != "generate" and not preserve_enabled:
        return "generate", (
            "source-preserving deploys are switched off (features.source_preserve); "
            f"the DAG asks for {policy!r}"
        )
    if policy == "preserve" and force_strict:
        policy = "strict-preserve"
    if policy not in cfg.allowed_policies:
        allowed = ", ".join(cfg.allowed_policies)
        return policy, f"the source policy {policy!r} is not allowed here (allowed: {allowed})"
    return policy, None


# --------------------------------------------------------------------------- import cache

Importer = Callable[[Sequence[tuple[str, str]], float], list[Any]]


class _ImportCache:
    """Per-process cache of imports keyed by the file's hash (derived data with a TTL, so
    replicas never need to agree on it)."""

    def __init__(self, size: int = 64) -> None:
        self.size = size
        self._items: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str, ttl: float) -> Any:
        if ttl <= 0:
            return None
        with self._lock:
            item = self._items.get(key)
            if item is None or time.monotonic() - item[0] > ttl:
                return None
            self._items.move_to_end(key)
            return item[1]

    def put(self, key: str, value: Any, ttl: float) -> None:
        if ttl <= 0:
            return
        with self._lock:
            self._items[key] = (time.monotonic(), value)
            while len(self._items) > self.size:
                self._items.popitem(last=False)


_CACHE = _ImportCache()


def _default_importer(sources: Sequence[tuple[str, str]], timeout: float) -> list[Any]:
    from autoair.importer.python_dag import import_python_sources

    return import_python_sources(sources, timeout=timeout)


def folder_importer(folders: Sequence[Path]) -> Importer:
    """An importer that lets the files import from ``folders`` (a local dags folder: the
    helper modules next to the DAG files resolve, as in Airflow)."""
    from autoair.importer.python_dag import import_python_sources

    def run(sources: Sequence[tuple[str, str]], timeout: float) -> list[Any]:
        return import_python_sources(sources, timeout=timeout, pythonpath=folders)

    return run


class _Imports:
    def __init__(
        self, importer: Importer, timeout: float, ttl: float, dag_id: str, scope: str = ""
    ) -> None:
        self.importer = importer
        self.timeout = timeout
        self.ttl = ttl
        self.dag_id = dag_id
        self.scope = scope  # what else the import depends on (the import path)

    def _key(self, src: str) -> str:
        return f"{self.scope}:{self.dag_id}:{sha256_text(src)}"

    def specs(self, sources: Sequence[str]) -> list[tuple[DagSpec | None, str | None]]:
        """``(spec, error)`` of ``dag_id`` for each source (each in its own subprocess)."""
        out: list[tuple[DagSpec | None, str | None] | None] = []
        todo: list[tuple[int, str]] = []
        for i, src in enumerate(sources):
            cached = _CACHE.get(self._key(src), self.ttl)
            out.append(cached)
            if cached is None:
                todo.append((i, src))
        if todo:
            # Each file in its own interpreter: a file can only ever influence its own
            # result, so results of different files never contaminate each other (or the
            # cache), and each import gets the full timeout.
            from concurrent.futures import ThreadPoolExecutor

            def one(src: str) -> Any:
                return self.importer([(src, f"{self.dag_id}.py")], self.timeout)[0]

            with ThreadPoolExecutor(max_workers=len(todo)) as pool:
                results = list(pool.map(one, [src for _, src in todo]))
            for (i, src), result in zip(todo, results, strict=True):
                value = _pick(result, self.dag_id)
                if value[0] is not None:
                    _CACHE.put(self._key(src), value, self.ttl)
                out[i] = value
        return [v if v is not None else (None, "import failed") for v in out]


def _pick(result: Any, dag_id: str) -> tuple[DagSpec | None, str | None]:
    from autoair.importer.code_sync import short_error

    if result.import_errors:
        return None, "; ".join(short_error(e) for e in result.import_errors)[:2000]
    for dag in result.dags:
        if dag.dag_id == dag_id:
            if dag.spec is None:
                return None, dag.error or "the DAG cannot be converted"
            return dag.spec, None
    found = ", ".join(d.dag_id for d in result.dags) or "none"
    return None, f"the file does not define the DAG {dag_id!r} (found: {found})"


# --------------------------------------------------------------------------- comparison


def _canonical(spec: DagSpec) -> dict[str, Any]:
    """The importer's view of a spec, for the round-trip comparison: no source info, task
    code compared as code (formatting and comments do not matter)."""
    import ast

    data = spec.model_dump(mode="json", by_alias=True, exclude_none=True)
    data.pop("source", None)

    def functions_as_code(value: Any) -> None:
        # Inline functions (``python_callable=fn`` defined in the file) come back from
        # generated code without their comments: compare them as code too.
        if isinstance(value, list):
            for item in value:
                functions_as_code(item)
        elif isinstance(value, dict):
            source = value.get("source")
            if value.get("$type") == "function" and isinstance(source, str):
                with contextlib.suppress(SyntaxError):
                    value["source"] = ast.dump(ast.parse(source))
                return
            for item in value.values():
                functions_as_code(item)

    for node in data.get("nodes", []):
        code = node.get("code")
        if isinstance(code, str):
            with contextlib.suppress(SyntaxError):
                node["code"] = ast.dump(ast.parse(code))
        functions_as_code(node.get("params"))
    data["edges"] = sorted(data.get("edges", []), key=lambda e: (e["source"], e["target"]))
    data["nodes"] = sorted(data.get("nodes", []), key=lambda n: n["id"])
    return data


def _differences(a: DagSpec, b: DagSpec, limit: int = 8) -> list[str]:
    from autoair.merge import diff_specs

    ca, cb = DagSpec.model_validate(_canonical(a)), DagSpec.model_validate(_canonical(b))
    d = diff_specs(ca, cb)
    out = [f"DAG {c.path}" for c in d.changed_fields]
    out += [f"task {n.get('task_id') or n.get('group_id')}: missing" for n in d.added_nodes]
    out += [f"task {n.get('task_id') or n.get('group_id')}: unexpected" for n in d.removed_nodes]
    names = {n.id: getattr(n, "task_id", None) or n.id for n in ca.nodes}
    for n in d.changed_nodes:
        out += [f"task {names.get(n.id, n.id)}: {c.path}" for c in n.changes]
    out += [f"missing edge {e.source} -> {e.target}" for e in d.added_edges]
    out += [f"unexpected edge {e.source} -> {e.target}" for e in d.removed_edges]
    return out[:limit]


_CANVAS_FIELDS = ("position", "label", "icon", "template_ref")
_AUTOAIR_FIELDS = ("output_mode", "team", "environments", "source")


def _rekeyed(spec: DagSpec) -> dict[str, Any]:
    """``spec`` in the importer's view: node ids as the importer derives them from task and
    group ids, no canvas-only fields, notes or AutoAir-only DAG fields."""
    keys = _task_keys(spec)
    groups = _group_keys(spec)

    def node_id(prefix: str, key: str) -> str:
        return f"{prefix}_{hashlib.sha1(key.encode()).hexdigest()[:8]}"  # noqa: S324 - ids only

    ids = {nid: node_id("t", key) for nid, key in keys.items()}
    ids |= {nid: node_id("g", key) for nid, key in groups.items()}
    data = _canonical(spec)
    for field_name in _AUTOAIR_FIELDS:
        data.pop(field_name, None)
    nodes = []
    for node in data.get("nodes", []):
        if node.get("kind") == "note":
            continue
        for f in _CANVAS_FIELDS:
            node.pop(f, None)
        node["id"] = ids.get(node["id"], node["id"])
        if node.get("parent_group"):
            node["parent_group"] = ids.get(node["parent_group"], node["parent_group"])
        nodes.append(_remap_refs(node, ids))
    data["nodes"] = sorted(nodes, key=lambda n: n["id"])
    edges = [
        {
            **e,
            "source": ids.get(e["source"], e["source"]),
            "target": ids.get(e["target"], e["target"]),
        }
        for e in data.get("edges", [])
    ]
    for e in edges:
        e.pop("id", None)
    data["edges"] = sorted(edges, key=lambda e: (e["source"], e["target"]))
    return data


def _remap_refs(value: Any, ids: dict[str, str]) -> Any:
    if isinstance(value, dict):
        if value.get("$type") == "xcom" and isinstance(value.get("node"), str):
            value = {**value, "node": ids.get(value["node"], value["node"])}
        return {k: _remap_refs(v, ids) for k, v in value.items()}
    if isinstance(value, list):
        return [_remap_refs(v, ids) for v in value]
    return value


def _flat(a: Any, b: Any, path: str, out: dict[str, Any]) -> None:
    if a == b:
        return
    if isinstance(a, dict) and isinstance(b, dict):
        for key in a.keys() | b.keys():
            _flat(a.get(key), b.get(key), f"{path}.{key}" if path else str(key), out)
        return
    out[path] = b


def _by_id(data: dict[str, Any]) -> dict[str, Any]:
    view = {k: v for k, v in data.items() if k not in ("nodes", "edges")}
    view["nodes"] = {n["id"]: n for n in data.get("nodes", [])}
    view["edges"] = {f"{e['source']}->{e['target']}": e for e in data.get("edges", [])}
    return view


def _change_mismatch(old: DagSpec, got: DagSpec, intended: DagSpec) -> list[str]:
    """Where the change from ``old`` to ``got`` (the patched file) differs from the change
    from ``old`` to ``intended``, in the importer's view."""
    base = _by_id(_rekeyed(old))
    expected: dict[str, Any] = {}
    actual: dict[str, Any] = {}
    _flat(base, _by_id(_rekeyed(intended)), "", expected)
    _flat(base, _by_id(_rekeyed(got)), "", actual)
    out = []
    for path in sorted(expected.keys() | actual.keys()):
        if expected.get(path, _MISSING) != actual.get(path, _MISSING):
            out.append(path)
    return out


_MISSING = object()


def _group_keys(spec: DagSpec) -> dict[str, str]:
    """group node id -> Airflow group id (parent prefixes applied)."""
    groups = {g.id: g for g in spec.group_nodes()}
    out: dict[str, str] = {}

    def full(gid: str) -> str:
        if gid in out:
            return out[gid]
        g = groups[gid]
        parent = groups.get(g.parent_group) if g.parent_group else None
        key = g.group_id
        if parent is not None and parent.prefix_group_id:
            key = f"{full(parent.id)}.{g.group_id}"
        out[gid] = key
        return key

    for gid in groups:
        full(gid)
    return out


def _task_keys(spec: DagSpec) -> dict[str, str]:
    """node id -> Airflow task id (group prefixes applied, like TaskGroup.child_id)."""
    groups = {g.id: g for g in spec.group_nodes()}
    full = _group_keys(spec)
    out: dict[str, str] = {}
    for node in spec.task_nodes():
        group = groups.get(node.parent_group) if node.parent_group else None
        if group is not None and group.prefix_group_id:
            out[node.id] = f"{full[group.id]}.{node.task_id}"
        else:
            out[node.id] = node.task_id
    return out


def _renames(before: DagSpec | None, after: DagSpec) -> dict[str, str]:
    if before is None:
        return {}
    old, new = _task_keys(before), _task_keys(after)
    return {old[i]: new[i] for i in old.keys() & new.keys() if old[i] != new[i]}


def _strip_source(spec: DagSpec) -> DagSpec:
    return spec.model_copy(update={"source": None}) if spec.source is not None else spec


def mark_hand_written(
    spec: DagSpec,
    code: str | None,
    warnings: list[str] | None = None,
    *,
    edited_from: DagSource | None = None,
) -> DagSpec:
    """``spec`` as a DAG that comes from the hand-written file ``code`` (keeps its policy).

    ``edited_from``: the source info of the document ``code`` was edited from (code mode):
    its policy and ``base_sha`` are kept, so the edited code is told apart from the file it
    started from. ``code`` larger than a raw code DAG may be is not kept (a warning is added
    to ``warnings``); deploys then patch the target's file."""
    from autoair.spec.models import RAW_CODE_MAX_CHARS

    known = edited_from or spec.source
    policy = known.policy if known is not None else "auto"
    if code is not None and len(code) > RAW_CODE_MAX_CHARS:
        code = None
        if warnings is not None:
            warnings.append(
                "the file is too large to keep in the spec: deploys patch the file in the "
                "deploy target instead (or generate the file when the target has none)"
            )
    if edited_from is not None:
        base_sha = edited_from.base_sha
    else:
        base_sha = sha256_text(code) if code is not None else None
    source = DagSource(policy=policy, origin="python", code=code, base_sha=base_sha)
    return spec.model_copy(update={"source": source})


def keeps_hand_written_file(spec: DagSpec, settings: Any | None = None) -> bool:
    """The DAG's deploys patch a hand-written file instead of generating one (``settings``:
    the admin switches and allowed policies in force; defaults without)."""
    if not applicable(spec):
        return False
    cfg = getattr(settings, "preserve", None) or PreserveSettings()
    enabled = settings.feature("source_preserve") if settings is not None else True
    strict = settings.feature("strict_source_preserve") if settings is not None else False
    policy, problem = resolve_policy(spec, cfg, preserve_enabled=enabled, force_strict=strict)
    return policy != "generate" and problem is None


FORCE_OVERWRITE_OFF = (
    "overwriting a DAG file changed outside AutoAir or the hand-written file it keeps is "
    "turned off here (preserve.allow_force_overwrite): apply the change with preserve or "
    "keep the DAG as raw code"
)


def can_force_overwrite(cfg: PreserveSettings | None) -> bool:
    """The organisation allows the overwrite (force) choice at all
    (``preserve.allow_force_overwrite``); the caller also needs 'deploy:overwrite_ejected'."""
    return bool(cfg is not None and cfg.allow_force_overwrite)


# --------------------------------------------------------------------------- planning


@dataclass
class PlanContext:
    """Everything :func:`plan_source` needs besides the spec."""

    cfg: PreserveSettings
    preserve_enabled: bool
    force_strict: bool
    render: Callable[[DagSpec], str]  # spec -> generated python (no fingerprint)
    importer: Importer = _default_importer
    import_scope: str = ""  # cache scope of imports (e.g. the dags folder on the path)
    can_execute: bool = True  # the round trip imports code (code:execute)
    can_overwrite: bool = False  # deploy:overwrite_ejected


def plan_source(
    spec: DagSpec,
    *,
    effective: DagSpec | None = None,
    existing: ExistingFile | None,
    stored: DagSpec | None,
    ctx: PlanContext,
    resolution: Resolution | None = None,
    environment: str | None = None,
) -> SourcePlan:
    """The file a deploy of ``spec`` writes (see the module docstring). Blocking: imports
    run in a subprocess."""
    effective = effective or spec
    requested = spec.source.policy if spec.source else "auto"
    plan = SourcePlan(requested=requested, target_file=existing.location if existing else None)
    if not applicable(spec):
        plan.applicable = False
        plan.action = "not_applicable"
        plan.message = (
            "the source policy applies to Python output only"
            if spec.raw_code is None
            else "raw code DAGs are deployed as written"
        )
        return plan
    state = classify(existing, spec)
    plan.target_state = state
    policy, problem = resolve_policy(
        spec, ctx.cfg, preserve_enabled=ctx.preserve_enabled, force_strict=ctx.force_strict
    )
    plan.policy = policy
    if problem is not None:
        plan.action = "refused"
        plan.refusals.append(SourceIssue(code="policy", message=problem))
        plan.options = ["raw"] if policy == "generate" else ["generate", "raw"]
        plan.message = problem
        return plan
    plan.target_sha = sha256_text(existing.content) if existing is not None else ""
    if policy != "generate" and _has_blueprints(effective, stored):
        plan.action = "refused"
        plan.refusals.append(
            SourceIssue(
                code="unsupported",
                message="DAGs with Blueprint steps cannot keep a hand-written file yet: "
                "deploy them with the generate policy",
            )
        )
        out = _refused(plan, policy == "strict-preserve")
    else:
        generated = fingerprint.add(ctx.render(effective))
        if policy == "generate":
            out = _generate(
                plan,
                generated,
                existing,
                state,
                ctx,
                resolution,
                spec,
                effective,
                stored,
                environment,
            )
        else:
            out = _preserve(
                plan, spec, effective, generated, existing, state, stored, ctx, environment
            )
    if "generate" not in ctx.cfg.allowed_policies:
        out.options = [o for o in out.options if o != "generate"]
    if "raw" in out.options and out.action in ("blocked", "refused"):
        code = spec.source.code if spec.source else None
        out.current_code = (
            existing.content if existing is not None and out.base != "source" else code
        )
    if existing is not None and not _visible(existing, spec):
        # A hand-written file AutoAir never wrote (and the user did not import): its
        # content is not shown to spec writers who may not read it in Airflow.
        out.content_hidden = True
        out.diff = ""
        out.diff_truncated = False
        out.current_code = None
        _hide_details(out)
    return out


# Import and round-trip errors quote the file (tracebacks show its lines): for a hidden
# file only what failed is said, not the details.
_HIDDEN_DETAILS = {
    "import": "the DAG file does not import (details are not shown: AutoAir did not write "
    "the file in the target)",
    "roundtrip": "the patched file does not import as exactly the intended change (details "
    "are not shown: AutoAir did not write the file in the target)",
}


def _hide_details(plan: SourcePlan) -> None:
    for issue in plan.refusals:
        generic = _HIDDEN_DETAILS.get(issue.code)
        if generic is None or issue.message == generic:
            continue
        plan.message = plan.message.replace(issue.message, generic)
        issue.message = generic
    plan.warnings = [
        "round trip checked against the intended changes (the generated reference does not import)"
        if w.startswith("round trip checked against the intended changes")
        else w
        for w in plan.warnings
    ]


def _visible(existing: ExistingFile, spec: DagSpec) -> bool:
    """May the caller see the file's content? Files AutoAir wrote (spec writers saw them
    before), generated files, and the file the user imported themselves."""
    if existing.recorded_sha is not None or is_owned(existing.content):
        return True
    code = spec.source.code if spec.source else None
    return code is not None and sha256_text(code) == sha256_text(existing.content)


def _has_blueprints(*specs: DagSpec | None) -> bool:
    return any(
        getattr(node, "kind", None) == "blueprint"
        for spec in specs
        if spec is not None
        for node in spec.nodes
    )


def _diff(plan: SourcePlan, before: str, after: str, name: str, cfg: PreserveSettings) -> None:
    diff = unified_diff(before, after, name, limit=cfg.max_diff_chars)
    if len(diff) > cfg.max_diff_chars:
        plan.diff = diff[: cfg.max_diff_chars]
        plan.diff_truncated = True
    else:
        plan.diff = diff


def _generate(
    plan: SourcePlan,
    generated: str,
    existing: ExistingFile | None,
    state: TargetState,
    ctx: PlanContext,
    resolution: Resolution | None,
    spec: DagSpec,
    effective: DagSpec,
    stored: DagSpec | None,
    environment: str | None,
) -> SourcePlan:
    name = f"{spec.dag_id}.py"
    _diff(plan, existing.content if existing else "", generated, name, ctx.cfg)
    if state in ("absent", "clean"):
        plan.action = "generate"
        plan.source = generated
        plan.message = "the DAG file is generated from the spec"
        return plan
    assert existing is not None
    owned = is_owned(existing.content)
    what = {
        "ejected": "was changed outside AutoAir since it was last deployed (ejected)",
        "hand_written": "is the hand-written file AutoAir keeps for this DAG",
    }.get(state, "is a hand-written file AutoAir did not create")
    kept = state == "hand_written"
    # Every overwrite needs preserve.allow_force_overwrite. Regenerating the hand-written file
    # AutoAir keeps is a deliberate step (switch to generate): it needs the permission but
    # not preserve.ejected_files=overwrite.
    can_overwrite = can_force_overwrite(ctx.cfg) and (
        (kept and ctx.can_overwrite)
        or (owned and not kept and ctx.cfg.ejected_files == "overwrite")
    )
    if resolution == "overwrite":
        if not owned and not kept:
            plan.action = "blocked"
            plan.refusals.append(
                SourceIssue(
                    code="foreign",
                    message=f"{existing.location} {what}; AutoAir never overwrites it "
                    "(apply the change with preserve instead)",
                )
            )
            plan.options = ["preserve", "raw"]
            plan.message = plan.refusals[0].message
            return plan
        if not can_force_overwrite(ctx.cfg):
            plan.action = "blocked"
            plan.refusals.append(
                SourceIssue(code="overwrite_disabled", message=FORCE_OVERWRITE_OFF)
            )
            plan.options = ["preserve", "raw"]
            plan.message = plan.refusals[0].message
            return plan
        if not can_overwrite and not kept:
            plan.action = "blocked"
            plan.refusals.append(
                SourceIssue(
                    code="ejected",
                    message="overwriting ejected files is not allowed here "
                    f"(preserve.ejected_files={ctx.cfg.ejected_files!r})",
                )
            )
            plan.options = ["preserve", "raw"]
            plan.message = plan.refusals[0].message
            return plan
        if not ctx.can_overwrite:
            plan.action = "blocked"
            plan.refusals.append(
                SourceIssue(
                    code="permission",
                    message="overwriting a file changed outside AutoAir needs the AutoAir "
                    "permission 'deploy:overwrite_ejected'",
                )
            )
            plan.options = ["preserve", "raw"]
            plan.message = plan.refusals[0].message
            return plan
        plan.action = "generate"
        plan.source = generated
        plan.overwrite = True
        plan.hand_written = False
        plan.expected_sha = sha256_text(existing.content)
        plan.warnings.append(f"{existing.location} {what}; it is overwritten as confirmed")
        plan.message = "the DAG file is regenerated and overwrites the changed file"
        return plan
    if resolution == "preserve" or (ctx.cfg.ejected_files == "warn" and not kept):
        return _ejected_preserve(
            plan, spec, effective, existing, stored, ctx, resolution, what, environment
        )
    plan.action = "blocked"
    plan.options = ["preserve", "overwrite", "raw"] if can_overwrite else ["preserve", "raw"]
    plan.message = (
        f"{existing.location} {what}. Regenerating it would discard those changes: apply "
        "your change to the file with preserve"
        + (", overwrite it," if can_overwrite else "")
        + " or keep the DAG as a raw code DAG."
    )
    return plan


def _ejected_preserve(
    plan: SourcePlan,
    spec: DagSpec,
    effective: DagSpec,
    existing: ExistingFile,
    stored: DagSpec | None,
    ctx: PlanContext,
    resolution: Resolution | None,
    what: str,
    environment: str | None,
) -> SourcePlan:
    """generate policy, ejected target: apply the change to the target file instead."""
    policy: ConcretePolicy = "strict-preserve" if ctx.force_strict else "preserve"
    if policy not in ctx.cfg.allowed_policies or not ctx.preserve_enabled:
        plan.action = "blocked"
        plan.options = ["raw"]
        plan.message = (
            f"{existing.location} {what}, and source-preserving deploys are not allowed here"
        )
        return plan
    marked = mark_hand_written(spec, spec.source.code if spec.source else None)
    marked = marked.model_copy(
        update={"source": marked.source.model_copy(update={"policy": "auto"})}  # type: ignore[union-attr]
    )
    sub = SourcePlan(
        requested=plan.requested, target_file=plan.target_file, target_sha=plan.target_sha
    )
    sub.policy = policy
    sub.target_state = plan.target_state
    # The environment overlay applies to the patched file like to a generated one.
    marked_effective = effective.model_copy(update={"source": marked.source})
    out = _preserve(
        sub,
        marked,
        marked_effective,
        None,
        existing,
        plan.target_state,
        stored,
        ctx,
        environment,
        force_target=True,
    )
    if resolution != "preserve":
        out.warnings.insert(
            0,
            f"{existing.location} {what}; the change was applied to it with preserve "
            "(preserve.ejected_files=warn)",
        )
    if out.action in ("preserve", "unchanged") and out.stored_spec is None:
        out.stored_spec = marked
    if out.action == "refused":
        if plan.target_state == "hand_written":
            overwrite = ctx.can_overwrite
        else:
            overwrite = is_owned(existing.content) and ctx.cfg.ejected_files == "overwrite"
        overwrite = overwrite and can_force_overwrite(ctx.cfg)
        out.options = ["raw", "overwrite"] if overwrite else ["raw"]
    return out


def _preserve(
    plan: SourcePlan,
    spec: DagSpec,
    effective: DagSpec,
    generated: str | None,
    existing: ExistingFile | None,
    state: TargetState,
    stored: DagSpec | None,
    ctx: PlanContext,
    environment: str | None,
    *,
    force_target: bool = False,
) -> SourcePlan:
    name = f"{spec.dag_id}.py"
    strict = plan.policy == "strict-preserve"
    code = spec.source.code if spec.source else None
    # The file to patch: the hand-written source.code when it is newer than what was
    # deployed (an import, an edit in the code mode), else the target's file.
    base_text: str | None
    if existing is None:
        base_text, plan.base = code, ("source" if code is not None else None)
    elif force_target or state in ("ejected", "foreign"):
        base_text, plan.base = existing.content, "target"
    elif code is not None and sha256_text(code) not in (
        existing.source_sha,
        sha256_text(existing.content),
    ):
        base_text, plan.base = code, "source"
    else:
        base_text, plan.base = existing.content, "target"
    plan.uses_source_code = plan.base == "source"
    from autoair.spec.models import RAW_CODE_MAX_CHARS

    if base_text is not None and len(base_text) > RAW_CODE_MAX_CHARS:
        return _refuse(
            plan,
            "unsupported",
            f"the DAG file is larger than {RAW_CODE_MAX_CHARS} characters: it is not patched",
            strict,
        )
    if base_text is None:
        # Nothing hand-written to preserve yet: the first file is generated.
        generated = generated or fingerprint.add(ctx.render(effective))
        plan.action = "generate"
        plan.source = generated
        plan.message = "there is no existing file to preserve yet: the DAG file is generated"
        _diff(plan, "", generated, name, ctx.cfg)
        return plan
    if not ctx.can_execute:
        return _refuse(
            plan,
            "permission",
            "preserving the file runs a round-trip import of the DAG code, which needs the "
            "AutoAir permission 'code:execute' (for example the CodeDeployer role)",
            strict,
        )
    imports = _Imports(
        ctx.importer, ctx.cfg.import_timeout, ctx.cfg.cache_ttl, spec.dag_id, ctx.import_scope
    )
    # The target's file differs from what the user's spec was made from: merge first. A
    # hand-written file identical to the code the user imported is not an outside change.
    same_as_code = (
        code is not None
        and existing is not None
        and sha256_text(code) == sha256_text(existing.content)
    )
    ejected = state in ("ejected", "foreign") and plan.base == "target" and not same_as_code
    sources = [base_text]
    if ejected:
        if environment and spec.environments:
            return _refuse(
                plan,
                "conflict",
                "the file was changed outside AutoAir; with environment overlays the outside "
                "change cannot be merged with yours",
                strict,
            )
        # The common ancestor of the outside change and the user's: the hand-written file
        # the editor started from when it is new, else the spec deployed last.
        # Code edited in the code mode is not that file (base_sha differs): then the spec
        # deployed last is the ancestor, so the code-mode edits count as the user's change.
        recorded = existing.source_sha if existing else None
        pristine = (
            code is not None
            and spec.source is not None
            and (spec.source.base_sha == sha256_text(code))
        )
        if pristine and code is not None and sha256_text(code) != recorded:
            sources.append(code)
        elif stored is not None:
            sources.append(ctx.render(_strip_source(stored)))
        elif pristine and code is not None:
            sources.append(code)
        else:
            return _refuse(
                plan,
                "conflict",
                "the file in the target was not written from this DAG and AutoAir has no "
                "common version to merge with: import the file into AutoAir first",
                strict,
            )
        sources.append(ctx.render(_strip_source(effective)))
    results = imports.specs(sources)
    old_spec, error = results[0]
    if old_spec is None:
        return _refuse(plan, "import", f"the existing DAG file does not import: {error}", strict)
    intended = effective
    if ejected:
        from autoair.importer.code_sync import carry_canvas_state
        from autoair.merge import merge_specs

        theirs = old_spec
        base_spec, berr = results[1]
        ours, oerr = results[-1]
        if base_spec is None or ours is None:
            return _refuse(plan, "import", f"cannot merge: {berr or oerr}", strict)
        merged = merge_specs(_strip_source(base_spec), _strip_source(ours), _strip_source(theirs))
        if merged.merged is None:
            paths = ", ".join(c.path for c in merged.conflicts[:6])
            return _refuse(
                plan,
                "conflict",
                "the file was changed outside AutoAir in the same places as your change "
                f"({paths}); apply the outside change in AutoAir first",
                strict,
            )
        intended = merged.merged
        plan.stored_spec = mark_hand_written(
            carry_canvas_state(intended, spec), spec.source.code if spec.source else None
        )
        plan.warnings.append(
            "the file was changed outside AutoAir since the last deploy: your change was "
            "merged with those changes"
        )
    gen_old = ctx.render(_strip_source(old_spec))
    gen_new = ctx.render(_strip_source(intended))
    if gen_old == gen_new:
        plan.action = "unchanged"
        plan.source = base_text
        plan.hand_written = fingerprint.state(base_text) != "valid"
        plan.message = "the file already matches the spec: it is written as it is"
        _finish(plan, existing, base_text, base_text, name, ctx)
        return plan
    before = stored if stored is not None and not ejected else None
    renames = _renames(before or old_spec, intended)
    result = patch_source(
        base_text, gen_old, gen_new, dag_id=spec.dag_id, strict=strict, renames=renames
    )
    plan.operations = result.operations
    plan.warnings += result.warnings
    if not result.ok or result.source is None:
        plan.refusals = [_issue(i) for i in result.refusals]
        return _refused(plan, strict)
    # A valid fingerprint is refreshed (the file stays a self-verifying generated one); a
    # stale one is dropped: the file is now a hand-written file AutoAir keeps.
    base_fp = fingerprint.state(base_text)
    if base_fp == "valid":
        patched = fingerprint.refresh(result.source)
    elif base_fp == "mismatch":
        patched = fingerprint.strip(result.source)
    else:
        patched = result.source
    plan.hand_written = fingerprint.state(patched) != "valid"
    check = imports.specs([patched, gen_new])
    got, gerr = check[0]
    want, werr = check[1]
    if got is None:
        return _refuse(plan, "roundtrip", f"the patched file does not import: {gerr}", strict)
    if want is not None:
        mismatch = _differences(got, want) if _canonical(got) != _canonical(want) else []
        if _canonical(got) != _canonical(want) and not mismatch:
            mismatch = ["(details unavailable)"]
    else:
        # The generated reference does not import (the existing file uses something the
        # importer cannot represent, e.g. positional TaskFlow arguments): compare what the
        # patch changed with what was meant to change instead.
        mismatch = _change_mismatch(old_spec, got, intended)
        plan.warnings.append(
            "round trip checked against the intended changes (the generated reference "
            f"does not import: {werr})"
        )
    if mismatch:
        return _refuse(
            plan,
            "roundtrip",
            "the patched file does not import as exactly the intended change ("
            + "; ".join(mismatch[:8])
            + ")",
            strict,
        )
    plan.action = "preserve"
    plan.source = patched
    plan.message = (
        f"the change is applied to the existing file ({len(result.operations)} edit(s)); "
        "the rest of the file is kept as written"
    )
    _finish(plan, existing, base_text, patched, name, ctx)
    return plan


def _finish(
    plan: SourcePlan,
    existing: ExistingFile | None,
    base: str,
    content: str,
    name: str,
    ctx: PlanContext,
) -> None:
    """The diff against the file in the target (against the hand-written base when the
    target has none yet) and the hash the write is conditional on."""
    _diff(plan, existing.content if existing else base, content, name, ctx.cfg)
    if existing is not None:
        plan.expected_sha = sha256_text(existing.content)


def _issue(issue: PatchIssue) -> SourceIssue:
    return SourceIssue(code=issue.code, message=issue.message, task_id=issue.task_id)


def _refuse(plan: SourcePlan, code: str, message: str, strict: bool) -> SourcePlan:
    plan.refusals.append(SourceIssue(code=code, message=message))
    return _refused(plan, strict)


def _refused(plan: SourcePlan, strict: bool) -> SourcePlan:
    plan.action = "refused"
    plan.source = None
    plan.options = ["raw", "generate"]
    mode = "strict-preserve" if strict else "preserve"
    codes = {r.code for r in plan.refusals}
    if codes <= {"import", "permission", "conflict"} and plan.refusals:
        reason = plan.refusals[0].message
    else:
        reason = (
            f"{mode} cannot apply this change to the file without rewriting more than the "
            "change itself"
        )
    plan.message = (
        f"{reason}. Nothing was regenerated: keep the file as a raw code DAG, or switch the "
        "DAG's source policy to 'generate' to regenerate the file deliberately."
    )
    return plan


__all__ = [
    "ExistingFile",
    "PlanContext",
    "Resolution",
    "SourceIssue",
    "SourcePlan",
    "TargetState",
    "applicable",
    "classify",
    "folder_importer",
    "is_owned",
    "keeps_hand_written_file",
    "mark_hand_written",
    "plan_source",
    "resolve_policy",
    "sha256_text",
]
