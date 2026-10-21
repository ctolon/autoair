import type { XYPosition } from "@xyflow/react";

import { getTemplate } from "../api/gen/sdk.gen";
import type { Template } from "../api/gen/types.gen";
import { errorMessage } from "../api/setup";
import { collabActive, collabRedo, collabUndo } from "../collab/session";
import { i18n } from "../i18n";
import type { BuilderNode, ClipboardFragment, FragmentInput, NodeSpec } from "../lib/types";
import { flushHistory, selectedEdgeIds, selectedNodeIds, useBuilder } from "../store/builder";
import { useLifecycle } from "../lifecycle/store";
import { toaster } from "../ui/toaster";
import { useCatalogPrefs } from "./catalogPrefs";
import { makeNoteSpec } from "./factories";
import { getFlow, pointerFlowPosition, viewportCenter } from "./flowInstance";
import { parseFragment } from "./fragment";
import { GROUP_SIZE, NOTE_SIZE, TASK_SIZE, descendantsOf, freeSpot, groupAt } from "./geometry";
import type { InsertItem, InsertPayload } from "./insertables";
import { instantiateBlueprint, requiredPaths } from "./operatorTemplates";
import { AUTO_FIT_MIN_ZOOM } from "./largeGraph";
import { autoLayout } from "./layout";

/**
 * Imperative canvas actions shared by keyboard commands, context menus, the node toolbar
 * and the command palette. They work on the store and the mounted React Flow instance.
 */

const t = (key: string, opts?: Record<string, unknown>) => i18n.t(key, { ns: "canvas", ...opts });
const builder = () => useBuilder.getState();

export const selection = () => selectedNodeIds(builder());
export const hasSelection = () => selection().length > 0;
export const hasAnySelection = () => hasSelection() || selectedEdgeIds(builder()).length > 0;

/** In-memory clipboard: used when the system clipboard is not readable (permissions, insecure context). */
let memoryClipboard: string | null = null;

export function serializeSelection(): string | null {
  const ids = selection();
  if (!ids.length) return null;
  const json = JSON.stringify(builder().exportFragment(ids), null, 2);
  memoryClipboard = json;
  return json;
}

/** Copy via the async clipboard API (menus / palette). Keyboard copy uses the native `copy` event. */
export async function copySelection(): Promise<boolean> {
  const json = serializeSelection();
  if (!json) return false;
  try {
    await navigator.clipboard.writeText(json);
  } catch {
    /* kept in the in-memory clipboard */
  }
  toaster.create({ type: "info", title: t("toast.copied", { count: selection().length }) });
  return true;
}

export async function cutSelection(): Promise<boolean> {
  const ids = selection();
  if (!(await copySelection())) return false;
  builder().removeElements(ids);
  return true;
}

/** Where pasted content goes: under the pointer when it is over the canvas, else offset from the source. */
function pastePosition(): XYPosition | undefined {
  return pointerFlowPosition() ?? undefined;
}

/** Inserts clipboard text when it is one of our fragments / a DAG spec. Returns false otherwise. */
export function pasteText(text: string, at: XYPosition | undefined = pastePosition()): boolean {
  const fragment = parseFragment(text);
  if (!fragment) return false;
  insertFragmentAt(fragment, at);
  return true;
}

/** Paste from menus / palette: system clipboard if readable, else the in-memory clipboard. */
export async function pasteFromClipboard(at?: XYPosition): Promise<void> {
  let text: string | null;
  try {
    text = await navigator.clipboard.readText();
  } catch {
    text = null;
  }
  if (text && pasteText(text, at ?? pastePosition())) return;
  if (memoryClipboard && pasteText(memoryClipboard, at ?? pastePosition())) return;
  toaster.create({ type: "warning", title: t("toast.nothingToPaste") });
}

export function canPaste(): boolean {
  return memoryClipboard !== null || typeof navigator.clipboard?.readText === "function";
}

/** Inserts a fragment, dropping it into the task group under `at` when there is one. */
function insertFragmentAt(fragment: FragmentInput | ClipboardFragment, at?: XYPosition): string[] {
  const parentId = at ? groupAt(builder().nodes, at) : null;
  return builder().insertFragment(fragment, at, { parentId });
}

export function duplicateSelection(): void {
  const ids = selection();
  if (ids.length) builder().duplicateNodes(ids);
}

export function deleteSelection(): void {
  const s = builder();
  s.removeElements(selectedNodeIds(s), selectedEdgeIds(s));
}

export function selectAll(): void {
  const { nodes, edges, setSelection } = builder();
  const hidden = descendantsOf(
    nodes,
    nodes.filter((n) => n.type === "task_group" && n.data.collapsed).map((n) => n.id),
  );
  setSelection(
    nodes.filter((n) => !hidden.has(n.id)).map((n) => n.id),
    edges.map((e) => e.id),
  );
}

export function clearSelection(): void {
  builder().setSelection([]);
}

export function groupSelection(): void {
  const ids = selection();
  if (!ids.length) return;
  builder().groupNodes(ids);
}

/** Groups to ungroup: selected groups, or the group of the single selected node. */
function ungroupTargets(): string[] {
  const { nodes } = builder();
  const ids = selection();
  return ids.filter((id) => nodes.find((n) => n.id === id)?.type === "task_group");
}

export const canUngroup = () => ungroupTargets().length > 0;

export function ungroupSelection(): void {
  for (const g of ungroupTargets()) builder().ungroup(g);
}

