/** Formatting helpers of the admin panel (pure: unit-tested). */

export function formatNumber(value: number | null | undefined, lng = "en"): string {
  if (value === null || value === undefined) return "—";
  return new Intl.NumberFormat(lng).format(value);
}

export function formatCost(value: number | null | undefined, lng = "en"): string {
  if (value === null || value === undefined) return "—";
  return new Intl.NumberFormat(lng, {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: value < 1 ? 4 : 2,
  }).format(value);
}

/** ISO timestamp → local date-time (invalid input is shown as is, never throws). */
export function formatTime(value: string | null | undefined, lng = "en"): string {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat(lng, { dateStyle: "medium", timeStyle: "short" }).format(date);
}

/** Seconds since a UNIX time, rounded for humans ("5 s", "3 min", "2 h"). */
export function ago(unixSeconds: number, now = Date.now() / 1000): string {
  const delta = Math.max(0, Math.round(now - unixSeconds));
  if (delta < 60) return `${delta} s`;
  if (delta < 3600) return `${Math.round(delta / 60)} min`;
  if (delta < 86400) return `${Math.round(delta / 3600)} h`;
  return `${Math.round(delta / 86400)} d`;
}

/** Totals of AI usage rows. */
export function usageTotals(
  rows: readonly { requests?: number; input_tokens?: number; output_tokens?: number; cost_usd?: number | null }[],
) {
  let requests = 0;
  let input = 0;
  let output = 0;
  let cost: number | null = null;
  for (const r of rows) {
    requests += r.requests ?? 0;
    input += r.input_tokens ?? 0;
    output += r.output_tokens ?? 0;
    if (r.cost_usd !== null && r.cost_usd !== undefined) cost = (cost ?? 0) + r.cost_usd;
  }
  return { requests, input, output, cost };
}

/** `2026-10-15` (a date input) → the API's ISO lower/upper bound, or undefined. */
export function dayBound(day: string, end = false): string | undefined {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) return undefined;
  if (!end) return day;
  const next = new Date(`${day}T00:00:00Z`);
  next.setUTCDate(next.getUTCDate() + 1);
  return next.toISOString().slice(0, 10);
}
