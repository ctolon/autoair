import { useQuery } from "@tanstack/react-query";

import { getAirflowCapabilitiesOptions } from "../api/gen/@tanstack/react-query.gen";
import type { AirflowCapabilities } from "../api/gen/types.gen";

/** Nothing optional: what the UI assumes until (or unless) the server answers. */
export const NO_CAPABILITIES: AirflowCapabilities = {
  version: null,
  source: "unknown",
  materialize: false,
  clear_run_on_latest_version: false,
  clear_only_new: false,
  keep_task_state: false,
  backfill_run_on_latest_version: false,
  scheduling_state: false,
  retry_try_is_next: false,
};

/**
 * Version-dependent run operations of the connected Airflow (`GET /airflow/capabilities`).
 * Options a version lacks are hidden: Airflow's strict request bodies reject unknown fields.
 */
export function useAirflowCapabilities(): AirflowCapabilities {
  const q = useQuery({ ...getAirflowCapabilitiesOptions(), retry: false, staleTime: 300_000 });
  return q.data ?? NO_CAPABILITIES;
}
