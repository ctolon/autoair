import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";

import { getDagStatusOptions, getMetaOptions } from "../api/gen/@tanstack/react-query.gen";
import type { MetaInfo } from "../api/gen/types.gen";
import { useBuilder } from "../store/builder";

export function useMeta(): MetaInfo | undefined {
  return useQuery({ ...getMetaOptions(), staleTime: 5 * 60_000 }).data;
}

/** Deployment environments: the server's plus the ones the spec defines overlays for. */
export function useEnvironments(): string[] {
  const meta = useMeta();
  const specEnvs = useBuilder((s) => s.dag.environments);
  return useMemo(
    () => [...new Set([...(meta?.environments ?? []), ...Object.keys(specEnvs ?? {})])],
    [meta?.environments, specEnvs],
  );
}

/**
 * Whether Airflow has parsed the DAG (a saved DAG may still be waiting for the next bundle
 * refresh / git sync): Airflow-side queries (runs, versions) wait for it instead of 404ing.
 */
export function useDagInAirflow(dagId: string | null | undefined): boolean {
  const status = useQuery({
    ...getDagStatusOptions({ path: { dag_id: dagId ?? "_" } }),
    enabled: !!dagId,
    retry: false,
    refetchInterval: (q) => (q.state.data?.exists ? 30_000 : 5_000),
  });
  return !!dagId && !!status.data?.exists;
}
