import { create } from "zustand";

type ColorMode = "light" | "dark";

const STORAGE_KEY = "autoair.colorMode";

/**
 * Inside Airflow the host owns the theme: we follow it (class / color-scheme on <html>) and
 * offer no toggle. Standalone, the user picks light/dark; we set `class="dark"|"light"` on
 * <html> (Chakra's dark condition) without depending on next-themes.
 */
const HOST_THEMED = import.meta.env.MODE === "plugin";

function readHost(): ColorMode {
  if (typeof document === "undefined") return "light";
  const root = document.documentElement;
  if (root.classList.contains("dark")) return "dark";
  if (root.classList.contains("light")) return "light";
  if (root.style.colorScheme === "dark" || root.dataset.theme === "dark") return "dark";
  return "light";
}

function readStored(): ColorMode {
  try {
    const v = localStorage.getItem(STORAGE_KEY);
    if (v === "dark" || v === "light") return v;
  } catch {
    /* storage unavailable */
  }
  return typeof window !== "undefined" && window.matchMedia?.("(prefers-color-scheme: dark)").matches
    ? "dark"
    : "light";
}

function apply(mode: ColorMode): void {
  if (typeof document === "undefined") return;
  const root = document.documentElement;
  root.classList.remove(mode === "dark" ? "light" : "dark");
  root.classList.add(mode);
  root.style.colorScheme = mode;
}

type ColorModeState = {
  mode: ColorMode;
  /** False inside Airflow (the host theme wins). */
  canToggle: boolean;
  setMode: (mode: ColorMode) => void;
  toggle: () => void;
};

export const useColorMode = create<ColorModeState>()((set, get) => {
  const initial = HOST_THEMED ? readHost() : readStored();
  if (!HOST_THEMED) apply(initial);
  else if (typeof MutationObserver !== "undefined" && typeof document !== "undefined") {
    new MutationObserver(() => {
      const mode = readHost();
      if (mode !== get().mode) set({ mode });
    }).observe(document.documentElement, { attributes: true, attributeFilter: ["class", "style", "data-theme"] });
  }
  return {
    mode: initial,
    canToggle: !HOST_THEMED,
    setMode: (mode) => {
      if (HOST_THEMED) return;
      apply(mode);
      try {
        localStorage.setItem(STORAGE_KEY, mode);
      } catch {
        /* ignore */
      }
      set({ mode });
    },
    toggle: () => get().setMode(get().mode === "dark" ? "light" : "dark"),
  };
});
