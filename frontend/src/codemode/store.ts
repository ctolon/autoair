import { create } from "zustand";

import type { CodeSyncResult } from "../api/gen/types.gen";
import type { Access } from "../lib/access";
import { useBuilder } from "../store/builder";

type EditorMode = "visual" | "code";

type CheckState = {
  status: "idle" | "checking" | "ok" | "error";
  /** The draft text the result belongs to (a newer draft makes it stale). */
  source: string | null;
  result: CodeSyncResult | null;
  /** Request failure (network, 403…), as opposed to problems in the code. */
  error: string | null;
};

const IDLE: CheckState = { status: "idle", source: null, result: null, error: null };

/**
 * Code mode: the generated DAG file edited as text. `draft` is null while the editor shows
 * the generated code; the first keystroke starts a draft (remembering the generated code it
 * started from, `base`). Applying imports the draft into the visual spec.
 */
export const useCodeMode = create<{
  mode: EditorMode;
  draft: string | null;
  base: string | null;
  check: CheckState;
  /** Last applied/checked import, kept for the summary after an apply. */
  applying: boolean;
  /**
   * Environment whose code the Code tab / code mode preview read-only (null: the base, which
   * is the only editable one). UI state: not part of the document or its history.
   */
  previewEnv: string | null;
  /** A raw code line to scroll to (Problems tab → raw editor); `seq` makes repeats count. */
  reveal: { line: number; column: number | null; seq: number } | null;
  setMode: (mode: EditorMode) => void;
  setPreviewEnv: (env: string | null) => void;
  revealLine: (line: number, column?: number | null) => void;
  edit: (text: string, generated: string | null) => void;
  discard: () => void;
  setCheck: (check: CheckState) => void;
  setApplying: (applying: boolean) => void;
}>()((set, get) => ({
  mode: "visual",
  draft: null,
  base: null,
  check: IDLE,
  applying: false,
  previewEnv: null,
  reveal: null,
  setMode: (mode) => {
    if (get().mode !== mode) set({ mode });
  },
  setPreviewEnv: (previewEnv) => set({ previewEnv }),
  revealLine: (line, column = null) => set((s) => ({ reveal: { line, column, seq: (s.reveal?.seq ?? 0) + 1 } })),
  edit: (text, generated) => {
    const s = get();
    // Typing back to exactly the generated code ends the draft.
    if (s.draft === null && text === generated) return;
    if (text === (s.base ?? generated) && s.draft !== null) {
      set({ draft: null, base: null, check: IDLE });
      return;
    }
    set({ draft: text, base: s.draft === null ? generated : s.base });
  },
  discard: () => set({ draft: null, base: null, check: IDLE }),
  setCheck: (check) => set({ check }),
  setApplying: (applying) => set({ applying }),
}));

export const IDLE_CHECK = IDLE;

/**
 * Which editors this deployment offers and what the user may do with code: `canEditCode`
 * (synced code mode: the importer runs the code), `canRaw` (write raw code DAGs, `code:raw`).
 */
export function editorAvailability(access: Pick<Access, "feature" | "can">) {
  const visual = access.feature("visual_editor");
  // The server refuses to switch both off; never leave the page without an editor.
  const code = access.feature("code_editor") || !visual;
  const python = access.feature("python_code");
  const canEditCode = access.can("spec:write") && access.can("code:execute") && python;
  const canRaw = access.feature("code_editor") && python && access.can("spec:write") && access.can("code:raw");
  return { visual, code, both: visual && code, canEditCode, canRaw };
}

/** The open document is a raw code DAG (`raw_code` set): edited as a file, no canvas. */
export function isRawDag(dag: { raw_code?: string | null }): boolean {
  return dag.raw_code !== undefined && dag.raw_code !== null;
}

/**
 * Lifecycle dialogs that read or rewrite the visual spec (tasks, edges, operator params).
 * A raw code DAG has none, so every entry point (palette, toolbar, inspector) hides them
 * and `openDialog` refuses them.
 */
export const SPEC_ONLY_DIALOGS: ReadonlySet<string> = new Set([
  "aiEdit",
  "aiFix",
  "aiReview",
  "aiLeaks",
  "aiOptimize",
  "aiTaskCode",
]);

/** The dialog `kind` needs a visual DAG and the open document is a raw code DAG. */
export function specOnlyBlocked(kind: string, dag: { raw_code?: string | null } = useBuilder.getState().dag): boolean {
  return SPEC_ONLY_DIALOGS.has(kind) && isRawDag(dag);
}

/** React hook: the open document is a raw code DAG. */
export function useIsRaw(): boolean {
  return useBuilder((s) => isRawDag(s.dag));
}

/**
 * The canvas is what the user sees (visual mode, a visual DAG): canvas-only commands and
 * shortcuts (delete, nudge, insert, layout…) apply only then. In code-only deployments the
 * canvas is not mounted at all, so its commands are not registered.
 */
export function canvasActive(): boolean {
  return useCodeMode.getState().mode !== "code" && !isRawDag(useBuilder.getState().dag);
}

/** The mode actually shown: a single enabled editor wins over the user's choice. */
export function effectiveMode(mode: EditorMode, availability: { visual: boolean; code: boolean }): EditorMode {
  if (!availability.visual) return "code";
  if (!availability.code) return "visual";
  return mode;
}

/** Generated-code changes since the draft started (the visual DAG changed meanwhile). */
export function isStale(draft: string | null, base: string | null, generated: string | null): boolean {
  return draft !== null && base !== null && generated !== null && base !== generated;
}
