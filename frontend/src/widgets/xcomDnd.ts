import { type DragEvent, useEffect, useRef, useState } from "react";
import { create } from "zustand";

import type { XComRef } from "../lib/types";

/**
 * Drag-to-map plumbing between the inspector "Inputs" panel (upstream task outputs) and
 * parameter fields. A drag carries both our JSON payload and a `text/plain` Jinja snippet,
 * so dropping straight into a CodeMirror editor inserts the template at the drop point.
 */
const XCOM_MIME = "application/x-autoair-xcom";

export type XComPayload = {
  /** Builder node id of the producing task. */
  node: string;
  /** Effective Airflow task id (group prefixes included). */
  taskId: string;
  /** XCom key (multiple_outputs dict key) — omitted for the return value. */
  key?: string | null;
};

const pyStr = (s: string) => `'${s.replace(/\\/g, "\\\\").replace(/'/g, "\\'")}'`;

/** `{{ ti.xcom_pull(task_ids='extract', key='rows') }}` */
export function xcomTemplate(p: Pick<XComPayload, "taskId" | "key">): string {
  const key = p.key ? `, key=${pyStr(p.key)}` : "";
  return `{{ ti.xcom_pull(task_ids=${pyStr(p.taskId)}${key}) }}`;
}

export function xcomRef(p: Pick<XComPayload, "node" | "key">): XComRef {
  return p.key ? { $type: "xcom", node: p.node, key: p.key } : { $type: "xcom", node: p.node };
}

export function setXcomDragData(e: DragEvent, p: XComPayload): void {
  e.dataTransfer.setData(XCOM_MIME, JSON.stringify(p));
  e.dataTransfer.setData("text/plain", xcomTemplate(p));
  e.dataTransfer.effectAllowed = "copy";
}

function readXcomDragData(e: DragEvent): XComPayload | null {
  const raw = e.dataTransfer.getData(XCOM_MIME);
  if (!raw) return null;
  try {
    const v = JSON.parse(raw) as Partial<XComPayload>;
    return typeof v.node === "string" && typeof v.taskId === "string"
      ? { node: v.node, taskId: v.taskId, key: v.key ?? null }
      : null;
  } catch {
    return null;
  }
}

const isXcomDrag = (e: DragEvent): boolean => e.dataTransfer.types.includes(XCOM_MIME);

/** A field that can receive an upstream output via the "Insert" button. */
type InsertTarget = {
  id: string;
  label: string;
  /** Templated fields receive a Jinja snippet at the cursor. */
  insertTemplate?: (text: string) => void;
  /** Non-templated fields receive an XCom reference value. */
  setXcom?: (ref: XComRef) => void;
};

type TargetState = {
  target: InsertTarget | null;
  setTarget: (t: InsertTarget) => void;
  clearTarget: (id: string) => void;
};

/** The last focused parameter field ("Insert" in the Inputs panel writes into it). */
export const useInsertTarget = create<TargetState>()((set) => ({
  target: null,
  setTarget: (target) => set({ target }),
  clearTarget: (id) => set((s) => (s.target?.id === id ? { target: null } : s)),
}));

export function applyToTarget(target: InsertTarget, p: XComPayload): void {
  if (target.insertTemplate) target.insertTemplate(xcomTemplate(p));
  else target.setXcom?.(xcomRef(p));
}

/**
 * Drop-zone props for a field wrapper. `onPayload` receives the dragged output; the
 * returned `isOver` drives a highlight.
 */
export function useXcomDrop(onPayload: ((p: XComPayload) => void) | null) {
  const [isOver, setOver] = useState(false);
  if (!onPayload) return { isOver: false, dropProps: {} };
  return {
    isOver,
    dropProps: {
      onDragOver: (e: DragEvent) => {
        if (!isXcomDrag(e)) return;
        e.preventDefault();
        e.dataTransfer.dropEffect = "copy";
        if (!isOver) setOver(true);
      },
      onDragLeave: () => setOver(false),
      onDrop: (e: DragEvent) => {
        setOver(false);
        // CodeMirror already inserted the text/plain snippet at the drop point.
        if (e.nativeEvent.defaultPrevented) return;
        const p = readXcomDragData(e);
        if (!p) return;
        e.preventDefault();
        e.stopPropagation();
        onPayload(p);
      },
    },
  };
}

/**
 * Stable `(v) => onChange(v)` that always calls the latest `onChange` (insert targets are
 * registered on focus but may fire after later edits re-created the handler).
 */
export function useLatest<A extends unknown[]>(fn: (...args: A) => void): (...args: A) => void {
  const ref = useRef(fn);
  useEffect(() => {
    ref.current = fn;
  });
  const [stable] = useState(
    () =>
      (...args: A) =>
        ref.current(...args),
  );
  return stable;
}
