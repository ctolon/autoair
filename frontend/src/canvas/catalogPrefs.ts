import { create } from "zustand";

const FAVORITES_KEY = "autoair.palette.favorites";
const RECENT_KEY = "autoair.palette.recent";
const RECENT_LIMIT = 10;

function readList(key: string): string[] {
  try {
    const raw = localStorage.getItem(key);
    const v: unknown = raw ? JSON.parse(raw) : [];
    return Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : [];
  } catch {
    return [];
  }
}

function writeList(key: string, list: string[]): void {
  try {
    localStorage.setItem(key, JSON.stringify(list));
  } catch {
    /* storage unavailable (private mode, quota): keep in memory only */
  }
}

type CatalogPrefs = {
  /** Insert-item keys (see `InsertItem.key`). */
  favorites: string[];
  /** Most recent first, at most `RECENT_LIMIT`. */
  recent: string[];
  toggleFavorite: (key: string) => void;
  recordRecent: (key: string) => void;
};

/** Palette favorites / recently used items, persisted per browser. */
export const useCatalogPrefs = create<CatalogPrefs>()((set, get) => ({
  favorites: readList(FAVORITES_KEY),
  recent: readList(RECENT_KEY),
  toggleFavorite: (key) => {
    const cur = get().favorites;
    const favorites = cur.includes(key) ? cur.filter((k) => k !== key) : [...cur, key];
    set({ favorites });
    writeList(FAVORITES_KEY, favorites);
  },
  recordRecent: (key) => {
    const recent = [key, ...get().recent.filter((k) => k !== key)].slice(0, RECENT_LIMIT);
    set({ recent });
    writeList(RECENT_KEY, recent);
  },
}));
