/** Pure helpers of the knowledge-base admin tab (unit-tested; never throw on odd data). */

const KB_VERSION_RE = /^[\w.+-]{1,40}$/;

/** "installed, 3.1.0  stable" → ["installed", "3.1.0", "stable"]; invalid entries reported. */
export function parseVersions(text: string): { versions: string[]; invalid: string[] } {
  const parts = (typeof text === "string" ? text : "")
    .split(/[\s,]+/)
    .map((p) => p.trim())
    .filter(Boolean);
  const versions: string[] = [];
  const invalid: string[] = [];
  for (const p of parts) {
    if (!KB_VERSION_RE.test(p)) invalid.push(p);
    else if (!versions.includes(p)) versions.push(p);
  }
  return { versions: versions.slice(0, 20), invalid };
}

/** Percentage of a running refresh's current unit (null when the total is unknown). */
export function progressPercent(done: unknown, total: unknown): number | null {
  if (typeof done !== "number" || typeof total !== "number" || !Number.isFinite(total) || total <= 0) return null;
  return Math.max(0, Math.min(100, Math.round((done / total) * 100)));
}

/** Rows of "pages per component" sorted by count, with their versions. */
export function componentRows(
  byComponent: Record<string, number> | null | undefined,
  versions: Record<string, string[]> | null | undefined,
): { component: string; pages: number; versions: string[] }[] {
  const counts = byComponent && typeof byComponent === "object" ? byComponent : {};
  const vers = versions && typeof versions === "object" ? versions : {};
  return Object.entries(counts)
    .filter(([, n]) => typeof n === "number" && Number.isFinite(n))
    .map(([component, pages]) => ({
      component,
      pages,
      versions: Array.isArray(vers[component]) ? vers[component].map(String) : [],
    }))
    .sort((a, b) => b.pages - a.pages || a.component.localeCompare(b.component));
}

/** Chakra colour palette of a status message level. */
export function levelPalette(level: string | null | undefined): "red" | "orange" | "blue" {
  if (level === "warning") return "orange";
  if (level === "info") return "blue";
  return "red";
}

/** Palette of a run outcome / source state. */
export function outcomePalette(value: string | null | undefined): "green" | "orange" | "red" | "gray" {
  if (value === "ok") return "green";
  if (value === "partial" || value === "cancelled" || value === "skipped") return "orange";
  if (value === "failed") return "red";
  return "gray";
}

/** Toggle `id` in a list of components (keeps order, no duplicates). */
export function toggleComponent(current: readonly string[] | null | undefined, id: string): string[] {
  const list = Array.isArray(current) ? current.filter((c) => typeof c === "string") : [];
  return list.includes(id) ? list.filter((c) => c !== id) : [...list, id];
}

/** A link only for http(s) URLs (citations from other schemes are shown as text). */
export function safeHref(url: string | null | undefined): string | null {
  return typeof url === "string" && /^https?:\/\//i.test(url) ? url : null;
}
