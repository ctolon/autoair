import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useMemo } from "react";

import { previewSource } from "../api/gen/sdk.gen";
import type { DagSpec } from "../api/gen/types.gen";
import { errorMessage } from "../api/setup";
import { useDebounced } from "../hooks/useDebounced";
import { isHandWritten, policyApplies, policyOf } from "../lib/sourcePolicy";
import { useBuilder } from "../store/builder";
import { toSpec } from "../store/convert";

export type PreservedFile = {
  /** The DAG keeps its hand-written file: the code view shows that file, not generated code. */
  active: boolean;
  /** The file a deploy would write (null while loading or when the change cannot be applied). */
  code: string | null;
  loading: boolean;
  /** Why the file cannot be shown (a refused patch, a failed preview). */
  error: string | null;
};

const INACTIVE: PreservedFile = { active: false, code: null, loading: false, error: null };

/** True when deploys of ``dag`` patch its hand-written file (source policy preserve). */
export function keepsHandWrittenFile(dag: {
  output_mode?: string | null;
  raw_code?: string | null;
  source?: { policy?: unknown; origin?: unknown; code?: string | null } | null;
}): boolean {
  if (!policyApplies(dag)) return false;
  const policy = policyOf(dag);
  if (policy === "generate") return false;
  // The file lives in the deploy target or in source.code: the preview reads either.
  return policy !== "auto" || isHandWritten(dag);
}

/** The document without canvas positions: moving nodes does not change the file. */
function fileSpec(spec: DagSpec): DagSpec {
  return { ...spec, nodes: (spec.nodes ?? []).map((n) => ({ ...n, position: undefined })) as DagSpec["nodes"] };
}

/**
 * The DAG file of a document that keeps its hand-written file (source policy preserve): the
 * file with the current canvas changes patched in, as the server's source preview computes
 * it. Editing the generated code instead would replace the hand-written file on deploy.
 */
export function usePreservedFile(): PreservedFile {
  const dag = useBuilder((s) => s.dag);
  const nodes = useBuilder((s) => s.nodes);
  const edges = useBuilder((s) => s.edges);
  const active = keepsHandWrittenFile(dag);
  const live = useMemo(() => (active ? fileSpec(toSpec(dag, nodes, edges)) : null), [active, dag, nodes, edges]);
  // Each preview imports the file on the server: wait until the edits pause.
  const spec = useDebounced(live, 800);
  const query = useQuery({
    queryKey: ["codemode", "preservedFile", spec],
    enabled: spec !== null,
    staleTime: 60_000,
    retry: false,
    placeholderData: keepPreviousData,
    queryFn: async ({ signal }) => {
      if (!spec) return null;
      const { data } = await previewSource({
        path: { dag_id: spec.dag_id },
        body: { spec, environment: null },
        signal,
        throwOnError: true,
      });
      return data;
    },
  });
  if (!active) return INACTIVE;
  if (query.isPending) return { active, code: null, loading: true, error: null };
  if (query.isError) return { active, code: null, loading: false, error: errorMessage(query.error) };
  const plan = query.data;
  const code = typeof plan?.content === "string" ? plan.content : null;
  const ok = plan?.action === "preserve" || plan?.action === "unchanged";
  if (ok && code !== null) return { active, code, loading: false, error: null };
  if (plan?.action === "generate") return INACTIVE; // nothing hand-written to keep yet
  return { active, code: null, loading: false, error: plan?.message || null };
}
