import type { XYPosition } from "@xyflow/react";

import type {
  BuilderEdge,
  BuilderNode,
  ClipboardFragment,
  FragmentInput,
  NodeKind,
  SpecEdge,
  SpecNode,
} from "../lib/types";
import { nodeKey } from "../lib/types";
import { edgeId, graphFromSpec, specEdgesOf, specNodesOf } from "../store/convert";
import { absPos, descendantsOf, sortParentsFirst } from "./geometry";

const KINDS: ReadonlySet<NodeKind> = new Set(["operator", "python_task", "task_group", "custom", "note"]);

/** Default offset of a paste/duplicate when no target position is given. */
const PASTE_OFFSET = { x: 40, y: 40 } as const;

/**
 * Unique key derived from `key`: kept as-is when free, otherwise `<stem>_<n>` with the
 * smallest free n (a trailing `_<digits>` of `key` is treated as a counter, not part of the stem).
 */
export function freshKey(used: ReadonlySet<string>, key: string): string {
  if (!used.has(key)) return key;
  const stem = key.replace(/_\d+$/, "") || "task";
  let i = 1;
  while (used.has(`${stem}_${i}`)) i++;
  return `${stem}_${i}`;
}

/**
 * Serialises a subset of the canvas. Selected task groups bring all their members; nodes
 * whose group is not part of the subset become top-level (with absolute positions).
 */
export function exportFragment(
  nodes: readonly BuilderNode[],
  edges: readonly BuilderEdge[],
  ids: Iterable<string>,
): ClipboardFragment {
  const roots = new Set(ids);
  const keep = new Set([...roots, ...descendantsOf(nodes, roots)]);
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const subset = nodes.filter((n) => keep.has(n.id));
  const specNodes = specNodesOf(subset).map((sn): SpecNode => {
    if (sn.parent_group && keep.has(sn.parent_group)) return sn;
    const abs = absPos(nodes, sn.id, byId);
    return {
      ...sn,
      parent_group: null,
      position: { ...sn.position, x: Math.round(abs.x), y: Math.round(abs.y) },
    } as SpecNode;
  });
  const specEdges = specEdgesOf(edges.filter((e) => keep.has(e.source) && keep.has(e.target)));
  return { autoair: "fragment", nodes: specNodes, edges: specEdges };
}

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);

function isSpecNode(v: unknown): v is SpecNode {
  return isRecord(v) && typeof v.id === "string" && typeof v.kind === "string" && KINDS.has(v.kind as NodeKind);
}

function isSpecEdge(v: unknown): v is SpecEdge {
  return isRecord(v) && typeof v.source === "string" && typeof v.target === "string";
}

/** Accepts our clipboard fragment (`{autoair: "fragment", …}`) or a whole DAG spec; anything else → null. */
export function parseFragment(text: string): FragmentInput | null {
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch {
    return null;
  }
  if (!isRecord(value) || !Array.isArray(value.nodes)) return null;
  const isOurs = value.autoair === "fragment" || typeof value.dag_id === "string";
  if (!isOurs) return null;
  const nodes = value.nodes.filter(isSpecNode);
  if (!nodes.length) return null;
  const edges = Array.isArray(value.edges) ? value.edges.filter(isSpecEdge) : [];
  return { nodes, edges };
}

/** Deep-copies `value`, pointing XCom references (`{"$type": "xcom", node}`) at re-generated node ids. */
function remapXComRefs<T>(value: T, idMap: ReadonlyMap<string, string>): T {
  const walk = (v: unknown): unknown => {
    if (Array.isArray(v)) return v.map(walk);
    if (!isRecord(v)) return v;
    const out: Record<string, unknown> = {};
    for (const [k, inner] of Object.entries(v)) out[k] = walk(inner);
    if (out.$type === "xcom" && typeof out.node === "string") {
      const mapped = idMap.get(out.node);
      if (mapped) out.node = mapped;
    }
    return out;
  };
  return walk(value) as T;
}

type PreparedFragment = {
  nodes: BuilderNode[];
  edges: BuilderEdge[];
  /** fragment node id → new node id */
  idMap: Map<string, string>;
};

