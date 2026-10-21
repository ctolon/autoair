import type { InsertItem } from "./insertables";

/**
 * Fast insertable search: each item's searchable text is lower-cased once (cached per item
 * object), queries are split into words that must all match, and results are ranked so the
 * best label matches come first. With every provider installed there are >1200 operators.
 */
const HAYSTACK = new WeakMap<InsertItem, { label: string; all: string }>();

function haystack(item: InsertItem) {
  let h = HAYSTACK.get(item);
  if (!h) {
    const label = item.label.toLowerCase();
    h = { label, all: [label, item.hint, item.provider ?? "", ...item.keywords].join("\n").toLowerCase() };
    HAYSTACK.set(item, h);
  }
  return h;
}

export const searchTerms = (query: string): string[] => query.trim().toLowerCase().split(/\s+/).filter(Boolean);

/** 0 = no match; higher = better (label prefix > label word > anywhere). */
export function matchScore(item: InsertItem, terms: string[]): number {
  if (!terms.length) return 1;
  const h = haystack(item);
  let score = 0;
  for (const term of terms) {
    if (!h.all.includes(term)) return 0;
    if (h.label.startsWith(term)) score += 4;
    else if (h.label.includes(term)) score += 2;
    else score += 1;
  }
  return score;
}

export const matchesQuery = (item: InsertItem, terms: string[]) => matchScore(item, terms) > 0;

/** Best `limit` matches, best first (stable for equal scores). */
export function rankItems(items: readonly InsertItem[], query: string, limit = Number.POSITIVE_INFINITY): InsertItem[] {
  const terms = searchTerms(query);
  if (!terms.length) return items.slice(0, limit);
  const scored: { item: InsertItem; score: number; i: number }[] = [];
  items.forEach((item, i) => {
    const score = matchScore(item, terms);
    if (score) scored.push({ item, score, i });
  });
  scored.sort((a, b) => b.score - a.score || a.item.label.length - b.item.label.length || a.i - b.i);
  return scored.slice(0, limit).map((s) => s.item);
}
