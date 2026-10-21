import { type ReactFlowState, useStore } from "@xyflow/react";

import { LOW_DETAIL_ZOOM } from "./largeGraph";

const isLowDetail = (s: ReactFlowState) => s.transform[2] < LOW_DETAIL_ZOOM;

/**
 * True while the canvas is zoomed out below `LOW_DETAIL_ZOOM` (only reachable on large
 * graphs, see `LARGE_GRAPH_MIN_ZOOM`). A boolean selector: views re-render when the zoom
 * crosses the threshold, not on every pan or zoom frame.
 */
export function useLowDetail(): boolean {
  return useStore(isLowDetail);
}
