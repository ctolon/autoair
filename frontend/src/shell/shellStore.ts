import type { XYPosition } from "@xyflow/react";
import { create } from "zustand";

type PaletteMode = "all" | "insert";

type ShellState = {
  palette: { open: boolean; mode: PaletteMode; at: XYPosition | null };
  shortcutsOpen: boolean;
  openPalette: (mode?: PaletteMode, at?: XYPosition | null) => void;
  closePalette: () => void;
  setShortcutsOpen: (open: boolean) => void;
};

/** Global shell UI: command palette and shortcuts sheet. */
export const useShell = create<ShellState>()((set) => ({
  palette: { open: false, mode: "all", at: null },
  shortcutsOpen: false,
  openPalette: (mode = "all", at = null) => set({ palette: { open: true, mode, at } }),
  closePalette: () => set((s) => ({ palette: { ...s.palette, open: false } })),
  setShortcutsOpen: (shortcutsOpen) => set({ shortcutsOpen }),
}));
