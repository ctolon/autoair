import type { BuilderEdge, BuilderNode, TaskGroupFlowNode, TaskGroupSpec } from "../lib/types";
import { edgeId } from "../store/convert";
import { GROUP_PADDING, absBox, absPos, descendantsOf, sortParentsFirst, topLevelOf, unionBox } from "./geometry";

const stripParent = (n: BuilderNode): BuilderNode => {
  const { parentId: _p, extent: _e, ...rest } = n;
  return rest as BuilderNode;
};

/**
 * Wraps `ids` in a new task group sized around them. The group is created inside the
 * members' common parent (top-level when they live in different groups); members keep
 * their absolute canvas position.
 */
export function groupNodes(
  nodes: readonly BuilderNode[],
  ids: Iterable<string>,
  group: { id: string; spec: TaskGroupSpec },
): BuilderNode[] | null {
  const present = new Set(nodes.map((n) => n.id));
  const members = topLevelOf(
    nodes,
    [...ids].filter((id) => present.has(id)),
  );
  if (!members.length) return null;
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const parents = new Set(members.map((id) => byId.get(id)?.parentId ?? null));
  const parentId = parents.size === 1 ? ([...parents][0] ?? null) : null;

  const box = unionBox(members.map((id) => absBox(nodes, byId.get(id) as BuilderNode, byId)));
  if (!box) return null;
  const groupAbs = { x: box.x - GROUP_PADDING.x, y: box.y - GROUP_PADDING.top };
  const parentAbs = parentId ? absPos(nodes, parentId, byId) : { x: 0, y: 0 };
  const groupNode: TaskGroupFlowNode = {
    id: group.id,
    type: "task_group",
    position: { x: groupAbs.x - parentAbs.x, y: groupAbs.y - parentAbs.y },
    ...(parentId ? { parentId } : {}),
    data: { spec: group.spec },
    style: {
      width: Math.round(box.width + 2 * GROUP_PADDING.x),
      height: Math.round(box.height + GROUP_PADDING.top + GROUP_PADDING.bottom),
    },
    selected: true,
  };
  const memberSet = new Set(members);
  const next = nodes.map((n): BuilderNode => {
    if (!memberSet.has(n.id)) return n.selected ? { ...n, selected: false } : n;
    const abs = absPos(nodes, n.id, byId);
    return {
      ...stripParent(n),
      parentId: group.id,
      position: { x: abs.x - groupAbs.x, y: abs.y - groupAbs.y },
      selected: false,
    } as BuilderNode;
  });
  return sortParentsFirst([...next, groupNode]);
}

/**
 * Dissolves a task group: members move to the group's parent (keeping their absolute
 * position) and edges attached to the group are rewired with Airflow semantics — an edge
 * into the group feeds its entry members, an edge out of it leaves from its exit members.
 */
export function ungroupNode(
  nodes: readonly BuilderNode[],
  edges: readonly BuilderEdge[],
  groupId: string,
): { nodes: BuilderNode[]; edges: BuilderEdge[] } | null {
  const group = nodes.find((n) => n.id === groupId);
  if (!group || group.type !== "task_group") return null;
  const children = nodes.filter((n) => n.parentId === groupId).map((n) => n.id);
  const childSet = new Set(children);

  // Map every descendant onto the direct child that contains it.
  const directOf = new Map<string, string>();
  for (const c of children) {
    directOf.set(c, c);
    for (const d of descendantsOf(nodes, [c])) directOf.set(d, c);
  }
  const hasIn = new Set<string>();
  const hasOut = new Set<string>();
  for (const e of edges) {
    const s = directOf.get(e.source);
    const t = directOf.get(e.target);
    if (s && t && s !== t) {
      hasOut.add(s);
      hasIn.add(t);
    }
  }
  const entries = children.filter((c) => !hasIn.has(c));
  const exits = children.filter((c) => !hasOut.has(c));

  const out: BuilderEdge[] = [];
  const seen = new Set<string>();
  const push = (e: BuilderEdge) => {
    if (e.source === e.target) return;
    const key = `${e.source}->${e.target}`;
    if (seen.has(key)) return;
    seen.add(key);
    out.push(e);
  };
  for (const e of edges) {
    if (e.target === groupId && e.source !== groupId) {
      for (const m of entries) push({ ...e, id: edgeId(e.source, m), target: m });
    } else if (e.source === groupId) {
      for (const m of exits) push({ ...e, id: edgeId(m, e.target), source: m });
    } else {
      push(e);
    }
  }

  const next = nodes
    .filter((n) => n.id !== groupId)
    .map((n): BuilderNode => {
      if (!childSet.has(n.id)) return n;
      const position = { x: n.position.x + group.position.x, y: n.position.y + group.position.y };
      const base = { ...stripParent(n), position } as BuilderNode;
      return group.parentId ? ({ ...base, parentId: group.parentId } as BuilderNode) : base;
    });
  return { nodes: sortParentsFirst(next), edges: out };
}

/** Run/visibility information derived from collapsed task groups (UI only). */
type CollapsedView = {
  /** Nodes inside a collapsed group (not rendered). */
  hidden: Set<string>;
  /** Hidden node → outermost collapsed group standing in for it. */
  representative: Map<string, string>;
};

export function collapsedView(nodes: readonly BuilderNode[]): CollapsedView {
  const hidden = new Set<string>();
  const representative = new Map<string, string>();
  const collapsed = new Set(nodes.filter((n) => n.type === "task_group" && n.data.collapsed).map((n) => n.id));
  if (!collapsed.size) return { hidden, representative };
  const byId = new Map(nodes.map((n) => [n.id, n]));
  for (const n of nodes) {
    let rep: string | undefined;
    let cur = n.parentId;
    const seen = new Set<string>();
    while (cur && !seen.has(cur)) {
      seen.add(cur);
      if (collapsed.has(cur)) rep = cur;
      cur = byId.get(cur)?.parentId;
    }
    if (rep) {
      hidden.add(n.id);
      representative.set(n.id, rep);
    }
  }
  return { hidden, representative };
}

/** Edges as displayed: endpoints inside collapsed groups are redirected to the group. */
export function displayEdgeEndpoints(
  edges: readonly BuilderEdge[],
  view: CollapsedView,
): Map<string, { source: string; target: string; hidden: boolean }> {
  const out = new Map<string, { source: string; target: string; hidden: boolean }>();
  const seen = new Set<string>();
  for (const e of edges) {
    const source = view.representative.get(e.source) ?? e.source;
    const target = view.representative.get(e.target) ?? e.target;
    const key = `${source}->${target}`;
    const hidden = source === target || seen.has(key);
    seen.add(key);
    out.set(e.id, { source, target, hidden });
  }
  return out;
}
