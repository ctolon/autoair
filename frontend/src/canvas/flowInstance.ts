import type { ReactFlowInstance, XYPosition } from "@xyflow/react";

import type { BuilderEdge, BuilderNode } from "../lib/types";

/**
 * The mounted canvas, for imperative actions run outside React (commands, palette, menus).
 * Set by `FlowCanvas`; null while the canvas is not mounted.
 */
let instance: ReactFlowInstance<BuilderNode, BuilderEdge> | null = null;
let element: HTMLElement | null = null;
/** Last pointer position over the canvas (client coordinates); null when the pointer left it. */
let pointer: XYPosition | null = null;

export function setFlowInstance(rf: ReactFlowInstance<BuilderNode, BuilderEdge> | null, el: HTMLElement | null) {
  instance = rf;
  element = el;
}

export function getFlow(): ReactFlowInstance<BuilderNode, BuilderEdge> | null {
  return instance;
}

export function setPointer(p: XYPosition | null): void {
  pointer = p;
}

/** Canvas coordinates of the visible center (fallback: a fixed point). */
export function viewportCenter(): XYPosition {
  if (!instance || !element) return { x: 120, y: 120 };
  const r = element.getBoundingClientRect();
  return instance.screenToFlowPosition({ x: r.left + r.width / 2, y: r.top + r.height / 2 });
}

/** Canvas coordinates under the pointer when it is over the canvas. */
export function pointerFlowPosition(): XYPosition | null {
  if (!instance || !pointer) return null;
  return instance.screenToFlowPosition(pointer);
}

export function screenToFlow(p: XYPosition): XYPosition {
  return instance ? instance.screenToFlowPosition(p) : p;
}
