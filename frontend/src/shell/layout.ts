import { create } from "zustand";

export type PanelId = "palette" | "inspector" | "bottom";
export type SidePanelId = Exclude<PanelId, "bottom">;

type Collapsed = Record<PanelId, boolean>;
type Sizes = Record<PanelId, number>;
type Overlay = Record<SidePanelId, boolean>;

const KEY = "autoair.layout.collapsed";
const SIZES_KEY = "autoair.layout.sizes";
const DEFAULTS: Collapsed = { palette: false, inspector: false, bottom: false };

/** Panel sizes in CSS pixels (width of the side panels, height of the bottom panel). */
export const DEFAULT_SIZES: Sizes = { palette: 280, inspector: 380, bottom: 280 };
export const MIN_SIZES: Sizes = { palette: 200, inspector: 280, bottom: 120 };
/** The canvas never gets narrower/shorter than this while panels are resized. */
export const MIN_CANVAS = { width: 320, height: 160 } as const;

/**
 * Responsive breakpoints (width of the builder, not of the window: inside Airflow the builder
 * is narrower than the page). Below `inspector` the inspector becomes an overlay drawer, below
 * `palette` the palette too.
 */
const OVERLAY_BELOW: Record<SidePanelId, number> = { inspector: 1100, palette: 720 };

function readJson<T>(key: string): Partial<T> | null {
  try {
    const raw = localStorage.getItem(key);
    return raw ? (JSON.parse(raw) as Partial<T>) : null;
  } catch {
    return null;
  }
}

function readCollapsed(): Collapsed {
  const v = readJson<Collapsed>(KEY);
  if (!v) return DEFAULTS;
  return { palette: v.palette === true, inspector: v.inspector === true, bottom: v.bottom === true };
}

function readSizes(): Sizes {
  const v = readJson<Sizes>(SIZES_KEY) ?? {};
  const pick = (id: PanelId) => {
    const n = v[id];
    return typeof n === "number" && Number.isFinite(n) && n >= MIN_SIZES[id] && n < 4000
      ? Math.round(n)
      : DEFAULT_SIZES[id];
  };
  return { palette: pick("palette"), inspector: pick("inspector"), bottom: pick("bottom") };
}

function write(key: string, value: unknown): void {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* storage unavailable (private mode, sandbox) */
  }
}

/** Overlay mode of each side panel for a builder of `width` pixels. */
export function overlayFor(width: number): Overlay {
  return { palette: width < OVERLAY_BELOW.palette, inspector: width < OVERLAY_BELOW.inspector };
}

type LayoutState = {
  /** Docked panels the user hid (VS Code style; remembered per browser). */
  collapsed: Collapsed;
  sizes: Sizes;
  /** Side panels shown as overlay drawers (narrow builder); not persisted. */
  overlay: Overlay;
  /** The open overlay drawer, if any. */
  drawer: SidePanelId | null;
  toggle: (id: PanelId) => void;
  /** Hide (`true`) or show (`false`) a panel: docked panels collapse, overlay panels close. */
  set: (id: PanelId, collapsed: boolean) => void;
  setSize: (id: PanelId, px: number) => void;
  setOverlay: (overlay: Overlay) => void;
};

/** Whether panel `id` is visible (a docked panel expanded, or its overlay drawer open). */
export function isPanelOpen(s: Pick<LayoutState, "collapsed" | "overlay" | "drawer">, id: PanelId): boolean {
  if (id !== "bottom" && s.overlay[id]) return s.drawer === id;
  return !s.collapsed[id];
}

/**
 * Which panels are visible (⌘/Ctrl+B palette, ⌘/Ctrl+I inspector, ⌘/Ctrl+J bottom panel) and
 * their sizes. `Workbench` renders from this store; the shortcuts, the edge buttons and the
 * rails only change it.
 */
export const useLayout = create<LayoutState>()((set, get) => ({
  collapsed: readCollapsed(),
  sizes: readSizes(),
  overlay: { palette: false, inspector: false },
  drawer: null,
  toggle: (id) => get().set(id, isPanelOpen(get(), id)),
  set: (id, value) => {
    const s = get();
    if (id !== "bottom" && s.overlay[id]) {
      const drawer = value ? (s.drawer === id ? null : s.drawer) : id;
      if (drawer !== s.drawer) set({ drawer });
      return;
    }
    if (s.collapsed[id] === value) return;
    const collapsed = { ...s.collapsed, [id]: value };
    write(KEY, collapsed);
    set({ collapsed });
  },
  setSize: (id, px) => {
    const size = Math.max(MIN_SIZES[id], Math.round(px));
    if (get().sizes[id] === size) return;
    const sizes = { ...get().sizes, [id]: size };
    write(SIZES_KEY, sizes);
    set({ sizes });
  },
  setOverlay: (overlay) => {
    const s = get();
    if (s.overlay.palette === overlay.palette && s.overlay.inspector === overlay.inspector) return;
    const drawer = s.drawer && overlay[s.drawer] ? s.drawer : null;
    set({ overlay, drawer });
  },
}));
