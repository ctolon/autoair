import { useQuery } from "@tanstack/react-query";
import { useEffect } from "react";

import { getRunOverlayOptions } from "../api/gen/@tanstack/react-query.gen";
import { useBuilder } from "../store/builder";
import { buildRunOverlay, isRunActive } from "./lib/runOverlay";
import { useLifecycle } from "./store";

const POLL_MS = 3000;

/** The overlay of the selected run (polled every 3 s while the run is active). */
export function useSelectedRunOverlay() {
  const dagId = useBuilder((s) => s.savedDagId);
  const runId = useLifecycle((s) => s.selectedRunId);
  return useQuery({
    ...getRunOverlayOptions({ path: { dag_id: dagId ?? "_", run_id: runId ?? "_" } }),
    enabled: !!dagId && !!runId,
    retry: false,
    refetchInterval: (q) => (isRunActive(q.state.data) ? POLL_MS : false),
  });
}

/** Paints the selected run's task states on the canvas (mounted once by the host). */
export function useRunOverlaySync(): void {
  const dagId = useBuilder((s) => s.savedDagId);
  const runId = useLifecycle((s) => s.selectedRunId);
  const nodes = useBuilder((s) => s.nodes);
  const dag = useBuilder((s) => s.dag);
  const overlay = useSelectedRunOverlay();
  useEffect(() => {
    const st = useBuilder.getState();
    if (!dagId || !runId) {
      if (st.runOverlay) st.setRunOverlay(null);
      return;
    }
    if (!overlay.data) return;
    st.setRunOverlay(buildRunOverlay(st.toSpec().nodes ?? [], dagId, runId, overlay.data));
    // Re-map when the graph changes (task ids / groups renamed).
  }, [dagId, runId, overlay.data, nodes, dag]);
}
