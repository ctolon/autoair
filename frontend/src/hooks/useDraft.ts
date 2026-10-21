import { useEffect } from "react";

import type { DagSpec } from "../api/gen/types.gen";
import { i18n } from "../i18n";
import { loadAsUnsaved, openDag } from "../lifecycle/actions";
import { useBuilder } from "../store/builder";
import { toaster } from "../ui/toaster";

const KEY = "autoair.draft.v1";
const SAVE_DELAY = 800;

type Draft = { spec: DagSpec; savedDagId: string | null; savedAt: number };

function readDraft(): Draft | null {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return null;
    const d = JSON.parse(raw) as Partial<Draft>;
    return d.spec && typeof d.savedAt === "number" ? (d as Draft) : null;
  } catch {
    return null;
  }
}

function writeDraft(draft: Draft | null): void {
  try {
    if (draft) localStorage.setItem(KEY, JSON.stringify(draft));
    else localStorage.removeItem(KEY);
  } catch {
    /* storage full or unavailable: the beforeunload prompt still protects the work */
  }
}

/** Puts a draft back on the canvas (re-attaching it to its stored DAG when it had one). */
async function restoreDraft(draft: Draft): Promise<void> {
  if (draft.savedDagId) await openDag(draft.savedDagId, { confirm: false, quiet404: true });
  loadAsUnsaved(draft.spec, { keepIdentity: true });
}

/**
 * Unsaved work survives reloads and closed tabs: the dirty document is kept as a local draft
 * (this browser only), leaving the page asks first, and the next visit offers to restore it.
 */
export function useDraftProtection(): void {
  // Offer to restore a draft left by an earlier session (once, on mount).
  useEffect(() => {
    const draft = readDraft();
    if (!draft || useBuilder.getState().dirty) return;
    const t = (key: string, opts?: Record<string, unknown>) => i18n.t(key, { ns: "lifecycle", ...opts });
    toaster.create({
      type: "info",
      title: t("draft.found", { dagId: draft.spec.dag_id }),
      description: t("draft.savedAt", { time: new Date(draft.savedAt).toLocaleString(i18n.language) }),
      duration: Infinity,
      closable: true,
      action: { label: t("draft.restore"), onClick: () => void restoreDraft(draft) },
    });
  }, []);

  // Keep the draft in sync with the document (debounced); clear it once saved.
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    const unsubscribe = useBuilder.subscribe((s, prev) => {
      if (s.dirty === prev.dirty && s.nodes === prev.nodes && s.edges === prev.edges && s.dag === prev.dag) return;
      clearTimeout(timer);
      if (!s.dirty) {
        // Saved (or discarded on purpose): the draft is obsolete. A clean load at startup
        // (deep link, new DAG) must not delete a draft the user hasn't decided about yet.
        if (prev.dirty) writeDraft(null);
        return;
      }
      timer = setTimeout(() => {
        const st = useBuilder.getState();
        if (st.dirty) writeDraft({ spec: st.toSpec(), savedDagId: st.savedDagId, savedAt: Date.now() });
      }, SAVE_DELAY);
    });
    return () => {
      clearTimeout(timer);
      unsubscribe();
    };
  }, []);

  // Leaving with unsaved changes asks first (browsers show their own wording).
  useEffect(() => {
    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      if (!useBuilder.getState().dirty) return;
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, []);
}
