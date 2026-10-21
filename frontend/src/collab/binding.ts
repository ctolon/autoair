import * as Y from "yjs";

import { sortParentsFirst } from "../canvas/geometry";
import type { BuilderEdge, BuilderNode, DagSettings } from "../lib/types";
import { type BuilderState, useBuilder } from "../store/builder";

/** Transaction origin of changes made by this tab (the undo manager only tracks these). */
const LOCAL = "autoair-local";

type Store = typeof useBuilder;
type SavedInfo = { dagId: string; etag: string | null; pendingPrUrl: string | null };

/**
 * The shared document: one Y.Map per collection, values are JSON strings of the persisted
 * part of each element (last writer wins per node / edge / DAG settings). The code of a raw
 * code DAG is a Y.Text (`raw`; merged per character), present when `dag.raw` is set.
 */
export function maps(doc: Y.Doc) {
  return {
    nodes: doc.getMap<string>("nodes"),
    edges: doc.getMap<string>("edges"),
    dag: doc.getMap<string>("dag"),
    saved: doc.getMap<string>("saved"),
    raw: doc.getText("raw"),
  };
}

/** Applies `next` to a Y.Text as one splice (common prefix and suffix kept). */
function spliceText(text: Y.Text, next: string): void {
  const prev = text.toString();
  if (prev === next) return;
  let start = 0;
  const max = Math.min(prev.length, next.length);
  while (start < max && prev.charCodeAt(start) === next.charCodeAt(start)) start++;
  let end = 0;
  while (end < max - start && prev.charCodeAt(prev.length - 1 - end) === next.charCodeAt(next.length - 1 - end)) end++;
  const removed = prev.length - start - end;
  if (removed > 0) text.delete(start, removed);
  const inserted = next.slice(start, next.length - end);
  if (inserted) text.insert(start, inserted);
}

const persistNode = ({ id, type, position, parentId, data, style }: BuilderNode) =>
  JSON.stringify({ id, type, position, parentId, data, style });
const persistEdge = ({ id, source, target, label }: BuilderEdge) => JSON.stringify({ id, source, target, label });

const isEmpty = (doc: Y.Doc): boolean => {
  const m = maps(doc);
  return m.nodes.size === 0 && m.edges.size === 0 && !m.dag.has("settings");
};

function writeCollection<T extends { id: string }>(map: Y.Map<string>, items: readonly T[], persist: (x: T) => string) {
  const ids = new Set<string>();
  for (const item of items) {
    ids.add(item.id);
    const json = persist(item);
    if (map.get(item.id) !== json) map.set(item.id, json);
  }
  for (const key of [...map.keys()]) if (!ids.has(key)) map.delete(key);
}

/** Writes the local document (only what differs) into the shared doc. */
function writeLocal(doc: Y.Doc, s: Pick<BuilderState, "dag" | "nodes" | "edges">): void {
  const m = maps(doc);
  doc.transact(() => {
    writeCollection(m.nodes, s.nodes, persistNode);
    writeCollection(m.edges, s.edges, persistEdge);
    const { raw_code: code, ...settings } = s.dag;
    const dag = JSON.stringify(settings);
    if (m.dag.get("settings") !== dag) m.dag.set("settings", dag);
    if (code === undefined || code === null) {
      if (m.dag.has("raw")) m.dag.delete("raw");
      if (m.raw.length) m.raw.delete(0, m.raw.length);
    } else {
      if (m.dag.get("raw") !== "1") m.dag.set("raw", "1");
      spliceText(m.raw, code);
    }
  }, LOCAL);
}

