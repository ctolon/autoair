import { syncPythonCode } from "../api/gen/sdk.gen";
import type { CodeProblem, CodeSyncResult, SpecDiff } from "../api/gen/types.gen";
import { errorMessage } from "../api/setup";
import { undo } from "../canvas/actions";
import { i18n } from "../i18n";
import { useBuilder } from "../store/builder";
import { toaster } from "../ui/toaster";
import type { CodeDiagnostic } from "../widgets/code";
import { IDLE_CHECK, useCodeMode } from "./store";

const t = (key: string, opts?: Record<string, unknown>) => i18n.t(key, { ns: "lifecycle", ...opts });

/** Pause after the last keystroke before the draft is checked on the server. */
const CHECK_DELAY = 1200;

/** Imports `source` on the server against the current document (nothing is changed). */
export async function syncCode(source: string, signal?: AbortSignal): Promise<CodeSyncResult> {
  const current = useBuilder.getState().toSpec();
  const res = await syncPythonCode({
    body: { source, current, dag_id: current.dag_id },
    ...(signal ? { signal } : {}),
    throwOnError: true,
  });
  return res.data;
}

/** Problems as editor diagnostics (unlocated ones go on line 1). */
export function toDiagnostics(problems: CodeProblem[] | undefined): CodeDiagnostic[] {
  return (problems ?? []).map((p) => ({
    line: p.line ?? 1,
    column: p.column ? p.column - 1 : null,
    message: p.message,
    severity: p.severity,
  }));
}

/** "+1 −0 ~2" style counts of a diff. */
export function diffCounts(diff: SpecDiff | null | undefined): { added: number; removed: number; changed: number } {
  return {
    added: diff?.added_nodes?.length ?? 0,
    removed: diff?.removed_nodes?.length ?? 0,
    changed: (diff?.changed_nodes ?? []).filter((n) => !n.layout_only).length + (diff?.changed_fields?.length ?? 0),
  };
}

let controller: AbortController | null = null;

/** Checks the current draft now (cancels a check in flight). */
export async function checkDraft(): Promise<CodeSyncResult | null> {
  const { draft, setCheck } = useCodeMode.getState();
  controller?.abort();
  if (draft === null) {
    setCheck(IDLE_CHECK);
    return null;
  }
  const ctrl = new AbortController();
  controller = ctrl;
  setCheck({ status: "checking", source: draft, result: useCodeMode.getState().check.result, error: null });
  try {
    const result = await syncCode(draft, ctrl.signal);
    if (ctrl.signal.aborted || useCodeMode.getState().draft !== draft) return null;
    setCheck({ status: result.ok ? "ok" : "error", source: draft, result, error: null });
    return result;
  } catch (e) {
    if (ctrl.signal.aborted) return null;
    setCheck({ status: "error", source: draft, result: null, error: errorMessage(e) });
    return null;
  }
}

/**
 * Debounced server check of every draft change (one subscription for the whole app, so the
 * full-screen code mode and the inline Code tab share it). Returns the unsubscribe function.
 */
export function startDraftChecks(delay = CHECK_DELAY): () => void {
  let timer: ReturnType<typeof setTimeout> | undefined;
  const unsubscribe = useCodeMode.subscribe((s, prev) => {
    if (s.draft === prev.draft) return;
    clearTimeout(timer);
    if (s.draft === null) {
      controller?.abort();
      return;
    }
    timer = setTimeout(() => void checkDraft(), delay);
  });
  return () => {
    clearTimeout(timer);
    unsubscribe();
  };
}

/**
 * Applies the draft: imports it (or reuses the fresh check of the same text) and replaces the
 * visual spec as one undoable step. On problems the draft is kept and they are shown inline.
 */
export async function applyDraft(): Promise<boolean> {
  const st = useCodeMode.getState();
  const draft = st.draft;
  if (draft === null || st.applying) return false;
  st.setApplying(true);
  try {
    const fresh = st.check.source === draft && st.check.status !== "checking" ? st.check.result : null;
    const result = fresh ?? (await checkDraft());
    if (!result) {
      const error = useCodeMode.getState().check.error;
      if (error) toaster.create({ type: "error", title: t("codeMode.applyFailed"), description: error });
      return false;
    }
    if (!result.ok || !result.spec) {
      toaster.create({
        type: "error",
        title: t("codeMode.notApplied"),
        description: t("codeMode.fixProblems", {
          count: (result.problems ?? []).filter((p) => p.severity === "error").length,
        }),
      });
      return false;
    }
    useBuilder.getState().replaceSpec(result.spec);
    useCodeMode.getState().discard();
    const c = diffCounts(result.diff);
    toaster.create({
      type: "success",
      title: t("codeMode.applied"),
      description: t("codeMode.appliedSummary", c),
      action: { label: t("codeMode.undo"), onClick: () => undo() },
    });
    return true;
  } finally {
    useCodeMode.getState().setApplying(false);
  }
}
