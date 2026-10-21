import { useShallow } from "zustand/react/shallow";

import type { Problem } from "../../api/gen/types.gen";
import type { NodeKind, SpecFor } from "../../lib/types";
import { problemText } from "../../lib/problemMessages";
import { enforceLocks } from "../../canvas/operatorTemplates";
import { useBuilder } from "../../store/builder";
import { useNodeTemplate } from "./TemplateBanner";

export type TaskTab = "parameters" | "execution" | "behaviour" | "data" | "docs";
export const TASK_TABS: TaskTab[] = ["parameters", "execution", "behaviour", "data", "docs"];

/**
 * Typed spec patcher for the node being edited. Fields locked by the node's operator
 * template are read-only: a patch never changes them.
 */
export function useUpdate<K extends NodeKind>(id: string) {
  const updateSpec = useBuilder((s) => s.updateSpec);
  const { locks } = useNodeTemplate(id);
  return (patch: Partial<SpecFor<K>>) => {
    if (!locks.size) return updateSpec<K>(id, patch);
    const current = useBuilder.getState().nodes.find((n) => n.id === id)?.data.spec ?? {};
    return updateSpec<K>(id, enforceLocks(patch, locks, current));
  };
}

export function useNodeProblems(id: string): Problem[] {
  return useBuilder(useShallow((s) => s.problems.filter((p) => p.node_id === id)));
}

/** First error message for a field of a node. */
export function useFieldError(id: string, field: string): string | undefined {
  return useBuilder((s) => {
    const p = s.problems.find((x) => x.node_id === id && x.field === field && x.severity === "error");
    return p ? problemText(p) : undefined;
  });
}

const EXECUTION_FIELDS = new Set([
  "trigger_rule",
  "retries",
  "retry_delay_seconds",
  "retry_exponential_backoff",
  "execution_timeout_seconds",
  "pool",
  "pool_slots",
  "queue",
  "owner",
  "priority_weight",
  "weight_rule",
  "executor",
  "executor_config",
  "max_active_tis_per_dagrun",
  "depends_on_past",
  "task_display_name",
]);
const BEHAVIOUR_FIELDS = new Set(["is_setup", "is_teardown", "callbacks", "retry_policy", "disabled"]);
const DATA_FIELDS = new Set(["mapping", "outlets", "inlets"]);
const BEHAVIOUR_CODES = new Set(["teardown", "callback", "retry_policy", "disabled"]);
const IDENTITY_FIELDS = new Set(["task_id", "label", "parent_group"]);

/** Inspector tab owning a problem's field (null: header fields shown above the tabs). */
export function tabForProblem(p: Pick<Problem, "field" | "code">): TaskTab | null {
  const root = (p.field ?? "").split(/[.[]/)[0] ?? "";
  if (IDENTITY_FIELDS.has(root)) return null;
  if (EXECUTION_FIELDS.has(root)) return "execution";
  if (BEHAVIOUR_FIELDS.has(root) || BEHAVIOUR_CODES.has(p.code)) return "behaviour";
  if (DATA_FIELDS.has(root) || p.code === "mapping") return "data";
  if (root === "doc_md") return "docs";
  return "parameters";
}
