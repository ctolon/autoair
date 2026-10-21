import type { Problem } from "../api/gen/types.gen";
import type { BuilderNode } from "../lib/types";
import { nodeKey } from "../lib/types";
import type { DiffOverlay } from "./builder";

/**
 * Lookup tables derived from store values, built once per value (WeakMap by identity) and
 * shared by every node view. Without them each of N mounted nodes scanned the whole problem
 * list / node array on every store update — O(N²) per drag frame on large graphs.
 */

const NO_PROBLEMS: readonly Problem[] = Object.freeze([]);

const problemIndexes = new WeakMap<readonly Problem[], Map<string, readonly Problem[]>>();
let lastProblemIndex: Map<string, readonly Problem[]> | null = null;

function sameProblems(a: readonly Problem[], b: readonly Problem[]): boolean {
  return a.length === b.length && a.every((p, i) => JSON.stringify(p) === JSON.stringify(b[i]));
}

/**
 * Problems of each node id. A node whose problems did not change since the previous
 * validation keeps the previous array, so its view does not re-render when new results
 * (equal for that node) arrive.
 */
export function problemsByNode(problems: readonly Problem[]): Map<string, readonly Problem[]> {
  const cached = problemIndexes.get(problems);
  if (cached) return cached;
  const index = new Map<string, Problem[]>();
  for (const p of problems) {
    if (!p.node_id) continue;
    const list = index.get(p.node_id);
    if (list) list.push(p);
    else index.set(p.node_id, [p]);
  }
  const out = new Map<string, readonly Problem[]>(index);
  if (lastProblemIndex) {
    for (const [id, list] of index) {
      const prev = lastProblemIndex.get(id);
      if (prev && sameProblems(prev, list)) out.set(id, prev);
    }
  }
  lastProblemIndex = out;
  problemIndexes.set(problems, out);
  return out;
}

/** The problems of one node (a stable, shared empty array when there are none). */
export function nodeProblems(problems: readonly Problem[], id: string): readonly Problem[] {
  return problemsByNode(problems).get(id) ?? NO_PROBLEMS;
}

export type DiffStatus = "added" | "changed" | null;

const diffIndexes = new WeakMap<DiffOverlay, Map<string, Exclude<DiffStatus, null>>>();

/** Diff status of a node against the compared version (O(1) per node). */
export function diffStatusOf(diff: DiffOverlay | null, id: string): DiffStatus {
  if (!diff) return null;
  let index = diffIndexes.get(diff);
  if (!index) {
    index = new Map();
    // "added" wins over "changed", as before.
    for (const n of diff.changed) index.set(n, "changed");
    for (const n of diff.added) index.set(n, "added");
    diffIndexes.set(diff, index);
  }
  return index.get(id) ?? null;
}

const memberIndexes = new WeakMap<readonly BuilderNode[], Map<string, string[]>>();

/** Task node ids inside each task group (at any depth), per node array. */
export function groupTaskMembers(nodes: readonly BuilderNode[]): Map<string, string[]> {
  const cached = memberIndexes.get(nodes);
  if (cached) return cached;
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const index = new Map<string, string[]>();
  for (const n of nodes) {
    if (n.type === "task_group" || n.type === "note") continue;
    let cur = n.parentId;
    const seen = new Set<string>();
    while (cur && !seen.has(cur)) {
      seen.add(cur);
      const list = index.get(cur);
      if (list) list.push(n.id);
      else index.set(cur, [n.id]);
      cur = byId.get(cur)?.parentId;
    }
  }
  memberIndexes.set(nodes, index);
  return index;
}

const structureKeys = new WeakMap<readonly BuilderNode[], string>();

/**
 * What effective Airflow task ids depend on (ids, keys, parents, group prefixes), as one
 * string per node array: consumers re-run only when it changes, not on every drag frame.
 */
export function structureKey(nodes: readonly BuilderNode[]): string {
  let key = structureKeys.get(nodes);
  if (key === undefined) {
    key = nodes
      .map((n) => {
        const prefix = n.type === "task_group" && n.data.spec.prefix_group_id === false ? 0 : 1;
        return `${n.id}\u0001${n.parentId ?? ""}\u0001${nodeKey(n)}\u0001${prefix}`;
      })
      .join("\u0002");
    structureKeys.set(nodes, key);
  }
  return key;
}