type PrepareOptions = {
  /** Id generator (the store's uid in the app; deterministic in tests). */
  newId: () => string;
  /** Top-left corner of the inserted fragment (absolute canvas coordinates). */
  at?: XYPosition | undefined;
  /** Parent task group for the fragment's top-level nodes. */
  parentId?: string | null | undefined;
};

/**
 * Turns a fragment into nodes/edges that can be appended to `existing` without collisions:
 * fresh node ids, unique task_id / group_id / function_name, remapped parents, edges and
 * XCom references, positions moved to `at` (or offset from the original place).
 */
export function prepareFragment(
  existing: readonly BuilderNode[],
  fragment: FragmentInput,
  { newId, at, parentId }: PrepareOptions,
): PreparedFragment {
  const src = fragment.nodes.filter(isSpecNode);
  const idMap = new Map<string, string>();
  for (const n of src) idMap.set(n.id, newId());

  const usedKeys = new Set(existing.map(nodeKey));
  const usedFns = new Set<string>();
  for (const n of existing) if (n.data.spec.kind === "python_task") usedFns.add(n.data.spec.function_name);

  // Top-level = no parent inside the fragment; they get absolute coordinates.
  const isTop = (n: SpecNode) => !n.parent_group || !idMap.has(n.parent_group);
  const tops = src.filter(isTop);
  const unplaced = tops.every((n) => !n.position || (n.position.x === 0 && n.position.y === 0));
  const placed = new Map<string, XYPosition>();
  tops.forEach((n, i) => {
    placed.set(
      n.id,
      unplaced && tops.length > 1 ? { x: i * 280, y: 0 } : { x: n.position?.x ?? 0, y: n.position?.y ?? 0 },
    );
  });
  const minX = Math.min(...[...placed.values()].map((p) => p.x));
  const minY = Math.min(...[...placed.values()].map((p) => p.y));
  const delta = at ? { x: at.x - minX, y: at.y - minY } : PASTE_OFFSET;
  const parentAbs = parentId ? absPos(existing, parentId) : { x: 0, y: 0 };

  const specNodes = src.map((n): SpecNode => {
    const id = idMap.get(n.id) as string;
    const remapped = remapXComRefs(n, idMap);
    let position = n.position ?? { x: 0, y: 0 };
    let parent: string | null;
    if (isTop(n)) {
      const p = placed.get(n.id) ?? { x: 0, y: 0 };
      position = { ...position, x: p.x + delta.x - parentAbs.x, y: p.y + delta.y - parentAbs.y };
      parent = parentId ?? null;
    } else {
      parent = idMap.get(n.parent_group as string) ?? null;
    }
    const out = { ...remapped, id, parent_group: parent, position } as SpecNode;
    switch (out.kind) {
      case "task_group": {
        out.group_id = freshKey(usedKeys, out.group_id);
        usedKeys.add(out.group_id);
        break;
      }
      case "operator":
      case "custom": {
        out.task_id = freshKey(usedKeys, out.task_id);
        usedKeys.add(out.task_id);
        break;
      }
      case "python_task": {
        const sameName = out.function_name === out.task_id;
        out.task_id = freshKey(usedKeys, out.task_id);
        usedKeys.add(out.task_id);
        out.function_name = freshKey(usedFns, sameName ? out.task_id : out.function_name);
        usedFns.add(out.function_name);
        break;
      }
      case "note":
        break;
    }
    return out;
  });

  const seen = new Set<string>();
  const specEdges: SpecEdge[] = [];
  for (const e of fragment.edges ?? []) {
    const source = idMap.get(e.source);
    const target = idMap.get(e.target);
    if (!source || !target || source === target) continue;
    const id = edgeId(source, target);
    if (seen.has(id)) continue;
    seen.add(id);
    specEdges.push({ id, source, target, label: e.label ?? null });
  }

  const graph = graphFromSpec(specNodes, specEdges);
  return { nodes: sortParentsFirst(graph.nodes), edges: graph.edges, idMap };
}
