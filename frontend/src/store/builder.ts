import {
  type Connection,
  type EdgeChange,
  type NodeChange,
  type XYPosition,
  applyEdgeChanges,
  applyNodeChanges,
} from "@xyflow/react";
import { temporal } from "zundo";
import { create } from "zustand";

import type { DagSpec, Problem } from "../api/gen/types.gen";
import { exportFragment as exportFragmentOf, prepareFragment } from "../canvas/fragment";
import { absPos, absBox, descendantsOf, sortParentsFirst } from "../canvas/geometry";
import { ancestors, checkConnection } from "../canvas/graph";
import { groupNodes as groupNodesOf, ungroupNode } from "../canvas/grouping";
import type {
  BuilderEdge,
  BuilderNode,
  ClipboardFragment,
  DagSettings,
  FragmentInput,
  NodeSpec,
  SpecFor,
} from "../lib/types";
import { nodeKey } from "../lib/types";
import { GROUP_DEFAULT_SIZE, NOTE_DEFAULT_SIZE, edgeId, fromSpec, toSpec } from "./convert";

const emptyDag = (dagId = "my_new_dag"): DagSettings => ({
  spec_version: 2,
  dag_id: dagId,
  output_mode: "python",
  schedule: { type: "none" },
  start_date: new Date(Date.UTC(new Date().getUTCFullYear(), 0, 1)).toISOString(),
  catchup: false,
  tags: ["autoair"],
  default_args: { owner: "airflow", retries: 1, retry_delay_seconds: 300 },
  params: {},
});

type State = {
  dag: DagSettings;
  nodes: BuilderNode[];
  edges: BuilderEdge[];
  selectedId: string | null;
  problems: Problem[];
  /** dag_id of the spec as last loaded/deployed (null for a brand-new DAG). */
  savedDagId: string | null;
  dirty: boolean;
  lastError: string | null;
  /** Optimistic-locking token of the stored spec this document was loaded from / saved as. */
  baseEtag: string | null;
  /** The stored spec at `baseEtag` (3-way merge base on deploy conflicts). */
  baseSpec: DagSpec | null;
  /** Open pull/merge request when the deploy target works in PR mode. */
  pendingPrUrl: string | null;
  /** Deployment environment (overlay) used for deploy / preview. */
  environment: string | null;
  /** Task-instance states of a selected DAG run, painted on the canvas. */
  runOverlay: RunOverlay | null;
  /** Visual diff against another version, painted on the canvas. */
  diffOverlay: DiffOverlay | null;
};

export type RunOverlay = {
  dagId: string;
  runId: string;
  /** node id -> state (mapped tasks: aggregated) */
  states: Record<string, { state: string | null; tryNumber: number; mapped?: number }>;
};

export type DiffOverlay = {
  label: string;
  added: string[];
  removed: string[];
  changed: string[];
};

