import { create } from "zustand";

export const ADMIN_TABS = [
  "overview",
  "features",
  "audit",
  "users",
  "ai",
  "aiRules",
  "mcp",
  "templates",
  "proposals",
  "database",
  "catalog",
  "kb",
  "caches",
  "config",
] as const;
export type AdminTab = (typeof ADMIN_TABS)[number];

interface AdminState {
  open: boolean;
  tab: AdminTab;
  openPanel: (tab?: AdminTab) => void;
  close: () => void;
  setTab: (tab: AdminTab) => void;
}

/** Whether the full-page admin panel is shown, and its current tab. */
export const useAdmin = create<AdminState>()((set) => ({
  open: false,
  tab: "overview",
  openPanel: (tab) => set((s) => ({ open: true, tab: tab ?? s.tab })),
  close: () => set({ open: false }),
  setTab: (tab) => set({ tab }),
}));

/** Permission that unlocks the admin panel (`admin:panel`, granted to the Admin role). */
const ADMIN_PERMISSION = "admin:panel";

export const canOpenAdmin = (permissions: readonly string[] | undefined): boolean =>
  !!permissions?.includes(ADMIN_PERMISSION);
