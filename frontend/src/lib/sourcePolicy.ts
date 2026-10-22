import type { DagSpec } from "../api/gen/types.gen";

/** Source policies of a DAG file (DagSpec.source.policy). */
export const SOURCE_POLICIES = ["auto", "generate", "preserve", "strict-preserve"] as const;
export type SourcePolicyChoice = (typeof SOURCE_POLICIES)[number];

/** The policy of a document, "auto" for anything missing or unknown (e.g. a newer server). */
export function policyOf(dag: { source?: { policy?: unknown } | null } | null | undefined): SourcePolicyChoice {
  const value = dag?.source?.policy;
  return (SOURCE_POLICIES as readonly unknown[]).includes(value) ? (value as SourcePolicyChoice) : "auto";
}

/** True when the DAG comes from hand-written Python (imported or applied in the code mode). */
export function isHandWritten(dag: { source?: { origin?: unknown } | null } | null | undefined): boolean {
  return dag?.source?.origin === "python";
}

/** The source policy applies to the Python DAG files of visual DAGs only. */
export function policyApplies(dag: { output_mode?: string | null; raw_code?: string | null }): boolean {
  return (dag.output_mode ?? "python") === "python" && dag.raw_code == null;
}

/**
 * ``dag.source`` with another policy. Origin and hand-written code are kept unless
 * ``dropCode`` is set: saving a document that carries ``source.code`` needs ``code:raw``, so a
 * user without it who deliberately switches to generated code leaves the kept file behind.
 */
export function withPolicy(
  dag: { source?: DagSpec["source"] },
  policy: SourcePolicyChoice,
  { dropCode = false }: { dropCode?: boolean } = {},
): DagSpec["source"] {
  const current = dag.source && typeof dag.source === "object" ? dag.source : {};
  if (!dropCode) return { ...current, policy };
  const { code: _code, base_sha: _baseSha, ...rest } = current as Record<string, unknown>;
  return { ...rest, policy } as DagSpec["source"];
}
