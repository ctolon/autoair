import type { XYPosition } from "@xyflow/react";

import type { BuilderNode } from "../lib/types";
import { ancestors } from "./graph";

export const TASK_SIZE = { width: 220, height: 64 } as const;
export const NOTE_SIZE = { width: 240, height: 140 } as const;
export const GROUP_SIZE = { width: 360, height: 220 } as const;
/** Size of a collapsed task group on the canvas. */
export const COLLAPSED_GROUP_SIZE = { width: 240, height: 64 } as const;
/** Room kept around members when a task group is created around them (header on top). */
export const GROUP_PADDING = { x: 24, top: 56, bottom: 24 } as const;

export type Box = { x: number; y: number; width: number; height: number };

/** Absolute canvas position of a node (sums parent offsets). */
export function absPos(nodes: readonly BuilderNode[], id: string, byId?: Map<string, BuilderNode>): XYPosition {
  const index = byId ?? new Map(nodes.map((n) => [n.id, n]));
  let n = index.get(id);
  let x = 0;
  let y = 0;
  const seen = new Set<string>();
  while (n && !seen.has(n.id)) {
    seen.add(n.id);
    x += n.position.x;
    y += n.position.y;
    n = n.parentId ? index.get(n.parentId) : undefined;
  }
  return { x, y };
}

const num = (v: unknown): number | undefined => {
  const n = typeof v === "number" ? v : typeof v === "string" ? Number.parseFloat(v) : Number.NaN;
  return Number.isFinite(n) && n > 0 ? n : undefined;
};

/** Rendered (or best guess) size of a node. */
export function nodeSize(n: BuilderNode): { width: number; height: number } {
  const fallback = n.type === "task_group" ? GROUP_SIZE : n.type === "note" ? NOTE_SIZE : TASK_SIZE;
  return {
    width: num(n.style?.width) ?? n.measured?.width ?? n.width ?? fallback.width,
    height: num(n.style?.height) ?? n.measured?.height ?? n.height ?? fallback.height,
  };
}

/** Absolute bounding box of a node. */
export function absBox(nodes: readonly BuilderNode[], n: BuilderNode, byId?: Map<string, BuilderNode>): Box {
  return { ...absPos(nodes, n.id, byId), ...nodeSize(n) };
}

/** Union of boxes (null for an empty list). */
export function unionBox(boxes: readonly Box[]): Box | null {
  if (!boxes.length) return null;
  let minX = Number.POSITIVE_INFINITY;
  let minY = Number.POSITIVE_INFINITY;
  let maxX = Number.NEGATIVE_INFINITY;
  let maxY = Number.NEGATIVE_INFINITY;
  for (const b of boxes) {
    minX = Math.min(minX, b.x);
    minY = Math.min(minY, b.y);
    maxX = Math.max(maxX, b.x + b.width);
    maxY = Math.max(maxY, b.y + b.height);
  }
  return { x: minX, y: minY, width: maxX - minX, height: maxY - minY };
}

/** Deepest (non-collapsed) task group whose absolute box contains `point`, excluding `excludeId`'s subtree. */
export function groupAt(nodes: readonly BuilderNode[], point: XYPosition, excludeId?: string): string | null {
  const byId = new Map(nodes.map((n) => [n.id, n]));
  let best: { id: string; depth: number } | null = null;
  for (const n of nodes) {
    if (n.type !== "task_group" || n.id === excludeId || n.data.collapsed) continue;
    const anc = ancestors(nodes as BuilderNode[], n.id);
    if (excludeId && anc.has(excludeId)) continue;
    if ([...anc].some((a) => (byId.get(a)?.data as { collapsed?: boolean } | undefined)?.collapsed)) continue;
    const b = absBox(nodes, n, byId);
    if (point.x >= b.x && point.x <= b.x + b.width && point.y >= b.y && point.y <= b.y + b.height) {
      if (!best || anc.size > best.depth) best = { id: n.id, depth: anc.size };
    }
  }
  return best?.id ?? null;
}

/** Ids of `ids` that have no ancestor in `ids` (the "roots" of a selection). */
export function topLevelOf(nodes: readonly BuilderNode[], ids: Iterable<string>): string[] {
  const set = new Set(ids);
  return [...set].filter((id) => ![...ancestors(nodes as BuilderNode[], id)].some((a) => set.has(a)));
}

/** All descendants of the given node ids (not including them). */
export function descendantsOf(nodes: readonly BuilderNode[], ids: Iterable<string>): Set<string> {
  const children = new Map<string, string[]>();
  for (const n of nodes) {
    if (!n.parentId) continue;
    children.set(n.parentId, [...(children.get(n.parentId) ?? []), n.id]);
  }
  const out = new Set<string>();
  const stack = [...ids];
  while (stack.length) {
    const cur = stack.pop() as string;
    for (const c of children.get(cur) ?? []) {
      if (out.has(c)) continue;
      out.add(c);
      stack.push(c);
    }
  }
  return out;
}

/** React Flow needs parents before children in the nodes array. */
export function sortParentsFirst<T extends BuilderNode>(nodes: T[]): T[] {
  const depth = new Map(nodes.map((n) => [n.id, ancestors(nodes, n.id).size]));
  return [...nodes].sort((a, b) => (depth.get(a.id) ?? 0) - (depth.get(b.id) ?? 0));
}

const overlaps = (a: Box, b: Box, gap: number) =>
  a.x < b.x + b.width + gap && b.x < a.x + a.width + gap && a.y < b.y + b.height + gap && b.y < a.y + a.height + gap;

/**
 * The nearest spot at or below/right of `start` where a node of `size` does not cover another
 * task or note (groups may contain it). Used when inserting without an explicit drop point.
 */
export function freeSpot(
  nodes: readonly BuilderNode[],
  start: XYPosition,
  size: { width: number; height: number } = TASK_SIZE,
  gap = 16,
): XYPosition {
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const boxes = nodes.filter((n) => n.type !== "task_group").map((n) => absBox(nodes, n, byId));
  const stepY = size.height + gap * 2;
  const stepX = size.width + gap * 3;
  for (let col = 0; col < 12; col++) {
    for (let row = 0; row < 12; row++) {
      const candidate = { x: start.x + col * stepX, y: start.y + row * stepY, ...size };
      if (!boxes.some((b) => overlaps(candidate, b, gap))) return { x: candidate.x, y: candidate.y };
    }
  }
  return start;
}
