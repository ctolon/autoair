"""Spec-level diff and three-way merge.

Specs are compared structurally rather than as text: nodes are matched by ``id``, edges
by ``(source, target)``, DAG-level fields by name and node fields key by key (so two
people editing different params of the same operator merge cleanly). Lists other than
``nodes`` / ``edges`` (tags, rules, ...) are treated as atomic values, and tagged
objects (``type`` / ``kind`` / ``op`` / ``$type``) whose discriminator changed are too.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, ValidationError

from autoair.spec import DagSpec


class _Missing:
    """Sentinel for "absent on this side" (distinct from an explicit ``null``)."""

    def __repr__(self) -> str:
        return "<missing>"


MISSING: Any = _Missing()
_DISCRIMINATORS = ("type", "kind", "op", "$type")
_LAYOUT_FIELDS = frozenset({"position"})


# --------------------------------------------------------------------------- models


class Conflict(BaseModel):
    path: str = Field(description="e.g. 'schedule', 'nodes[extract].params.sql'.")
    base: Any = None
    ours: Any = None
    theirs: Any = None
    reason: str = "changed on both sides"
    base_present: bool = True
    ours_present: bool = True
    theirs_present: bool = True


class MergeResult(BaseModel):
    merged: DagSpec | None = Field(
        default=None, description="The merged spec; None when conflicts remain."
    )
    conflicts: list[Conflict] = Field(default_factory=list)
    draft: dict[str, Any] | None = Field(
        default=None,
        description="Best-effort merge (ours wins on conflicts) to resolve conflicts from.",
    )

    @property
    def clean(self) -> bool:
        return self.merged is not None and not self.conflicts


class FieldChange(BaseModel):
    path: str
    before: Any = None
    after: Any = None
    before_present: bool = Field(
        default=True, description="False when the key was absent before (not null)."
    )
    after_present: bool = Field(
        default=True, description="False when the key is absent after (not null)."
    )


class NodeChange(BaseModel):
    id: str
    kind: str | None = None
    changes: list[FieldChange] = Field(default_factory=list)
    layout_only: bool = Field(default=False, description="Only the canvas position changed.")


class EdgeRef(BaseModel):
    source: str
    target: str
    label: str | None = None


class EdgeChange(BaseModel):
    source: str
    target: str
    changes: list[FieldChange] = Field(default_factory=list)


class SpecDiff(BaseModel):
    added_nodes: list[dict[str, Any]] = Field(default_factory=list)
    removed_nodes: list[dict[str, Any]] = Field(default_factory=list)
    changed_nodes: list[NodeChange] = Field(default_factory=list)
    added_edges: list[EdgeRef] = Field(default_factory=list)
    removed_edges: list[EdgeRef] = Field(default_factory=list)
    changed_edges: list[EdgeChange] = Field(default_factory=list)
    changed_fields: list[FieldChange] = Field(
        default_factory=list, description="DAG-level fields (schedule, params, ...)."
    )
    identical: bool = True


# --------------------------------------------------------------------------- helpers


def _dump(spec: DagSpec) -> dict[str, Any]:
    data: dict[str, Any] = spec.model_dump(mode="json", by_alias=True, exclude_none=True)
    return data


def _edge_key(edge: dict[str, Any]) -> str:
    return f"{edge['source']}->{edge['target']}"


def _keyed(items: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    if key == "edge":
        return {_edge_key(e): e for e in items}
    return {str(i[key]): i for i in items}


def _without_id(item: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in item.items() if k != "id"}


def _join(path: str, key: str) -> str:
    return f"{path}.{key}" if path else key


def _value(v: Any) -> Any:
    return None if v is MISSING else v


# --------------------------------------------------------------------------- diff


def _change(path: str, before: Any, after: Any) -> FieldChange:
    return FieldChange(
        path=path,
        before=_value(before),
        after=_value(after),
        before_present=before is not MISSING,
        after_present=after is not MISSING,
    )


def _field_changes(path: str, before: Any, after: Any, out: list[FieldChange]) -> None:
    """Changes from ``before`` to ``after``. An absent key (``MISSING``) and an explicit
    null differ: ``execution_timeout: null`` overrides a DAG default, absence inherits it."""
    if before is MISSING and after is MISSING:
        return
    if before is not MISSING and after is not MISSING and before == after:
        return
    if isinstance(before, dict) and isinstance(after, dict):
        if any(before.get(d) != after.get(d) for d in _DISCRIMINATORS if d in before or d in after):
            out.append(_change(path, before, after))
            return
        for key in sorted(before.keys() | after.keys()):
            _field_changes(_join(path, key), before.get(key, MISSING), after.get(key, MISSING), out)
        return
    out.append(_change(path, before, after))


def diff_specs(a: DagSpec, b: DagSpec) -> SpecDiff:
    """What changed going from ``a`` to ``b``."""
    da, db = _dump(a), _dump(b)
    diff = SpecDiff()
    for key in sorted((da.keys() | db.keys()) - {"nodes", "edges"}):
        _field_changes(key, da.get(key, MISSING), db.get(key, MISSING), diff.changed_fields)

    nodes_a, nodes_b = _keyed(da.get("nodes", []), "id"), _keyed(db.get("nodes", []), "id")
    for node_id, node in nodes_b.items():
        if node_id not in nodes_a:
            diff.added_nodes.append(node)
    for node_id, node in nodes_a.items():
        if node_id not in nodes_b:
            diff.removed_nodes.append(node)
            continue
        changes: list[FieldChange] = []
        _field_changes("", node, nodes_b[node_id], changes)
        if changes:
            diff.changed_nodes.append(
                NodeChange(
                    id=node_id,
                    kind=nodes_b[node_id].get("kind"),
                    changes=changes,
                    layout_only=all(c.path.split(".", 1)[0] in _LAYOUT_FIELDS for c in changes),
                )
            )

    edges_a, edges_b = _keyed(da.get("edges", []), "edge"), _keyed(db.get("edges", []), "edge")
    for key, edge in edges_b.items():
        if key not in edges_a:
            diff.added_edges.append(EdgeRef.model_validate(edge, strict=False))
    for key, edge in edges_a.items():
        if key not in edges_b:
            diff.removed_edges.append(EdgeRef.model_validate(edge, strict=False))
            continue
        changes = []
        _field_changes("", edge, edges_b[key], changes)
        if changes:
            diff.changed_edges.append(
                EdgeChange(source=edge["source"], target=edge["target"], changes=changes)
            )
    diff.identical = not (
        diff.added_nodes
        or diff.removed_nodes
        or diff.changed_nodes
        or diff.added_edges
        or diff.removed_edges
        or diff.changed_edges
        or diff.changed_fields
    )
    return diff


# --------------------------------------------------------------------------- merge


class _Merger:
    def __init__(self) -> None:
        self.conflicts: list[Conflict] = []

    def conflict(self, path: str, base: Any, ours: Any, theirs: Any, reason: str) -> None:
        self.conflicts.append(
            Conflict(
                path=path,
                base=_value(base),
                ours=_value(ours),
                theirs=_value(theirs),
                reason=reason,
                base_present=base is not MISSING,
                ours_present=ours is not MISSING,
                theirs_present=theirs is not MISSING,
            )
        )

    def value(self, path: str, base: Any, ours: Any, theirs: Any) -> Any:
        """Merge one value; returns ``MISSING`` when the key should be absent."""
        if ours == theirs:
            return ours
        if ours == base:
            return theirs
        if theirs == base:
            return ours
        if ours is MISSING or theirs is MISSING:
            self.conflict(path, base, ours, theirs, "deleted on one side, modified on the other")
            return theirs if ours is MISSING else ours
        dicts = isinstance(ours, dict) and isinstance(theirs, dict)
        if dicts and (base is MISSING or isinstance(base, dict)):
            b = base if isinstance(base, dict) else {}
            if not any(
                len({repr(x.get(d)) for x in (b, ours, theirs) if d in x}) > 1
                for d in _DISCRIMINATORS
                if d in ours or d in theirs
            ):
                return self.mapping(path, b, ours, theirs)
            self.conflict(path, base, ours, theirs, "the object type changed")
            return ours
        self.conflict(path, base, ours, theirs, "changed on both sides")
        return ours

    def mapping(
        self, path: str, base: dict[str, Any], ours: dict[str, Any], theirs: dict[str, Any]
    ) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key in [*ours, *(k for k in theirs if k not in ours)]:
            merged = self.value(
                _join(path, key),
                base.get(key, MISSING),
                ours.get(key, MISSING),
                theirs.get(key, MISSING),
            )
            if merged is not MISSING:
                out[key] = merged
        return out

    def keyed_list(
        self,
        label: str,
        key: str,
        base: list[dict[str, Any]],
        ours: list[dict[str, Any]],
        theirs: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        b, o, t = _keyed(base, key), _keyed(ours, key), _keyed(theirs, key)
        out: list[dict[str, Any]] = []
        for k in [*o, *(k for k in t if k not in o)]:
            if key == "edge" and k in o and k in t and _without_id(o[k]) == _without_id(t[k]):
                t = {**t, k: o[k]}  # same edge, different client-generated ids
            merged = self.value(
                f"{label}[{k}]", b.get(k, MISSING), o.get(k, MISSING), t.get(k, MISSING)
            )
            if merged is not MISSING:
                out.append(merged)
        return out


def merge_specs(base: DagSpec, ours: DagSpec, theirs: DagSpec) -> MergeResult:
    """Three-way merge of two specs that both descend from ``base``."""
    db, do, dt = _dump(base), _dump(ours), _dump(theirs)
    merger = _Merger()
    top = merger.mapping(
        "",
        {k: v for k, v in db.items() if k not in ("nodes", "edges")},
        {k: v for k, v in do.items() if k not in ("nodes", "edges")},
        {k: v for k, v in dt.items() if k not in ("nodes", "edges")},
    )
    nodes = merger.keyed_list(
        "nodes", "id", db.get("nodes", []), do.get("nodes", []), dt.get("nodes", [])
    )
    edges = merger.keyed_list(
        "edges", "edge", db.get("edges", []), do.get("edges", []), dt.get("edges", [])
    )
    node_ids = {n["id"] for n in nodes}
    for edge in edges:
        missing = [e for e in (edge["source"], edge["target"]) if e not in node_ids]
        if missing:
            merger.conflict(
                f"edges[{_edge_key(edge)}]",
                MISSING,
                edge,
                MISSING,
                f"edge references deleted node(s): {', '.join(missing)}",
            )
    for node in nodes:
        parent = node.get("parent_group")
        if parent and parent not in node_ids:
            merger.conflict(
                f"nodes[{node['id']}].parent_group",
                MISSING,
                parent,
                MISSING,
                f"parent group {parent!r} was deleted",
            )
    draft = {**top, "nodes": nodes, "edges": edges}
    if merger.conflicts:
        return MergeResult(conflicts=merger.conflicts, draft=draft)
    try:
        merged = DagSpec.model_validate(draft)
    except ValidationError as exc:
        merger.conflict(
            "",
            MISSING,
            MISSING,
            MISSING,
            f"the combined spec is invalid: {exc.error_count()} validation error(s)",
        )
        return MergeResult(conflicts=merger.conflicts, draft=draft)
    return MergeResult(merged=merged, draft=draft)