type Actions = {
  onNodesChange: (changes: NodeChange<BuilderNode>[]) => void;
  onEdgesChange: (changes: EdgeChange<BuilderEdge>[]) => void;
  onConnect: (c: Connection) => void;
  addNode: (spec: NodeSpec, position: XYPosition) => string;
  updateSpec: <K extends NodeSpec["kind"]>(id: string, patch: Partial<SpecFor<K>>) => void;
  setParent: (id: string, groupId: string | null) => void;
  removeNode: (id: string) => void;
  setEdgeLabel: (id: string, label: string) => void;
  setDag: (patch: Partial<DagSettings>) => void;
  applyLayout: (layout: Map<string, { x: number; y: number; width?: number; height?: number }>) => void;
  load: (spec: DagSpec, saved: boolean, meta?: { etag?: string | null; pendingPrUrl?: string | null }) => void;
  /**
   * Replaces the whole document as ONE undoable edit (code mode "Apply", YAML apply): unlike
   * `load`, history and the storage identity (saved dag, etag, PR) are kept.
   */
  replaceSpec: (spec: DagSpec) => void;
  reset: (dagId?: string) => void;
  select: (id: string | null) => void;
  setProblems: (p: Problem[]) => void;
  markSaved: (dagId: string, meta?: { etag?: string | null; pendingPrUrl?: string | null }) => void;
  setEnvironment: (env: string | null) => void;
  setRunOverlay: (overlay: RunOverlay | null) => void;
  setDiffOverlay: (overlay: DiffOverlay | null) => void;
  setError: (msg: string | null) => void;
  toSpec: () => DagSpec;

  // ---- canvas editing (multi-node) ----
  /**
   * Inserts a fragment (clipboard, template fragment) with fresh ids / task_ids / group_ids /
   * function_names, remapped XCom refs and edges. `at` is the top-left corner of the inserted
   * nodes in absolute canvas coordinates (default: offset from their original place).
   * The inserted nodes become the selection. Returns the new node ids.
   */
  insertFragment: (
    fragment: FragmentInput,
    at?: XYPosition,
    opts?: { parentId?: string | null; select?: boolean },
  ) => string[];
  /** Serialises nodes (with members of selected groups and internal edges) as a clipboard fragment. */
  exportFragment: (ids: string[]) => ClipboardFragment;
  /** Copies nodes next to the originals; returns the new ids. */
  duplicateNodes: (ids: string[]) => string[];
  /** Removes nodes (group members are kept, re-parented) and edges in one history step. */
  removeElements: (nodeIds: string[], edgeIds?: string[]) => void;
  /** Wraps nodes in a new task group sized around them; returns the group id (null if nothing to group). */
  groupNodes: (ids: string[]) => string | null;
  /** Dissolves a task group, rewiring its edges onto its entry/exit members. */
  ungroup: (groupId: string) => void;
  /** Collapses/expands a task group (UI only: not part of the spec, does not mark the DAG dirty). */
  setCollapsed: (groupId: string, collapsed: boolean) => void;
  /** Adds an edge (validated like a manual connection); false when rejected (reason in `lastError`). */
  connect: (source: string, target: string) => boolean;
  /** Inserts a new node in the middle of an edge (source → new → target); returns its id. */
  insertOnEdge: (edgeId: string, spec: NodeSpec) => string | null;
  /** Moves nodes by a delta (keyboard nudging). */
  moveNodes: (ids: string[], dx: number, dy: number) => void;
  /** Replaces the canvas selection (React Flow `selected` flags) and the inspected node. */
  setSelection: (nodeIds: string[], edgeIds?: string[]) => void;
};

export type BuilderState = State & Actions;

const uid = () => `n${Math.random().toString(36).slice(2, 9)}`;

/** Ids of the nodes selected on the canvas (falls back to the inspected node). */
export function selectedNodeIds(s: Pick<BuilderState, "nodes" | "selectedId">): string[] {
  const ids = s.nodes.filter((n) => n.selected).map((n) => n.id);
  if (!ids.length && s.selectedId && s.nodes.some((n) => n.id === s.selectedId)) return [s.selectedId];
  return ids;
}

export function selectedEdgeIds(s: Pick<BuilderState, "edges">): string[] {
  return s.edges.filter((e) => e.selected).map((e) => e.id);
}

/** Unique, Airflow-valid key derived from `base` (e.g. "BashOperator" → "bash_1"). */
export function uniqueKey(nodes: BuilderNode[], base: string): string {
  const stem =
    base
      .replace(/(Operator|Sensor)$/, "")
      .replace(/([a-z0-9])([A-Z])/g, "$1_$2")
      .toLowerCase()
      .replace(/[^a-z0-9_]+/g, "_")
      .replace(/^_+|_+$/g, "") || "task";
  const used = new Set(nodes.map(nodeKey));
  let i = 1;
  while (used.has(`${stem}_${i}`)) i++;
  return `${stem}_${i}`;
}

/** Absolute canvas position of a node (sums parent offsets). */
export function absolutePosition(nodes: BuilderNode[], id: string): XYPosition {
  return absPos(nodes, id);
}

/** Removes nodes: members of a removed group are re-parented to that group's parent. */
function withoutNodes(nodes: BuilderNode[], ids: ReadonlySet<string>): BuilderNode[] {
  const byId = new Map(nodes.map((n) => [n.id, n]));
  return nodes
    .filter((n) => !ids.has(n.id))
    .map((n) => {
      if (!n.parentId || !ids.has(n.parentId)) return n;
      // Climb to the first surviving ancestor, accumulating offsets.
      let pos = { ...n.position };
      let parent = byId.get(n.parentId);
      while (parent && ids.has(parent.id)) {
        pos = { x: pos.x + parent.position.x, y: pos.y + parent.position.y };
        parent = parent.parentId ? byId.get(parent.parentId) : undefined;
      }
      const { parentId: _p, extent: _e, ...rest } = n;
      return (parent ? { ...rest, parentId: parent.id, position: pos } : { ...rest, position: pos }) as BuilderNode;
    });
}

