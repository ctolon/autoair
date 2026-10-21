/**
 * Thresholds of the large-graph mode. Measured with `e2e/large-graph.spec.ts` (500 and 1000
 * tasks); the limits and what each switch does are documented on the website page
 * "Large DAGs" (guides/large-dags).
 */

/** Above this many nodes, only elements inside the viewport are rendered. */
export const VIRTUALIZE_ABOVE = 150;

/**
 * Above this many nodes the canvas may zoom out further (to see the whole graph); task
 * cards then switch to a light, low-detail look below `LOW_DETAIL_ZOOM`.
 */
export const LARGE_GRAPH_NODES = 150;

/** Smallest zoom of a large graph (React Flow's default, 0.5, is kept for small ones). */
export const LARGE_GRAPH_MIN_ZOOM = 0.05;

/**
 * Floor of the automatic fits (opening a DAG, after auto layout, panel resizes): a large
 * graph opens readable around its centre, as on small graphs, instead of as a tiny overview
 * that mounts every node. "Fit view" (button, command, menu) still shows the whole graph.
 */
export const AUTO_FIT_MIN_ZOOM = 0.5;

/**
 * Below this zoom, cards render as plain boxes with their title, and edges without their
 * insert button and label: text is unreadable there and the full cards cost the most.
 */
export const LOW_DETAIL_ZOOM = 0.4;

/** Documents above this many nodes wait longer before the live check (validate + preview). */
const LIVE_CHECK_SCALE_ABOVE = 200;
const LIVE_CHECK_MAX_DELAY_MS = 2_000;

/**
 * Debounce of the live check for a document of `nodes` nodes: `base` up to 200 nodes, then
 * one more millisecond per node, at most 2 s. Every check serialises and sends the whole
 * document and re-renders the problem badges, so typing in a large DAG should not trigger
 * one per pause between two keystrokes.
 */
export function liveCheckDelay(base: number, nodes: number): number {
  if (nodes <= LIVE_CHECK_SCALE_ABOVE) return base;
  return Math.min(Math.max(base, LIVE_CHECK_MAX_DELAY_MS), base + (nodes - LIVE_CHECK_SCALE_ABOVE));
}
