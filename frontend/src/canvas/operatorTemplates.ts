import type { OperatorBlueprint, Template } from "../api/gen/types.gen";
import type { BuilderNode, TaskSpec } from "../lib/types";
import { nodeKey } from "../lib/types";
import { freshKey } from "./fragment";

/**
 * Operator templates ("blueprints"): one configured task saved for reuse. Each configurable
 * field is a *default* (editable copy), *locked* (read-only; the server flags changes of
 * tasks carrying the template's `template_ref`) or *required* (asked when inserting).
 *
 * Field paths: a task field (`retries`, `pool`) or one entry of a dict field
 * (`params.bash_command`, `arguments.x`, `config.y`) — the same paths the backend checks.
 */
export type FieldMode = "default" | "locked" | "required";
type BlueprintNode = OperatorBlueprint["node"];

/** Dict-valued task fields whose entries are configured one by one. */
const DICT_FIELDS = ["params", "arguments", "decorator_kwargs", "config"] as const;

/** Task-level settings a template can carry (execution settings and docs). */
export const TASK_FIELDS = [
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
  "max_active_tis_per_dagrun",
  "depends_on_past",
  "doc_md",
  "code",
] as const;

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);

export const isEmptyValue = (v: unknown): boolean =>
  v === undefined ||
  v === null ||
  v === "" ||
  (Array.isArray(v) && v.length === 0) ||
  (isRecord(v) && Object.keys(v).length === 0);

/** Value at `path` of a task spec (undefined when absent). */
export function getPath(spec: object, path: string): unknown {
  const [head, key] = path.split(".", 2) as [string, string | undefined];
  const value = (spec as Record<string, unknown>)[head];
  if (key === undefined) return value;
  return isRecord(value) ? value[key] : undefined;
}

/** Copy of `spec` with `path` set (an empty value removes a dict entry). */
export function setPath<T extends object>(spec: T, path: string, value: unknown): T {
  const [head, key] = path.split(".", 2) as [string, string | undefined];
  const rec = spec as Record<string, unknown>;
  if (key === undefined) return { ...spec, [head]: value };
  const entries = Object.entries(isRecord(rec[head]) ? rec[head] : {}).filter(([k]) => k !== key);
  if (value !== undefined) entries.push([key, value]);
  return { ...spec, [head]: Object.fromEntries(entries) };
}

/** Fields of a task that can be saved as a default / locked / required input (set ones first). */
export function configurablePaths(spec: TaskSpec): string[] {
  const out: string[] = [];
  const rec = spec as unknown as Record<string, unknown>;
  for (const field of DICT_FIELDS) {
    const dict = rec[field];
    if (isRecord(dict)) for (const key of Object.keys(dict)) out.push(`${field}.${key}`);
  }
  for (const field of TASK_FIELDS) {
    if (field === "code" && spec.kind !== "python_task") continue;
    if (!isEmptyValue(rec[field])) out.push(field);
  }
  return out;
}

/** The node stored in a blueprint: per-task identity, placement and template link removed. */
function blueprintNode(spec: TaskSpec): BlueprintNode {
  const { template_ref: _ref, ...rest } = spec as TaskSpec & { template_ref?: unknown };
  void _ref;
  return { ...rest, id: "template", parent_group: null, position: { x: 0, y: 0 } } as BlueprintNode;
}

/** Builds the blueprint from a task and the chosen mode of each field. */
export function makeBlueprint(spec: TaskSpec, modes: Readonly<Record<string, FieldMode>>): OperatorBlueprint {
  const locked: string[] = [];
  const required: string[] = [];
  let node = blueprintNode(spec);
  for (const [path, mode] of Object.entries(modes)) {
    if (mode === "locked") locked.push(path);
    else if (mode === "required") {
      required.push(path);
      // The inserter provides it: keep the current value out of the template.
      node = setPath(node, path, path.includes(".") ? undefined : path === "code" ? "" : null);
    }
  }
  return { node, locked, required };
}

export const lockedPaths = (tpl: Pick<Template, "blueprint">): string[] => tpl.blueprint?.locked ?? [];
export const requiredPaths = (tpl: Pick<Template, "blueprint">): string[] => tpl.blueprint?.required ?? [];

/**
 * A new task spec from an operator template: fresh task_id (and function name), the
 * inserter's `inputs` for the required fields, and `template_ref` linking it to the template.
 */
export function instantiateBlueprint(
  tpl: Pick<Template, "id" | "version" | "icon" | "blueprint">,
  existing: readonly BuilderNode[],
  inputs: Readonly<Record<string, unknown>> = {},
): TaskSpec {
  if (!tpl.blueprint) throw new Error(`template ${tpl.id} is not an operator template`);
  const raw = structuredClone(tpl.blueprint.node) as Record<string, unknown>;
  delete raw.id;
  delete raw.position;
  delete raw.parent_group;
  let spec = raw as unknown as TaskSpec;
  for (const path of requiredPaths(tpl)) {
    if (path in inputs) spec = setPath(spec, path, inputs[path]);
  }
  const used = new Set(existing.map(nodeKey));
  const taskId = freshKey(used, spec.task_id || tpl.id.replace(/-/g, "_"));
  spec = { ...spec, task_id: taskId };
  if (spec.kind === "python_task") {
    const fns = new Set<string>();
    for (const n of existing) if (n.data.spec.kind === "python_task") fns.add(n.data.spec.function_name);
    spec = {
      ...spec,
      function_name: freshKey(fns, spec.function_name === tpl.blueprint.node.task_id ? taskId : spec.function_name),
    };
  }
  return {
    ...spec,
    icon: spec.icon ?? tpl.icon ?? null,
    template_ref: { id: tpl.id, version: tpl.version ?? null },
  };
}

/**
 * Re-applies locked values to an inspector patch (locked fields are read-only: an edit of a
 * whole dict such as `params` keeps its locked entries).
 */
export function enforceLocks<T extends object>(
  patch: Partial<T>,
  locks: ReadonlyMap<string, unknown>,
  current: object,
): Partial<T> {
  if (!locks.size) return patch;
  let out: Record<string, unknown> = { ...patch };
  for (const [path, value] of locks) {
    const head = path.split(".", 1)[0] as string;
    if (!(head in out)) continue;
    if (path.includes(".")) {
      const base = { ...current, ...out };
      out = { ...out, [head]: (setPath(base, path, value) as Record<string, unknown>)[head] };
    } else out[head] = value;
  }
  return out as Partial<T>;
}

/** Locked path → template value (what `enforceLocks` and the inspector use). */
export function lockMap(tpl: Pick<Template, "blueprint"> | undefined | null): Map<string, unknown> {
  const out = new Map<string, unknown>();
  if (!tpl?.blueprint) return out;
  for (const path of tpl.blueprint.locked ?? []) out.set(path, getPath(tpl.blueprint.node, path));
  return out;
}