// Set by the history middleware below (grouped drags); declared first: the store is created
// at module load and installs them immediately.
let flushPendingHistory: () => void = () => {};
let discardPendingHistory: () => void = () => {};

export const useBuilder = create<BuilderState>()(
  temporal(
    (set, get) => ({
      dag: emptyDag(),
      nodes: [],
      edges: [],
      selectedId: null,
      problems: [],
      savedDagId: null,
      dirty: false,
      lastError: null,
      baseEtag: null,
      baseSpec: null,
      pendingPrUrl: null,
      environment: null,
      runOverlay: null,
      diffOverlay: null,

      onNodesChange: (changes) => {
        const structural = changes.some((c) => c.type !== "select" && c.type !== "dimensions");
        set((s) => ({
          nodes: applyNodeChanges(changes, s.nodes),
          dirty: s.dirty || structural,
          selectedId: changes.some((c) => c.type === "remove" && c.id === s.selectedId) ? null : s.selectedId,
        }));
      },
      onEdgesChange: (changes) =>
        set((s) => ({
          edges: applyEdgeChanges(changes, s.edges),
          dirty: s.dirty || changes.some((c) => c.type !== "select"),
        })),
      onConnect: ({ source, target }) => {
        const { nodes, edges } = get();
        const check = checkConnection(nodes, edges, source, target);
        if (!check.ok) {
          set({ lastError: check.reason });
          return;
        }
        set({ edges: [...edges, { id: edgeId(source, target), source, target }], dirty: true });
      },
      addNode: (spec, position) => {
        const id = uid();
        const base = { id, position, selected: false };
        let node: BuilderNode;
        switch (spec.kind) {
          case "task_group":
            node = { ...base, type: "task_group", data: { spec }, style: { ...GROUP_DEFAULT_SIZE } };
            break;
          case "operator":
            node = { ...base, type: "operator", data: { spec } };
            break;
          case "python_task":
            node = { ...base, type: "python_task", data: { spec } };
            break;
          case "custom":
            node = { ...base, type: "custom", data: { spec } };
            break;
          case "note":
            node = { ...base, type: "note", data: { spec }, style: { ...NOTE_DEFAULT_SIZE } };
            break;
        }
        set((s) => ({ nodes: sortParentsFirst([...s.nodes, node]), selectedId: id, dirty: true }));
        return id;
      },
      updateSpec: (id, patch) =>
        set((s) => ({
          nodes: s.nodes.map((n) =>
            n.id === id ? ({ ...n, data: { ...n.data, spec: { ...n.data.spec, ...patch } } } as BuilderNode) : n,
          ),
          dirty: true,
        })),
      setParent: (id, groupId) => {
        const { nodes } = get();
        if (groupId && (groupId === id || ancestors(nodes, groupId).has(id))) {
          set({ lastError: "A task group cannot be placed inside itself" });
          return;
        }
        const abs = absolutePosition(nodes, id);
        const parentAbs = groupId ? absolutePosition(nodes, groupId) : { x: 0, y: 0 };
        const position = { x: Math.max(10, abs.x - parentAbs.x), y: Math.max(40, abs.y - parentAbs.y) };
        set((s) => ({
          nodes: sortParentsFirst(
            s.nodes.map((n) => {
              if (n.id !== id) return n;
              const { parentId: _old, extent: _extent, ...rest } = n;
              return (groupId ? { ...rest, parentId: groupId, position } : { ...rest, position: abs }) as BuilderNode;
            }),
          ),
          dirty: true,
        }));
      },
      removeNode: (id) =>
        set((s) => {
          // Removing a group keeps its members, re-parented to the group's parent.
          const victim = s.nodes.find((n) => n.id === id);
          const nodes = s.nodes
            .filter((n) => n.id !== id)
            .map((n) => {
              if (n.parentId !== id || !victim) return n;
              const { parentId: _p, ...rest } = n;
              const pos = { x: n.position.x + victim.position.x, y: n.position.y + victim.position.y };
              return (
                victim.parentId ? { ...rest, parentId: victim.parentId, position: pos } : { ...rest, position: pos }
              ) as BuilderNode;
            });
          return {
            nodes,
            edges: s.edges.filter((e) => e.source !== id && e.target !== id),
            selectedId: s.selectedId === id ? null : s.selectedId,
            dirty: true,
          };
        }),
      setEdgeLabel: (id, label) =>
        set((s) => ({
          edges: s.edges.map((e) => (e.id === id ? { ...e, label: label || undefined } : e)),
          dirty: true,
        })),
      setDag: (patch) => set((s) => ({ dag: { ...s.dag, ...patch }, dirty: true })),
      applyLayout: (layout) =>
        set((s) => ({
          nodes: s.nodes.map((n) => {
            const l = layout.get(n.id);
            if (!l) return n;
            const style =
              n.type === "task_group" && l.width && l.height
                ? { ...n.style, width: l.width, height: l.height }
                : n.style;
            return { ...n, position: { x: l.x, y: l.y }, style } as BuilderNode;
          }),
          dirty: true,
        })),
      load: (spec, saved, meta) => {
        const { dag, nodes, edges } = fromSpec(spec);
        set({
          dag,
          nodes,
          edges,
          selectedId: null,
          problems: [],
          savedDagId: saved ? spec.dag_id : null,
          dirty: false,
          baseEtag: saved ? (meta?.etag ?? null) : null,
          baseSpec: saved ? spec : null,
          pendingPrUrl: meta?.pendingPrUrl ?? null,
          runOverlay: null,
          diffOverlay: null,
        });
        clearHistory();
      },
      replaceSpec: (spec) => {
        flushPendingHistory();
        const { dag, nodes, edges } = fromSpec(spec);
        set({ dag, nodes, edges, selectedId: null, dirty: true, runOverlay: null, diffOverlay: null });
      },
      reset: (dagId) => {
        set({
          dag: emptyDag(dagId),
          nodes: [],
          edges: [],
          selectedId: null,
          problems: [],
          savedDagId: null,
          dirty: false,
          baseEtag: null,
          baseSpec: null,
          pendingPrUrl: null,
          runOverlay: null,
          diffOverlay: null,
        });
        clearHistory();
      },
      select: (id) => set({ selectedId: id }),
      setProblems: (problems) => set({ problems }),
      markSaved: (dagId, meta) =>
        set((s) => ({
          savedDagId: dagId,
          dirty: false,
          baseEtag: meta?.etag ?? s.baseEtag,
          baseSpec: toSpec(s.dag, s.nodes, s.edges),
          pendingPrUrl: meta?.pendingPrUrl ?? null,
        })),
      setEnvironment: (environment) => set({ environment }),
      setRunOverlay: (runOverlay) => set({ runOverlay }),
      setDiffOverlay: (diffOverlay) => set({ diffOverlay }),
      setError: (lastError) => set({ lastError }),
      toSpec: () => {
        const { dag, nodes, edges } = get();
        return toSpec(dag, nodes, edges);
      },

      insertFragment: (fragment, at, opts) => {
        const { nodes } = get();
        const parentId = opts?.parentId && nodes.some((n) => n.id === opts.parentId) ? opts.parentId : null;
        const prepared = prepareFragment(nodes, fragment, { newId: uid, at, parentId });
        if (!prepared.nodes.length) return [];
        const select = opts?.select ?? true;
        const added = prepared.nodes.map((n) => ({ ...n, selected: select }) as BuilderNode);
        const ids = added.map((n) => n.id);
        set((s) => ({
          nodes: sortParentsFirst([
            ...(select ? s.nodes.map((n) => (n.selected ? ({ ...n, selected: false } as BuilderNode) : n)) : s.nodes),
            ...added,
          ]),
          edges: [
            ...(select ? s.edges.map((e) => (e.selected ? { ...e, selected: false } : e)) : s.edges),
            ...prepared.edges,
          ],
          selectedId: select ? (ids.length === 1 ? (ids[0] ?? null) : null) : s.selectedId,
          dirty: true,
        }));
        return ids;
      },
      exportFragment: (ids) => {
        const { nodes, edges } = get();
        return exportFragmentOf(nodes, edges, ids);
      },
      duplicateNodes: (ids) => {
        if (!ids.length) return [];
        const fragment = get().exportFragment(ids);
        // Keep duplicates of grouped nodes inside the same group.
        const { nodes } = get();
        const parents = new Set(ids.map((id) => nodes.find((n) => n.id === id)?.parentId ?? null));
        const parentId = parents.size === 1 ? ([...parents][0] ?? null) : null;
        let at: XYPosition | undefined;
        if (parentId) {
          const xs = fragment.nodes.filter((n) => !n.parent_group);
          const min = {
            x: Math.min(...xs.map((n) => n.position?.x ?? 0)),
            y: Math.min(...xs.map((n) => n.position?.y ?? 0)),
          };
          at = { x: min.x + 40, y: min.y + 40 };
        }
        return get().insertFragment(fragment, at, { parentId });
      },
      removeElements: (nodeIds, edgeIds = []) =>
        set((s) => {
          const ids = new Set(nodeIds);
          const dropEdges = new Set(edgeIds);
          if (!ids.size && !dropEdges.size) return {};
          return {
            nodes: withoutNodes(s.nodes, ids),
            edges: s.edges.filter((e) => !dropEdges.has(e.id) && !ids.has(e.source) && !ids.has(e.target)),
            selectedId: s.selectedId && ids.has(s.selectedId) ? null : s.selectedId,
            dirty: true,
          };
        }),
      groupNodes: (ids) => {
        const { nodes } = get();
        const id = uid();
        const next = groupNodesOf(nodes, ids, {
          id,
          spec: { kind: "task_group", group_id: uniqueKey(nodes, "group") },
        });
        if (!next) return null;
        set({ nodes: next, selectedId: id, dirty: true });
        return id;
      },
      ungroup: (groupId) => {
        const { nodes, edges } = get();
        const result = ungroupNode(nodes, edges, groupId);
        if (!result) return;
        set((s) => ({
          nodes: result.nodes,
          edges: result.edges,
          selectedId: s.selectedId === groupId ? null : s.selectedId,
          dirty: true,
        }));
      },
      setCollapsed: (groupId, collapsed) =>
        set((s) => {
          const hide = collapsed ? descendantsOf(s.nodes, [groupId]) : new Set<string>();
          return {
            nodes: s.nodes.map((n) => {
              if (n.id === groupId && n.type === "task_group") {
                const { collapsed: _c, ...data } = n.data;
                return { ...n, data: collapsed ? { ...data, collapsed: true } : data };
              }
              // Hidden members cannot stay selected (keyboard actions would hit invisible nodes).
              return hide.has(n.id) && n.selected ? ({ ...n, selected: false } as BuilderNode) : n;
            }),
            selectedId: s.selectedId && hide.has(s.selectedId) ? groupId : s.selectedId,
          };
        }),
      connect: (source, target) => {
        const { nodes, edges } = get();
        const check = checkConnection(nodes, edges, source, target);
        if (!check.ok) {
          set({ lastError: check.reason });
          return false;
        }
        set({ edges: [...edges, { id: edgeId(source, target), source, target }], dirty: true });
        return true;
      },
      insertOnEdge: (id, spec) => {
        const { nodes, edges } = get();
        const edge = edges.find((e) => e.id === id);
        if (!edge) return null;
        const byId = new Map(nodes.map((n) => [n.id, n]));
        const s = byId.get(edge.source);
        const t = byId.get(edge.target);
        if (!s || !t) return null;
        const sb = absBox(nodes, s, byId);
        const tb = absBox(nodes, t, byId);
        const mid = {
          x: (sb.x + sb.width + tb.x) / 2 - 110,
          y: (sb.y + sb.height / 2 + tb.y + tb.height / 2) / 2 - 32,
        };
        const parentId = s.parentId && s.parentId === t.parentId ? s.parentId : null;
        const parentAbs = parentId ? absPos(nodes, parentId, byId) : { x: 0, y: 0 };
        const newId = get().addNode(spec, { x: mid.x - parentAbs.x, y: mid.y - parentAbs.y });
        if (parentId) {
          set((st) => ({
            nodes: sortParentsFirst(st.nodes.map((n) => (n.id === newId ? ({ ...n, parentId } as BuilderNode) : n))),
          }));
        }
        // Rewire: source → new → target replaces source → target (keeps the label on the first hop).
        set((st) => ({
          edges: [
            ...st.edges.filter((e) => e.id !== id),
            {
              id: edgeId(edge.source, newId),
              source: edge.source,
              target: newId,
              ...(edge.label ? { label: edge.label } : {}),
            },
            { id: edgeId(newId, edge.target), source: newId, target: edge.target },
          ],
          dirty: true,
        }));
        return newId;
      },
      moveNodes: (ids, dx, dy) => {
        if (!ids.length || (!dx && !dy)) return;
        const set_ = new Set(ids);
        set((s) => ({
          nodes: s.nodes.map((n) =>
            set_.has(n.id) ? ({ ...n, position: { x: n.position.x + dx, y: n.position.y + dy } } as BuilderNode) : n,
          ),
          dirty: true,
        }));
      },
      setSelection: (nodeIds, edgeIds = []) =>
        set((s) => {
          const ns = new Set(nodeIds);
          const es = new Set(edgeIds);
          return {
            nodes: s.nodes.map((n) =>
              !!n.selected === ns.has(n.id) ? n : ({ ...n, selected: ns.has(n.id) } as BuilderNode),
            ),
            edges: s.edges.map((e) => (!!e.selected === es.has(e.id) ? e : { ...e, selected: es.has(e.id) })),
            selectedId:
              nodeIds.length === 1 ? (nodeIds[0] ?? null) : s.selectedId && ns.has(s.selectedId) ? s.selectedId : null,
          };
        }),
    }),
    {
      // Only the document is undoable; selection, problems and UI flags are not.
      partialize: (s) => ({
        dag: s.dag,
        nodes: s.nodes.map(
          ({ id, type, position, parentId, data, style }) =>
            ({ id, type, position, parentId, data, style }) as BuilderNode,
        ),
        edges: s.edges.map(({ id, source, target, label }) => ({ id, source, target, label })),
      }),
      equality: (a, b) => JSON.stringify(a) === JSON.stringify(b),
      // Collapse drags (position-only bursts) into one history entry; every other change
      // (insert, delete, edit) is its own entry. An entry must hold the state from *before the
      // first* change of its burst: a plain debounce kept the last call's past state and so
      // silently dropped earlier changes — undo then stopped short of them.
      handleSet: (handleSet) => {
        const commit = handleSet as unknown as (...a: unknown[]) => void;
        let timer: ReturnType<typeof setTimeout> | undefined;
        let burst: { start: unknown; replace: unknown; current: unknown; delta: unknown } | null = null;
        const flush = () => {
          clearTimeout(timer);
          if (burst) commit(burst.start, burst.replace, burst.current, burst.delta);
          burst = null;
        };
        // Undo/redo right after a drag must see the drag in history first; clearing the
        // history drops it (a late commit would resurrect a stale state).
        flushPendingHistory = flush;
        discardPendingHistory = () => {
          clearTimeout(timer);
          burst = null;
        };
        return (pastState, replace, currentState, deltaState) => {
          const past = pastState as unknown as Partial<BuilderState>;
          const current = currentState as Partial<BuilderState>;
          if (isMoveOnly(past, current) || isRawTyping(past, current)) {
            burst = burst
              ? { ...burst, current: currentState, delta: deltaState }
              : { start: pastState, replace, current: currentState, delta: deltaState };
            clearTimeout(timer);
            timer = setTimeout(flush, 250);
            return;
          }
          flush();
          commit(pastState, replace, currentState, deltaState);
        };
      },
      limit: 100,
    },
  ),
);

