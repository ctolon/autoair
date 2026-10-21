import type { DagSpec, TargetStatus } from "../../api/gen/types.gen";
import { canonicalJson } from "../../lib/canonical";

type DeployEffect =
  | { kind: "push"; branch: string; path: string | null }
  | { kind: "pr"; branch: string }
  | { kind: "write"; location: string };

type Rights = { git_mode?: string | null; default_opens_pr?: boolean } | null | undefined;

/** What a deploy will concretely do with this target (for the deploy dialog). */
export function deployEffect(storage: TargetStatus, rights: Rights, directPush = false): DeployEffect {
  if (storage.kind !== "git") return { kind: "write", location: storage.location };
  const details = storage.details ?? {};
  const branch = typeof details.base_branch === "string" && details.base_branch ? details.base_branch : "main";
  const subdir = typeof details.subdir === "string" && details.subdir ? details.subdir : null;
  const opensPr = !directPush && (rights?.default_opens_pr ?? (rights?.git_mode ?? details.mode) === "pr");
  return opensPr ? { kind: "pr", branch } : { kind: "push", branch, path: subdir };
}

type SpecNode = NonNullable<DagSpec["nodes"]>[number];
const nodeName = (n: SpecNode): string | null =>
  n.kind === "task_group" ? n.group_id : n.kind === "note" ? null : n.kind === "blueprint" ? n.step_id : n.task_id;

/**
 * Comparable form of a spec part: key order and `null` vs. missing are not changes (a spec
 * rebuilt by the editor orders keys differently from the one the server returned).
 */
const key = (value: unknown) => canonicalJson(value, { nullAsMissing: true });

/**
 * A commit message describing the change against the deployed version, e.g.
 * "Update sales_etl: add load, remove tmp, change extract". New DAGs get "Add <dag_id>".
 */
export function suggestCommitMessage(spec: DagSpec, base: DagSpec | null, maxNames = 3): string {
  if (!base) return `Add DAG ${spec.dag_id}`;
  const before = new Map<string, string>();
  for (const n of base.nodes ?? []) {
    const k = nodeName(n);
    if (k) before.set(k, key({ ...n, position: undefined }));
  }
  const added: string[] = [];
  const changed: string[] = [];
  const seen = new Set<string>();
  for (const n of spec.nodes ?? []) {
    const k = nodeName(n);
    if (!k) continue;
    seen.add(k);
    const old = before.get(k);
    if (old === undefined) added.push(k);
    else if (old !== key({ ...n, position: undefined })) changed.push(k);
  }
  const removed = [...before.keys()].filter((k) => !seen.has(k));
  const list = (verb: string, names: string[]) =>
    names.length
      ? `${verb} ${names.slice(0, maxNames).join(", ")}${names.length > maxNames ? ` (+${names.length - maxNames})` : ""}`
      : null;
  const parts = [list("add", added), list("remove", removed), list("change", changed)].filter(Boolean);
  const strip = ({ nodes: _n, edges: _e, ...rest }: DagSpec) => key(rest);
  if (strip(spec) !== strip(base)) parts.push("update DAG settings");
  const edgesKey = (s: DagSpec) =>
    (s.edges ?? [])
      .map((e) => `${e.source}>${e.target}`)
      .sort()
      .join();
  if (!parts.length && edgesKey(spec) !== edgesKey(base)) parts.push("update dependencies");
  return parts.length ? `Update ${spec.dag_id}: ${parts.join(", ")}` : `Update ${spec.dag_id}`;
}
