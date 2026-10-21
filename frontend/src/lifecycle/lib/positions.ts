import type { DagSpec } from "../../api/gen/types.gen";

type SpecNode = NonNullable<DagSpec["nodes"]>[number];

const keyOf = (n: SpecNode): string | null =>
  n.kind === "task_group" ? `g:${n.group_id}` : n.kind === "note" ? null : `t:${n.task_id}`;

/**
 * Copies canvas positions from `previous` onto `next` for nodes with the same task/group id
 * (re-imported documents, e.g. "Apply YAML to canvas"). Returns the number of matches.
 */
export function keepPositions(next: DagSpec, previous: DagSpec): { spec: DagSpec; matched: number } {
  const old = new Map<string, SpecNode>();
  for (const n of previous.nodes ?? []) {
    const k = keyOf(n);
    if (k) old.set(k, n);
  }
  let matched = 0;
  const nodes = (next.nodes ?? []).map((n) => {
    const k = keyOf(n);
    const prev = k ? old.get(k) : undefined;
    if (!prev?.position) return n;
    matched++;
    return { ...n, position: { ...prev.position } };
  });
  return { spec: { ...next, nodes }, matched };
}