/** Shared doc → store, keeping local-only flags (selection, measured size) and unchanged objects. */
function applyRemote(doc: Y.Doc, store: Store = useBuilder): void {
  const m = maps(doc);
  const s = store.getState();
  const localNodes = new Map(s.nodes.map((n) => [n.id, n]));
  const localEdges = new Map(s.edges.map((e) => [e.id, e]));

  const nodes: BuilderNode[] = [];
  for (const [id, json] of m.nodes.entries()) {
    const local = localNodes.get(id);
    if (local && persistNode(local) === json) {
      nodes.push(local);
      continue;
    }
    const remote = JSON.parse(json) as BuilderNode;
    if (remote.parentId === undefined) delete remote.parentId;
    if (remote.style === undefined) delete remote.style;
    nodes.push({
      ...remote,
      selected: local?.selected ?? false,
      ...(local?.measured ? { measured: local.measured } : {}),
    });
  }
  const edges: BuilderEdge[] = [];
  for (const [id, json] of m.edges.entries()) {
    const local = localEdges.get(id);
    if (local && persistEdge(local) === json) {
      edges.push(local);
      continue;
    }
    const remote = JSON.parse(json) as BuilderEdge;
    if (remote.label === undefined) delete remote.label;
    edges.push({ ...remote, selected: local?.selected ?? false });
  }
  const settings = m.dag.get("settings");
  const shared: DagSettings | null = settings ? (JSON.parse(settings) as DagSettings) : null;
  if (shared && m.dag.get("raw") === "1") shared.raw_code = m.raw.toString();
  const dag = shared && JSON.stringify(shared) !== JSON.stringify(s.dag) ? shared : s.dag;

  // Keep the local order for known nodes (stable rendering), then parents before children.
  const order = new Map(s.nodes.map((n, i) => [n.id, i]));
  nodes.sort((a, b) => (order.get(a.id) ?? Infinity) - (order.get(b.id) ?? Infinity));

  const temporal = store.temporal.getState();
  temporal.pause(); // remote edits never enter this tab's undo history
  try {
    store.setState({
      dag,
      nodes: sortParentsFirst(nodes),
      edges,
      dirty: true,
      selectedId: s.selectedId && m.nodes.has(s.selectedId) ? s.selectedId : null,
    });
  } finally {
    temporal.resume();
  }
}

export function readSaved(doc: Y.Doc): SavedInfo | null {
  const raw = maps(doc).saved.get("info");
  return raw ? (JSON.parse(raw) as SavedInfo) : null;
}

export interface Binding {
  undoManager: Y.UndoManager;
  destroy: () => void;
}

/**
 * Two-way binding between the builder store and a shared Y.Doc. Call after the initial sync:
 * an empty room is seeded from this tab, otherwise the room's state replaces the local one.
 */
export function bind(doc: Y.Doc, store: Store = useBuilder, onLeave?: () => void): Binding {
  const m = maps(doc);
  if (isEmpty(doc)) writeLocal(doc, store.getState());
  else applyRemote(doc, store);
  const room = store.getState().dag.dag_id;
  let applying = false;

  const observer = (_events: unknown, tr: Y.Transaction) => {
    if (tr.origin === LOCAL) return;
    applying = true;
    try {
      applyRemote(doc, store);
    } finally {
      applying = false;
    }
  };
  for (const map of [m.nodes, m.edges, m.dag]) map.observeDeep(observer);
  m.raw.observe(observer);

  // Someone deployed: their saved version is now everybody's base (no false conflicts).
  const savedObserver = (_e: unknown, tr: Y.Transaction) => {
    if (tr.origin === LOCAL) return;
    const info = readSaved(doc);
    if (!info) return;
    applying = true;
    try {
      store.getState().markSaved(info.dagId, { etag: info.etag, pendingPrUrl: info.pendingPrUrl });
    } finally {
      applying = false;
    }
  };
  m.saved.observe(savedObserver);

  const unsubscribe = store.subscribe((state, prev) => {
    if (applying) return;
    if (state.savedDagId !== prev.savedDagId && state.savedDagId && state.savedDagId !== room) {
      onLeave?.(); // another DAG was opened: leave the room
      return;
    }
    if (state.nodes !== prev.nodes || state.edges !== prev.edges || state.dag !== prev.dag) writeLocal(doc, state);
    if (state.baseEtag !== prev.baseEtag && !state.dirty && state.savedDagId) {
      const info: SavedInfo = { dagId: state.savedDagId, etag: state.baseEtag, pendingPrUrl: state.pendingPrUrl };
      doc.transact(() => m.saved.set("info", JSON.stringify(info)), LOCAL);
    }
  });

  const undoManager = new Y.UndoManager([m.nodes, m.edges, m.dag, m.raw], {
    trackedOrigins: new Set([LOCAL]),
    captureTimeout: 400,
  });

  return {
    undoManager,
    destroy: () => {
      unsubscribe();
      for (const map of [m.nodes, m.edges, m.dag]) map.unobserveDeep(observer);
      m.raw.unobserve(observer);
      m.saved.unobserve(savedObserver);
      undoManager.destroy();
    },
  };
}
