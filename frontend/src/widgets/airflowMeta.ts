import { useQuery } from "@tanstack/react-query";

import { getMetaOptions } from "../api/gen/@tanstack/react-query.gen";
import type { MetaInfo } from "../api/gen/types.gen";

/** Parses "3.1.2", "3.2.0rc1", "3.3.0.dev0" → [3, 1, 2]. */
function parseVersion(v: string | null | undefined): number[] | null {
  if (!v) return null;
  const m = /^(\d+)(?:\.(\d+))?(?:\.(\d+))?/.exec(v.trim());
  if (!m) return null;
  return [Number(m[1]), Number(m[2] ?? 0), Number(m[3] ?? 0)];
}

/** `version >= min`; unknown versions are treated as satisfying the requirement. */
function versionAtLeast(version: string | null | undefined, min: string): boolean {
  const a = parseVersion(version);
  const b = parseVersion(min);
  if (!a || !b) return true;
  for (let i = 0; i < 3; i++) {
    const x = a[i] ?? 0;
    const y = b[i] ?? 0;
    if (x !== y) return x > y;
  }
  return true;
}

/** The Airflow version code is generated for: the target version wins over the connected one. */
const effectiveAirflowVersion = (meta: MetaInfo | undefined): string | null =>
  meta?.target_airflow_version ?? meta?.airflow_version ?? null;

type AirflowGate = {
  meta: MetaInfo | undefined;
  version: string | null;
  /** True when the (target) Airflow version supports a feature introduced in `min`. */
  supports: (min: string) => boolean;
};

export function useAirflowGate(): AirflowGate {
  const meta = useQuery({ ...getMetaOptions(), retry: false, staleTime: 300_000 });
  const version = effectiveAirflowVersion(meta.data);
  return { meta: meta.data, version, supports: (min) => versionAtLeast(version, min) };
}
