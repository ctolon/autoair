import { useQuery } from "@tanstack/react-query";
import { useEffect, useSyncExternalStore } from "react";
import { create } from "zustand";

import { aiStatusOptions } from "../../api/gen/@tanstack/react-query.gen";
import type { AiStatus, Finding, FindingsReport, OptimizeReport, Usage } from "../../api/gen/types.gen";
import { useBuilder } from "../../store/builder";
import { focusNode } from "../../canvas/actions";
import { useFocusRequest } from "../../store/focus";

// ------------------------------------------------------------------ model selection (per browser)

const MODEL_KEY = "autoair.ai.model";
const listeners = new Set<() => void>();

function readModel(): string | null {
  try {
    return window.localStorage.getItem(MODEL_KEY);
  } catch {
    return null;
  }
}

/** Model chosen in the AI menu (null = the server's configured model). */
export function getAiModel(): string | null {
  return readModel();
}

export function setAiModel(model: string | null): void {
  try {
    if (model) window.localStorage.setItem(MODEL_KEY, model);
    else window.localStorage.removeItem(MODEL_KEY);
  } catch {
    // storage unavailable (private mode): the choice lasts for this page only
  }
  listeners.forEach((l) => l());
}

export function useAiModel(): string | null {
  return useSyncExternalStore(
    (cb) => {
      listeners.add(cb);
      return () => listeners.delete(cb);
    },
    readModel,
    () => null,
  );
}

let lastStatus: AiStatus | undefined;

export function useAiStatus(): AiStatus | undefined {
  const data = useQuery({ ...aiStatusOptions(), staleTime: 5 * 60_000 }).data;
  useEffect(() => {
    if (data) lastStatus = data;
  }, [data]);
  return data;
}

/** The last /ai/status seen by any component (for non-React callers such as commands). */
export function getAiStatus(): AiStatus | undefined {
  return lastStatus;
}

/**
 * Whether LLM features can be used: AI is switched on and either a model answers for this
 * user or users bring their own (per_user / both). Falls back to `/meta` before
 * `/ai/status` has loaded.
 */
export function aiUsable(status: AiStatus | undefined, metaEnabled: boolean): boolean {
  if (!status) return metaEnabled;
  if (status.enabled === false) return false;
  return status.configured || (status.llm_mode ?? "central") !== "central";
}

/** Per-user mode and this user has not configured a model yet. */
export function needsUserModel(status: AiStatus | undefined): boolean {
  return !!status && status.enabled !== false && status.llm_mode === "per_user" && !status.configured;
}

/** The model to send with a request: the user's pick when the server still offers it. */
export function requestModel(status: AiStatus | undefined): string | null {
  const picked = getAiModel();
  return picked && status?.models?.includes(picked) ? picked : null;
}

// ------------------------------------------------------------------ last results (reopen dialogs)

export type FindingsMode = "review" | "leaks" | "optimize" | "fix";

type AiResults = {
  reports: Partial<Record<FindingsMode, OptimizeReport | FindingsReport>>;
  setReport: (mode: FindingsMode, report: OptimizeReport | FindingsReport | null) => void;
};

/** Keeps the last report per feature so a dialog reopens with it after "reveal in editor". */
export const useAiResults = create<AiResults>()((set) => ({
  reports: {},
  setReport: (mode, report) => set((s) => ({ reports: { ...s.reports, [mode]: report ?? undefined } })),
}));

// ------------------------------------------------------------------ findings

export const SEVERITY_ORDER: Finding["severity"][] = ["critical", "high", "medium", "low", "info"];

export function severityPalette(severity: Finding["severity"]): string {
  switch (severity) {
    case "critical":
    case "high":
      return "red";
    case "medium":
      return "orange";
    case "low":
      return "yellow";
    default:
      return "gray";
  }
}

/** Findings the user can apply with one click (they carry a patch). */
export function patchable(findings: Finding[] | undefined): Finding[] {
  return (findings ?? []).filter((f) => (f.patch ?? []).length > 0);
}

/**
 * Selects the finding's node (or the DAG when it has none) and focuses the field it names,
 * using the inspector's `data-autoair-field` anchors. Panels render lazily, so focusing is
 * retried for a few frames.
 */
export function revealFinding(f: Pick<Finding, "node_id" | "field">): void {
  const st = useBuilder.getState();
  const nodeId = f.node_id && st.nodes.some((n) => n.id === f.node_id) ? f.node_id : null;
  if (nodeId) focusNode(nodeId);
  else st.select(null);
  // The inspector (task or DAG settings) opens the tab owning the field and focuses it.
  useFocusRequest.getState().focus({ node_id: nodeId, field: f.field ?? null, code: "ai" });
}

export function formatUsage(usage: Usage | null | undefined): string | null {
  if (!usage?.requests) return null;
  const tokens = (usage.input_tokens ?? 0) + (usage.output_tokens ?? 0);
  const cached = usage.cache_read_tokens ?? 0;
  const parts = [`${tokens.toLocaleString()} tokens`];
  if (cached) parts.push(`${cached.toLocaleString()} cached`);
  if (usage.cost_usd != null) parts.push(`$${usage.cost_usd.toFixed(4)}`);
  return parts.join(" · ");
}
