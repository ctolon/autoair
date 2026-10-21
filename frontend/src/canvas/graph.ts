import type { BuilderEdge, BuilderNode } from "../lib/types";

/** Ids of all ancestors (task groups) of a node. */
export function ancestors(nodes: BuilderNode[], id: string): Set<string> {
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const out = new Set<string>();
  let cur = byId.get(id)?.parentId;
  while (cur && !out.has(cur)) {
    out.add(cur);
    cur = byId.get(cur)?.parentId;
  }
  return out;
}

/**
 * Airflow semantics: an edge to/from a task group applies to all of its member tasks.
 * Group endpoints are expanded to their members before searching for cycles.
 */
function expandedAdjacency(nodes: BuilderNode[], edges: Pick<BuilderEdge, "source" | "target">[]) {
  const members = new Map<string, string[]>();
  for (const n of nodes) {
    if (n.type === "task_group") continue;
    for (const g of ancestors(nodes, n.id)) {
      members.set(g, [...(members.get(g) ?? []), n.id]);
    }
  }
  const isGroup = new Set(nodes.filter((n) => n.type === "task_group").map((n) => n.id));
  const expand = (id: string) => (isGroup.has(id) ? (members.get(id) ?? []) : [id]);
  const adj = new Map<string, Set<string>>();
  for (const e of edges) {
    for (const a of expand(e.source)) {
      for (const b of expand(e.target)) {
        if (a === b) continue;
        if (!adj.has(a)) adj.set(a, new Set());
        adj.get(a)?.add(b);
      }
    }
  }
  return adj;
}

export function hasCycle(nodes: BuilderNode[], edges: Pick<BuilderEdge, "source" | "target">[]): boolean {
  const adj = expandedAdjacency(nodes, edges);
  const state = new Map<string, 1 | 2>();
  const visit = (n: string): boolean => {
    const s = state.get(n);
    if (s === 1) return true;
    if (s === 2) return false;
    state.set(n, 1);
    for (const m of adj.get(n) ?? []) if (visit(m)) return true;
    state.set(n, 2);
    return false;
  };
  return [...adj.keys()].some(visit);
}

type ConnectionCheck = { ok: true } | { ok: false; reason: string };

/** Used by React Flow's `isValidConnection` and by the store before adding an edge. */
export function checkConnection(
  nodes: BuilderNode[],
  edges: BuilderEdge[],
  source: string,
  target: string,
): ConnectionCheck {
  if (source === target) return { ok: false, reason: "A node cannot depend on itself" };
  if (edges.some((e) => e.source === source && e.target === target)) {
    return { ok: false, reason: "These nodes are already connected" };
  }
  if (ancestors(nodes, source).has(target) || ancestors(nodes, target).has(source)) {
    return { ok: false, reason: "A task group cannot be connected with its own member" };
  }
  if (hasCycle(nodes, [...edges, { source, target }])) {
    return { ok: false, reason: "This connection would create a cycle" };
  }
  return { ok: true };
}

/** Task nodes upstream of `id` (used to offer XCom sources). */
export function upstreamTasks(nodes: BuilderNode[], edges: BuilderEdge[], id: string): BuilderNode[] {
  const preds = new Map<string, string[]>();
  for (const e of edges) preds.set(e.target, [...(preds.get(e.target) ?? []), e.source]);
  const seen = new Set<string>();
  const stack = [id, ...ancestors(nodes, id)];
  while (stack.length) {
    const cur = stack.pop() as string;
    for (const p of preds.get(cur) ?? []) {
      if (!seen.has(p)) {
        seen.add(p);
        stack.push(p);
      }
    }
  }
  // Blueprint steps render whole groups: they have no single XCom output.
  return nodes.filter((n) => seen.has(n.id) && n.type !== "task_group" && n.type !== "blueprint");
}
