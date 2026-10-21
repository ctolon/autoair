import { create } from "zustand";

import type { XYPosition } from "@xyflow/react";

import type { ConflictInfo, DagSpec, SourcePlan, Template } from "../api/gen/types.gen";
import { specOnlyBlocked } from "../codemode/store";

/** Request parameters of a deploy (kept to retry it after a conflict). */
export type DeployOptions = {
  message?: string | null;
  environment?: string | null;
  force?: boolean;
  baseEtag?: string | null;
  spec?: DagSpec;
  /** Git: push straight to the branch even in PR mode (needs deploy:direct_push). */
  directPush?: boolean | null;
  /** Git PR mode: the pull request description (the context block is appended by the server). */
  prDescription?: string | null;
  /** Per-user git credentials: the user's Airflow connection to commit with. */
  gitConnId?: string | null;
  /**
   * Answer to a deploy that stopped at a DAG file changed outside AutoAir: apply the change
   * to that file (preserve) or regenerate it (overwrite, needs deploy:overwrite_ejected).
   */
  sourceResolution?: "preserve" | "overwrite" | null;
  /** The previewed DAG file (`target_sha`): the deploy stops when it changed since. */
  sourceTargetSha?: string | null;
};

export type ImportSource = "python" | "airflow" | "yaml" | "spec";

/** Which lifecycle dialog is open (at most one at a time). */
export type LifecycleDialog =
  | { kind: "open" }
  | { kind: "deploy"; focus: "message" | "environment" }
  | { kind: "conflict"; conflict: ConflictInfo; request: DeployOptions; spec: DagSpec }
  /** A deploy stopped by the source policy: an ejected DAG file or a refused patch. */
  | { kind: "source"; plan: SourcePlan; request: DeployOptions; spec: DagSpec }
  | { kind: "trigger" }
  | { kind: "backfill" }
  | { kind: "clear"; runId: string }
  | { kind: "testTask"; nodeId: string | null; whole?: boolean }
  | { kind: "templates"; filter?: "dag" | "fragment" | "operator" }
  | { kind: "saveTemplate"; mode: "dag" | "fragment" }
  | { kind: "saveOperatorTemplate"; nodeId: string }
  | { kind: "operatorTemplateInputs"; template: Template; at?: XYPosition | undefined }
  /** Standalone operator template editor (no canvas needed): new, next version of, copy of. */
  | { kind: "operatorTemplateEditor"; mode: "new" | "edit" | "duplicate"; templateId?: string | undefined }
  | { kind: "aiGenerate" }
  | { kind: "aiEdit" }
  | { kind: "aiExplain" }
  | { kind: "aiExplainDag" }
  | { kind: "aiReview" }
  | { kind: "aiLeaks" }
  | { kind: "aiOptimize" }
  | { kind: "aiFix" }
  | { kind: "aiTaskCode"; nodeId: string }
  | { kind: "aiAsk" }
  | { kind: "aiWriter" }
  | { kind: "aiModel" }
  | { kind: "aiKnowledge" }
  | { kind: "import"; source: ImportSource; dagId?: string }
  | { kind: "bulkImport" }
  | { kind: "promote" }
  | { kind: "deleteDag"; dagId: string }
  | { kind: "storage" }
  | { kind: "duplicate" }
  | { kind: "config" };

export type BottomTab = "problems" | "code" | "runs" | "task" | "versions" | "airflow" | "audit";

type LifecycleState = {
  dialog: LifecycleDialog | null;
  bottomTab: BottomTab;
  /** Run selected in the Runs tab (drives the canvas overlay and the Task tab). */
  selectedRunId: string | null;
  /** The app was opened for a DAG that AutoAir does not manage (offer an import). */
  unmanagedDagId: string | null;
  openDialog: (d: LifecycleDialog) => void;
  closeDialog: () => void;
  setBottomTab: (tab: BottomTab) => void;
  setSelectedRunId: (id: string | null) => void;
  setUnmanagedDagId: (id: string | null) => void;
};

export const useLifecycle = create<LifecycleState>()((set) => ({
  dialog: null,
  bottomTab: "problems",
  selectedRunId: null,
  unmanagedDagId: null,
  // Spec-only dialogs on a raw code DAG would act on an empty spec: refused at the source.
  openDialog: (dialog) => {
    if (!specOnlyBlocked(dialog.kind)) set({ dialog });
  },
  closeDialog: () => set({ dialog: null }),
  setBottomTab: (bottomTab) => set({ bottomTab }),
  setSelectedRunId: (selectedRunId) => set({ selectedRunId }),
  setUnmanagedDagId: (unmanagedDagId) => set({ unmanagedDagId }),
}));
