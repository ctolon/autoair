import type { XYPosition } from "@xyflow/react";
import { create } from "zustand";

/** What a right click / context-menu key was aimed at. */
type ContextTarget = { kind: "pane" } | { kind: "node"; id: string } | { kind: "edge"; id: string };

/** Where the mini "insert node" popover was opened from. */
export type InsertMode =
  | { kind: "point" }
  | { kind: "edge"; edgeId: string }
  | { kind: "connect"; nodeId: string; handleType: "source" | "target" };

export type InsertRequest = {
  /** Viewport (client) coordinates of the popover. */
  screen: XYPosition;
  /** Canvas coordinates where a new node goes. */
  flow: XYPosition;
  mode: InsertMode;
};

type CanvasUi = {
  snapToGrid: boolean;
  showMinimap: boolean;
  contextTarget: ContextTarget;
  insert: InsertRequest | null;
  editingEdgeId: string | null;
  toggleSnap: () => void;
  toggleMinimap: () => void;
  setContextTarget: (t: ContextTarget) => void;
  openInsert: (req: InsertRequest) => void;
  closeInsert: () => void;
  setEditingEdge: (id: string | null) => void;
};

const PREFS_KEY = "autoair.canvas.prefs";

function readPrefs(): { snapToGrid: boolean; showMinimap: boolean } {
  const defaults = { snapToGrid: false, showMinimap: true };
  try {
    const raw = localStorage.getItem(PREFS_KEY);
    if (!raw) return defaults;
    const v = JSON.parse(raw) as Partial<typeof defaults>;
    return {
      snapToGrid: typeof v.snapToGrid === "boolean" ? v.snapToGrid : defaults.snapToGrid,
      showMinimap: typeof v.showMinimap === "boolean" ? v.showMinimap : defaults.showMinimap,
    };
  } catch {
    return defaults;
  }
}

function writePrefs(p: { snapToGrid: boolean; showMinimap: boolean }): void {
  try {
    localStorage.setItem(PREFS_KEY, JSON.stringify(p));
  } catch {
    /* storage unavailable */
  }
}

/** Canvas UI state (never part of the document or the undo history). */
export const useCanvasUi = create<CanvasUi>()((set, get) => ({
  ...readPrefs(),
  contextTarget: { kind: "pane" },
  insert: null,
  editingEdgeId: null,
  toggleSnap: () => {
    set({ snapToGrid: !get().snapToGrid });
    writePrefs({ snapToGrid: get().snapToGrid, showMinimap: get().showMinimap });
  },
  toggleMinimap: () => {
    set({ showMinimap: !get().showMinimap });
    writePrefs({ snapToGrid: get().snapToGrid, showMinimap: get().showMinimap });
  },
  setContextTarget: (contextTarget) => set({ contextTarget }),
  openInsert: (insert) => set({ insert }),
  closeInsert: () => set({ insert: null }),
  setEditingEdge: (editingEdgeId) => set({ editingEdgeId }),
}));

export const SNAP_GRID: [number, number] = [16, 16];
