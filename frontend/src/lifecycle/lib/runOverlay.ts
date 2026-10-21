import type { DagSpec, RunsOverlay, TaskInstanceInfo } from "../../api/gen/types.gen";
import { aggregateState as aggregateStates } from "../../canvas/runState";
import type { RunOverlay } from "../../store/builder";

type SpecNode = NonNullable<DagSpec["nodes"]>[number];

const TASK_KINDS = new Set(["operator", "python_task", "custom"]);

/** Task/group id including parent group prefixes, as Airflow computes it (mirrors the backend). */
function effectiveId(node: SpecNode, byId: Map<string, SpecNode>): string | null {
  let own: string;
  if (node.kind === "task_group") own = node.group_id;
  else if (node.kind === "note") return null;
  else if (node.kind === "blueprint") own = node.step_id;
  else own = node.task_id;
  const parts = [own];
  const seen = new Set<string>();
  let parentId = node.parent_group ?? null;
  while (parentId && !seen.has(parentId)) {
    seen.add(parentId);
    const parent = byId.get(parentId);
    if (!parent || parent.kind !== "task_group") break;
    if (parent.prefix_group_id !== false) parts.push(parent.group_id);
    parentId = parent.parent_group ?? null;
  }
  return parts.reverse().join(".");
}

/** Effective Airflow task id → builder node id, for every task node of the spec. */
export function effectiveTaskIds(nodes: readonly SpecNode[]): Map<string, string> {
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const out = new Map<string, string>();
  for (const n of nodes) {
    if (!TASK_KINDS.has(n.kind)) continue;
    const eff = effectiveId(n, byId);
    if (eff) out.set(eff, n.id);
  }
  return out;
}

/** Reverse of {@link effectiveTaskIds}: node id → effective Airflow task id. */
export function nodeTaskId(nodes: readonly SpecNode[], nodeId: string): string | null {
  for (const [eff, id] of effectiveTaskIds(nodes)) if (id === nodeId) return eff;
  return null;
}

const ACTIVE = new Set([
  "running",
  "queued",
  "scheduled",
  "deferred",
  "up_for_retry",
  "up_for_reschedule",
  "restarting",
  "awaiting_input",
]);

/** True while the run (or any of its task instances) is still progressing. */
/**
 * Newest try of a task instance that can have a log. From Airflow 3.4 (`retryTryIsNext`, see
 * `AirflowCapabilities.retry_try_is_next`) an `up_for_retry` instance already carries the
 * upcoming try; the failed one is `try_number - 1`.
 */
export function latestLogTry(
  ti: Pick<TaskInstanceInfo, "state" | "try_number"> | undefined,
  retryTryIsNext: boolean,
): number {
  const latest = Math.max(1, ti?.try_number ?? 1);
  return retryTryIsNext && ti?.state === "up_for_retry" ? Math.max(1, latest - 1) : latest;
}

export function isRunActive(overlay: RunsOverlay | undefined): boolean {
  if (!overlay) return false;
  const state = overlay.run?.state ?? null;
  if (state === "running" || state === "queued") return true;
  return overlay.task_instances.some((ti) => ACTIVE.has(ti.state ?? ""));
}

/**
 * Aggregated state of one (possibly mapped) task's instances: a single instance keeps its own
 * state (upstream_failed stays upstream_failed); several are summarised worst-first.
 */
export function aggregateState(instances: readonly TaskInstanceInfo[]): string | null {
  return aggregateStates(instances.map((ti) => ti.state ?? null));
}

/** Maps task instances of a run onto canvas node ids (mapped instances aggregated). */
export function buildRunOverlay(
  nodes: readonly SpecNode[],
  dagId: string,
  runId: string,
  overlay: RunsOverlay,
): RunOverlay {
  const ids = effectiveTaskIds(nodes);
  // A blueprint step's tasks are `<step>` or `<step>.<task>`: their states aggregate on the step.
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const steps = nodes
    .filter((n) => n.kind === "blueprint")
    .map((n) => [effectiveId(n, byId), n.id] as const)
    .filter((s): s is readonly [string, string] => s[0] !== null);
  const stepOf = (taskId: string) =>
    steps.find(([eff]) => taskId === eff || taskId.startsWith(`${eff}.`))?.[1] ?? undefined;
  const grouped = new Map<string, TaskInstanceInfo[]>();
  for (const ti of overlay.task_instances) {
    const nodeId = ids.get(ti.task_id) ?? stepOf(ti.task_id);
    if (!nodeId) continue;
    grouped.set(nodeId, [...(grouped.get(nodeId) ?? []), ti]);
  }
  const states: RunOverlay["states"] = {};
  for (const [nodeId, tis] of grouped) {
    const mapped = tis.filter((ti) => (ti.map_index ?? -1) >= 0).length;
    states[nodeId] = {
      state: aggregateState(tis),
      tryNumber: Math.max(0, ...tis.map((ti) => ti.try_number ?? 0)),
      ...(mapped ? { mapped } : {}),
    };
  }
  return { dagId, runId, states };
}
