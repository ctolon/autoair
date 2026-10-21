/**
 * Content equality of builder documents without serialising the whole document.
 *
 * Store updates are immutable: an unchanged node keeps its object, an unchanged spec keeps its
 * `data` object. So most comparisons end at a reference check, and only the parts whose
 * references differ are compared by value (JSON, as before). The result is the same as
 * comparing `JSON.stringify` of both documents, at a fraction of the cost on large graphs:
 * a drag fires these comparisons on every frame.
 */

/** Same JSON value: identical references, else equal serialisations. */
export function sameJson(a: unknown, b: unknown): boolean {
  return a === b || JSON.stringify(a) === JSON.stringify(b);
}

type Pos = { x: number; y: number } | undefined;

function samePosition(a: Pos, b: Pos): boolean {
  if (a === b) return true;
  if (a && b && a.x === b.x && a.y === b.y && Object.keys(a).length === 2 && Object.keys(b).length === 2) return true;
  return sameJson(a, b);
}

/** The undoable part of a node (see `partialize` in `builder.ts`). */
export type HistoryNode = {
  id: string;
  type?: string;
  position?: Pos;
  parentId?: string;
  data?: unknown;
  style?: unknown;
};
export type HistoryEdge = { id: string; source: string; target: string; label?: unknown };
export type HistoryDoc = { dag?: unknown; nodes?: readonly HistoryNode[]; edges?: readonly HistoryEdge[] };

/** Equality of two history snapshots (zundo `equality`). */
export function historyEqual(a: HistoryDoc, b: HistoryDoc): boolean {
  if (a === b) return true;
  const an = a.nodes ?? [];
  const bn = b.nodes ?? [];
  const ae = a.edges ?? [];
  const be = b.edges ?? [];
  if (an.length !== bn.length || ae.length !== be.length) return false;
  for (let i = 0; i < ae.length; i++) {
    const x = ae[i] as HistoryEdge;
    const y = be[i] as HistoryEdge;
    if (x === y) continue;
    if (x.id !== y.id || x.source !== y.source || x.target !== y.target || !sameJson(x.label, y.label)) return false;
  }
  for (let i = 0; i < an.length; i++) {
    const x = an[i] as HistoryNode;
    const y = bn[i] as HistoryNode;
    if (x === y) continue;
    if (x.id !== y.id || x.type !== y.type || x.parentId !== y.parentId) return false;
    if (!samePosition(x.position, y.position) || !sameJson(x.data, y.data) || !sameJson(x.style, y.style)) return false;
  }
  return sameJson(a.dag, b.dag);
}

/** What live validation depends on: settings, edges (ends, label), nodes (id, parent, spec). */
type LiveDoc = {
  dag: unknown;
  nodes: readonly { id: string; parentId?: string; data: unknown }[];
  edges: readonly { source: string; target: string; label?: unknown }[];
};

/** True when a change can alter validation or generated code (not just positions/selection). */
export function liveDocEqual(a: LiveDoc, b: LiveDoc): boolean {
  if (a.nodes.length !== b.nodes.length || a.edges.length !== b.edges.length) return false;
  if (a.edges !== b.edges) {
    for (let i = 0; i < a.edges.length; i++) {
      const x = a.edges[i];
      const y = b.edges[i];
      if (x === y) continue;
      if (!x || !y || x.source !== y.source || x.target !== y.target || !sameJson(x.label, y.label)) return false;
    }
  }
  if (a.nodes !== b.nodes) {
    for (let i = 0; i < a.nodes.length; i++) {
      const x = a.nodes[i];
      const y = b.nodes[i];
      if (x === y) continue;
      if (!x || !y || x.id !== y.id || x.parentId !== y.parentId || !sameJson(x.data, y.data)) return false;
    }
  }
  return sameJson(a.dag, b.dag);
}