export function toggleCollapsed(groupId: string): void {
  const node = builder().nodes.find((n) => n.id === groupId);
  if (node?.type !== "task_group") return;
  builder().setCollapsed(groupId, !node.data.collapsed);
}

export function selectedGroups(): string[] {
  const { nodes } = builder();
  return selection().filter((id) => nodes.find((n) => n.id === id)?.type === "task_group");
}

export function nudgeSelection(dx: number, dy: number): void {
  builder().moveNodes(selection(), dx, dy);
}

/** "Fit view": the whole graph. `auto` (after a layout): not below `AUTO_FIT_MIN_ZOOM`. */
export function fitView(auto = false): void {
  void getFlow()?.fitView({ padding: 0.2, duration: 300, maxZoom: 1, ...(auto ? { minZoom: AUTO_FIT_MIN_ZOOM } : {}) });
}

let layoutRunning = false;

/** What a layout result is valid for: the node ids, their parents and collapsed groups. */
const layoutShape = (nodes: readonly BuilderNode[]) =>
  nodes.map((n) => `${n.id}:${n.parentId ?? ""}:${n.type === "task_group" && n.data.collapsed ? 1 : 0}`).join();

export async function runAutoLayout(): Promise<void> {
  if (layoutRunning) return;
  layoutRunning = true;
  try {
    const { nodes, edges } = builder();
    const layout = await autoLayout(nodes, edges);
    // ELK takes a while on large graphs: a result for a document that gained, lost or
    // re-parented nodes meanwhile (an edit, a collaborator, another DAG opened) is stale.
    if (layoutShape(builder().nodes) !== layoutShape(nodes)) return;
    builder().applyLayout(layout);
    requestAnimationFrame(() => fitView(true));
  } catch (e) {
    toaster.create({ type: "error", title: t("toast.layoutFailed"), description: errorMessage(e) });
  } finally {
    layoutRunning = false;
  }
}

/** Selects a node and brings it into view (expanding collapsed groups around it). */
export function focusNode(id: string): void {
  const s = builder();
  const node = s.nodes.find((n) => n.id === id);
  if (!node) return;
  let cur = node.parentId;
  while (cur) {
    const parent = s.nodes.find((n) => n.id === cur);
    if (parent?.type === "task_group" && parent.data.collapsed) s.setCollapsed(parent.id, false);
    cur = parent?.parentId;
  }
  s.setSelection([id]);
  requestAnimationFrame(() => {
    void getFlow()?.fitView({ nodes: [{ id }], padding: 0.6, duration: 400, maxZoom: 1.2 });
  });
}

/** Adds a single node spec at `at` (top-left, canvas coordinates), inside the task group there if any. */
export function insertSpec(spec: NodeSpec, at?: XYPosition): string {
  const s = builder();
  // Without an explicit drop point, don't stack new nodes on top of existing ones.
  if (!at) {
    const size = spec.kind === "note" ? NOTE_SIZE : spec.kind === "task_group" ? GROUP_SIZE : TASK_SIZE;
    const center = viewportCenter();
    at = freeSpot(s.nodes, { x: center.x - size.width / 2, y: center.y - size.height / 2 }, size);
  }
  const group = groupAt(s.nodes, at);
  const id = s.addNode(spec, at);
  if (group) s.setParent(id, group);
  s.setSelection([id]);
  return id;
}

/**
 * Inserts a task from an operator template. Templates with required inputs open a small
 * form first (the dialog inserts on submit); returns the new node id, or null when deferred.
 */
export function insertOperatorTemplate(
  tpl: Template,
  at?: XYPosition,
  inputs?: Readonly<Record<string, unknown>>,
): string | null {
  if (!inputs && requiredPaths(tpl).length) {
    useLifecycle.getState().openDialog({ kind: "operatorTemplateInputs", template: tpl, at });
    return null;
  }
  return insertSpec(instantiateBlueprint(tpl, builder().nodes, inputs), at);
}

/** Inserts an insert payload at `at` (default: the visible center). Returns the new node ids. */
export async function insertPayload(payload: InsertPayload, at?: XYPosition): Promise<string[]> {
  if (payload.kind === "spec") return [insertSpec(payload.spec, at)];
  try {
    // Template payloads (palette rows, drag & drop) are fragments or operator templates.
    const { data } = await getTemplate({ path: { template_id: payload.templateId }, throwOnError: true });
    if (data.blueprint) {
      const id = insertOperatorTemplate(data, at);
      return id ? [id] : [];
    }
    if (!data.fragment) throw new Error(t("toast.notAFragment"));
    return insertFragmentAt(data.fragment, at ?? viewportCenter());
  } catch (e) {
    toaster.create({ type: "error", title: t("toast.fragmentFailed"), description: errorMessage(e) });
    return [];
  }
}

/** Inserts a palette / command-palette item and records it as recently used. */
export async function insertItem(item: InsertItem, at?: XYPosition): Promise<string[]> {
  if (item.disabledReason != null) return [];
  useCatalogPrefs.getState().recordRecent(item.key);
  return insertPayload(item.payload(), at);
}

export function addNote(at?: XYPosition): string {
  return insertSpec(makeNoteSpec(), at ?? pointerFlowPosition() ?? undefined);
}

export function undo(): void {
  if (collabActive()) collabUndo();
  else {
    flushHistory();
    useBuilder.temporal.getState().undo();
  }
}

export function redo(): void {
  if (collabActive()) collabRedo();
  else {
    flushHistory();
    useBuilder.temporal.getState().redo();
  }
}
