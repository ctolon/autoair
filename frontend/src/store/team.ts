import { useBuilder } from "./builder";

/**
 * Query options for team-scoped Airflow pickers (multi-team, Airflow 3.2+): when the DAG
 * belongs to a team, only global objects and the team's own are offered.
 */
export function useTeamQuery(): { query: { team: string } } | undefined {
  const team = useBuilder((s) => s.dag.team);
  return team ? { query: { team } } : undefined;
}
