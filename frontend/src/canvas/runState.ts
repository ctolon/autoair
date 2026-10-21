/** Visual language of Airflow task-instance states on the canvas. */
type RunStateStyle = { color: string; bg: string; palette: string; pulse?: boolean };

/**
 * Every Airflow 3 task-instance state, plus "none" (no state yet). The legend shows one row per
 * entry so each chip colour on the canvas can be looked up there.
 */
const RUN_STATES = [
  "success",
  "running",
  "restarting",
  "failed",
  "upstream_failed",
  "up_for_retry",
  "up_for_reschedule",
  "deferred",
  "awaiting_input",
  "queued",
  "scheduled",
  "skipped",
  "removed",
  "none",
] as const;

type RunState = (typeof RUN_STATES)[number];

const STYLES: Record<RunState, RunStateStyle> = {
  success: { color: "green.500", bg: "green.subtle", palette: "green" },
  failed: { color: "red.500", bg: "red.subtle", palette: "red" },
  running: { color: "blue.500", bg: "blue.subtle", palette: "blue", pulse: true },
  restarting: { color: "blue.300", bg: "blue.subtle", palette: "blue", pulse: true },
  queued: { color: "gray.500", bg: "gray.subtle", palette: "gray" },
  scheduled: { color: "teal.400", bg: "teal.subtle", palette: "teal" },
  skipped: { color: "pink.400", bg: "pink.subtle", palette: "pink" },
  up_for_retry: { color: "yellow.500", bg: "yellow.subtle", palette: "yellow" },
  up_for_reschedule: { color: "cyan.500", bg: "cyan.subtle", palette: "cyan" },
  upstream_failed: { color: "orange.500", bg: "orange.subtle", palette: "orange" },
  deferred: { color: "purple.500", bg: "purple.subtle", palette: "purple" },
  awaiting_input: { color: "purple.300", bg: "purple.subtle", palette: "purple", pulse: true },
  removed: { color: "gray.300", bg: "gray.subtle", palette: "gray" },
  none: { color: "gray.300", bg: "bg.panel", palette: "gray" },
};

const ALIASES: Record<string, RunState> = {
  // Older Airflow spellings / API variants.
  shutdown: "restarting",
  sensing: "running",
  no_status: "none",
  null: "none",
  "": "none",
};

/** Canonical legend key of an API task-instance state (unknown states fall back to "none"). */
export function normalizeRunState(state: string | null | undefined): RunState {
  if (state == null) return "none";
  const s = state.toLowerCase().trim();
  if ((RUN_STATES as readonly string[]).includes(s)) return s as RunState;
  return ALIASES[s] ?? "none";
}

export function runStateStyle(state: string | null | undefined): RunStateStyle {
  return STYLES[normalizeRunState(state)];
}

/** Minimap colors (plain CSS: the minimap is an SVG outside Chakra's style props). */
const MINIMAP: Record<RunState, { light: string; dark: string }> = {
  success: { light: "#22c55e", dark: "#15803d" },
  failed: { light: "#ef4444", dark: "#b91c1c" },
  running: { light: "#3b82f6", dark: "#1d4ed8" },
  restarting: { light: "#93c5fd", dark: "#3b82f6" },
  queued: { light: "#6b7280", dark: "#4b5563" },
  scheduled: { light: "#2dd4bf", dark: "#0f766e" },
  skipped: { light: "#f472b6", dark: "#be185d" },
  up_for_retry: { light: "#eab308", dark: "#a16207" },
  up_for_reschedule: { light: "#06b6d4", dark: "#0e7490" },
  upstream_failed: { light: "#f97316", dark: "#c2410c" },
  deferred: { light: "#a855f7", dark: "#7e22ce" },
  awaiting_input: { light: "#d8b4fe", dark: "#9333ea" },
  removed: { light: "#d1d5db", dark: "#374151" },
  none: { light: "#d1d5db", dark: "#374151" },
};

export function runStateHex(state: string | null | undefined, dark = false): string | undefined {
  if (state === undefined) return undefined;
  const c = MINIMAP[normalizeRunState(state)];
  return dark ? c.dark : c.light;
}

/** States shown in the legend, in display order (all of them: every chip colour is explained). */
export const LEGEND_STATES = RUN_STATES;

// Worst-first: the aggregated state of a collapsed group / mapped task is the first one present.
const PRIORITY: readonly RunState[] = [
  "failed",
  "upstream_failed",
  "up_for_retry",
  "restarting",
  "running",
  "awaiting_input",
  "deferred",
  "up_for_reschedule",
  "queued",
  "scheduled",
];

/** Single state summarising several task instances (a collapsed group, a mapped task). */
export function aggregateState(states: readonly (string | null | undefined)[]): string | null {
  const present = states.filter((s): s is string => !!s).map(normalizeRunState);
  if (!present.length) return null;
  if (present.length === 1) return present[0] ?? null;
  for (const p of PRIORITY) if (present.includes(p)) return p;
  if (present.every((s) => s === "skipped")) return "skipped";
  if (present.every((s) => s === "success" || s === "skipped")) return "success";
  if (present.every((s) => s === "removed")) return "removed";
  // Mixed finished + not-yet-started instances: some are still to run.
  return present.includes("none") ? "none" : (present[0] ?? null);
}

const RUN_TYPES = ["manual", "scheduled", "backfill", "dataset_triggered", "asset_triggered", "asset_materialization"];

/**
 * Short label of an Airflow run id: its type and local start time
 * ("manual__2026-10-15T13:43:34.497+00:00" → { type: "manual", when: "15 Oct, 16:43" }).
 * Custom run ids come back unchanged (`when` null).
 */
export function shortRunLabel(
  runId: string,
  locale?: string,
  timeZone?: string,
): { type: string | null; when: string | null; raw: string } {
  const m = /^([a-z_]+)__(.+)$/.exec(runId);
  if (!m || !RUN_TYPES.includes(m[1] ?? "")) return { type: null, when: null, raw: runId };
  const date = new Date(m[2] ?? "");
  if (Number.isNaN(date.getTime())) return { type: m[1] ?? null, when: null, raw: runId };
  const when = new Intl.DateTimeFormat(locale, {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
    ...(timeZone ? { timeZone } : {}),
  }).format(date);
  return { type: m[1] ?? null, when, raw: runId };
}