/** Commits a pending (grouped) drag to history; call before undo/redo/clear. */
export function flushHistory(): void {
  flushPendingHistory();
}

/** Clears undo/redo history, dropping a pending grouped drag. */
export function clearHistory(): void {
  discardPendingHistory();
  useBuilder.temporal.getState().clear();
}

/**
 * True when only the code of a raw code DAG differs (typing in the raw editor): a burst of
 * keystrokes is one history entry, like a drag.
 */
function isRawTyping(a: Partial<BuilderState>, b: Partial<BuilderState>): boolean {
  if (!a.dag || !b.dag || a.dag === b.dag || a.nodes?.length || b.nodes?.length) return false;
  const { raw_code: before, ...restA } = a.dag;
  const { raw_code: after, ...restB } = b.dag;
  if (before == null || after == null || before === after) return false;
  return JSON.stringify(restA) === JSON.stringify(restB);
}

/** True when only node positions differ (a drag): such changes are grouped in history. */
function isMoveOnly(a: Partial<BuilderState>, b: Partial<BuilderState>): boolean {
  if (a.dag !== b.dag || a.edges?.length !== b.edges?.length || a.nodes?.length !== b.nodes?.length) return false;
  const ae = a.edges ?? [];
  const be = b.edges ?? [];
  // A reconnect keeps the edge count but changes endpoints: that is an edit, not a move.
  if (!ae.every((e, i) => be[i]?.source === e.source && be[i]?.target === e.target && be[i]?.label === e.label))
    return false;
  const an = a.nodes ?? [];
  const bn = b.nodes ?? [];
  return an.every((n, i) => {
    const m = bn[i];
    return !!m && m.id === n.id && m.data === n.data && m.parentId === n.parentId;
  });
}

export const selectedNode = (s: BuilderState): BuilderNode | undefined => s.nodes.find((n) => n.id === s.selectedId);
