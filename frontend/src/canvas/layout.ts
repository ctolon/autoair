import type { ElkNode } from "elkjs/lib/elk-api";

import type { BuilderEdge, BuilderNode } from "../lib/types";
import { ELK_FALLBACK_GLOBAL } from "./elkFallback";
import { COLLAPSED_GROUP_SIZE, TASK_SIZE } from "./geometry";
import { getFlow } from "./flowInstance";
import { collapsedView } from "./grouping";
import { LOW_DETAIL_ZOOM } from "./largeGraph";
import LAYOUT_WORKER_URL from "./layout.worker.ts?worker&url";

type Elk = { layout: (g: ElkNode) => Promise<ElkNode> };

/**
 * ELK lives only in `layout.worker.ts`, emitted as its own ES module file. The worker loads it
 * by URL; the main-thread fallback imports the very same file, so ELK is bundled once. Vite
 * resolves the URL relative to the importing chunk (`base: "./"`), which also works for the
 * Airflow plugin bundle.
 */
let elkPromise: Promise<Elk> | null = null;
function getElk(): Promise<Elk> {
  elkPromise ??= (async () => {
    const [{ default: ELK }] = await Promise.all([
      import("elkjs/lib/elk-api.js"),
      import(/* @vite-ignore */ LAYOUT_WORKER_URL),
    ]);
    const FakeWorker = (globalThis as Record<string, unknown>)[ELK_FALLBACK_GLOBAL] as (new () => Worker) | undefined;
    if (!FakeWorker) throw new Error("ELK fallback unavailable");
    return new ELK({ workerFactory: () => new FakeWorker() });
  })();
  elkPromise.catch(() => (elkPromise = null));
  return elkPromise;
}

// ---- Web Worker transport (falls back to the main thread when workers are unavailable) ----

type Pending = {
  resolve: (g: ElkNode | undefined) => void;
  reject: (e: unknown) => void;
  timer: ReturnType<typeof setTimeout>;
};
class WorkerUnavailable extends Error {}

const WORKER_TIMEOUT_MS = 30_000;
let worker: Worker | null | undefined; // undefined: not tried yet; null: unavailable
let nextId = 1;
const pending = new Map<number, Pending>();

function disableWorker(reason: string): void {
  worker?.terminate();
  worker = null;
  for (const [id, p] of pending) {
    clearTimeout(p.timer);
    p.reject(new WorkerUnavailable(reason));
    pending.delete(id);
  }
}

async function getWorker(): Promise<Worker | null> {
  if (worker !== undefined) return worker;
  if (typeof Worker === "undefined") return (worker = null);
  try {
    const w = new Worker(LAYOUT_WORKER_URL, { type: "module", name: "autoair-layout" });
    w.onmessage = (e: MessageEvent<{ id: number; data?: ElkNode; error?: unknown }>) => {
      const p = pending.get(e.data.id);
      if (!p) return;
      pending.delete(e.data.id);
      clearTimeout(p.timer);
      if (e.data.error !== undefined) p.reject(e.data.error);
      else p.resolve(e.data.data);
    };
    w.onerror = (e) => {
      e.preventDefault();
      disableWorker(e.message || "layout worker failed");
    };
    worker = w;
    // Like elk-api: the worker knows no algorithm until they are registered.
    await request(w, { cmd: "register", algorithms: ["layered"] });
  } catch {
    disableWorker("layout worker unavailable");
  }
  return worker ?? null;
}

function request(w: Worker, message: Record<string, unknown>): Promise<ElkNode | undefined> {
  return new Promise((resolve, reject) => {
    const id = nextId++;
    const timer = setTimeout(() => disableWorker("layout worker timed out"), WORKER_TIMEOUT_MS);
    pending.set(id, { resolve, reject, timer });
    w.postMessage({ ...message, id });
  });
}

async function runLayout(graph: ElkNode): Promise<ElkNode> {
  const w = await getWorker();
  if (w) {
    try {
      const result = await request(w, { cmd: "layout", graph, layoutOptions: {}, options: {} });
      if (result) return result;
    } catch (e) {
      if (!(e instanceof WorkerUnavailable)) throw e;
    }
  }
  return (await getElk()).layout(graph);
}

const LAYOUT_OPTIONS = {
  "elk.algorithm": "layered",
  "elk.direction": "RIGHT",
  "elk.hierarchyHandling": "INCLUDE_CHILDREN",
  "elk.layered.spacing.nodeNodeBetweenLayers": "70",
  "elk.spacing.nodeNode": "40",
  "elk.padding": "[top=56,left=24,bottom=24,right=24]",
};

/**
 * Hierarchical (task-group aware) layered layout. ELK returns child coordinates relative
 * to their parent, which is exactly React Flow's `parentId` convention.
 */
export async function autoLayout(
  nodes: BuilderNode[],
  edges: BuilderEdge[],
): Promise<Map<string, { x: number; y: number; width?: number; height?: number }>> {
  // Collapsed task groups are laid out as plain boxes; their (hidden) members keep their place.
  const view = collapsedView(nodes);
  // Zoomed out, task cards are low-detail boxes and their measured size is not the size
  // of the full card: lay out with the default card size instead.
  const zoom = getFlow()?.getZoom() ?? 1;
  const measured = (n: BuilderNode) => (zoom < LOW_DETAIL_ZOOM && n.type !== "note" ? undefined : n.measured);
  const visible = nodes.filter((n) => !view.hidden.has(n.id));
  const elkNodes = new Map<string, ElkNode>();
  for (const n of visible) {
    const collapsed = n.type === "task_group" && n.data.collapsed;
    elkNodes.set(n.id, {
      id: n.id,
      ...(n.type === "task_group" && !collapsed
        ? { children: [], layoutOptions: LAYOUT_OPTIONS }
        : collapsed
          ? { ...COLLAPSED_GROUP_SIZE }
          : {
              width: measured(n)?.width ?? Number(n.style?.width ?? TASK_SIZE.width),
              height: measured(n)?.height ?? Number(n.style?.height ?? TASK_SIZE.height),
            }),
    });
  }
  const root: ElkNode = { id: "__root__", layoutOptions: LAYOUT_OPTIONS, children: [] };
  for (const n of visible) {
    const parent = n.parentId ? elkNodes.get(n.parentId) : root;
    (parent ?? root).children?.push(elkNodes.get(n.id) as ElkNode);
  }
  const seen = new Set<string>();
  root.edges = [];
  for (const e of edges) {
    const source = view.representative.get(e.source) ?? e.source;
    const target = view.representative.get(e.target) ?? e.target;
    const key = `${source}->${target}`;
    if (source === target || seen.has(key) || !elkNodes.has(source) || !elkNodes.has(target)) continue;
    seen.add(key);
    root.edges.push({ id: e.id, sources: [source], targets: [target] });
  }

  const result = await runLayout(root);
  const out = new Map<string, { x: number; y: number; width?: number; height?: number }>();
  const walk = (node: ElkNode) => {
    for (const child of node.children ?? []) {
      out.set(child.id, {
        x: child.x ?? 0,
        y: child.y ?? 0,
        ...(child.children ? { width: child.width, height: child.height } : {}),
      });
      walk(child);
    }
  };
  walk(result);
  return out;
}
