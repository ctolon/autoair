/**
 * Ranking of the command palette (cmdk `filter`). Item texts are the cmdk keywords: the first
 * one is the title/label, the others are secondary (hint, group, synonyms). Scores, best first:
 *
 *   exact title (1) > title prefix (0.9) > word start in title (0.8) > substring in title (0.6)
 *   > word start / substring in a secondary text (0.45 / 0.35) > fuzzy subsequence of the title (≤0.15)
 *
 * Commands (values prefixed with `cmd:`) get a small boost so "dep" lists Deploy above any
 * operator whose name merely contains d…e…p.
 */
export const COMMAND_PREFIX = "cmd:";
const COMMAND_BOOST = 0.08;

const WORD_START = /[\s._\-/@:()[\]]+/;

const norm = (s: string) => s.toLowerCase().trim();

function words(text: string): string[] {
  // Split on separators and camelCase humps ("TriggerDagRunOperator" → trigger, dag, run, operator).
  return text
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .toLowerCase()
    .split(WORD_START)
    .filter(Boolean);
}

function fuzzy(text: string, term: string): number {
  // Subsequence match; tighter spans score higher (max 0.15).
  let from = 0;
  let first = -1;
  for (const ch of term) {
    const at = text.indexOf(ch, from);
    if (at < 0) return 0;
    if (first < 0) first = at;
    from = at + 1;
  }
  const span = from - first;
  return 0.15 * (term.length / span);
}

function termScore(term: string, primary: string, secondary: string[]): number {
  const title = norm(primary);
  if (title === term) return 1;
  if (title.startsWith(term)) return 0.9;
  if (words(primary).some((w) => w.startsWith(term))) return 0.8;
  if (title.includes(term)) return 0.6;
  let best = 0;
  for (const s of secondary) {
    if (!s) continue;
    if (words(s).some((w) => w.startsWith(term))) best = Math.max(best, 0.45);
    else if (norm(s).includes(term)) best = Math.max(best, 0.35);
  }
  if (best) return best;
  return fuzzy(title, term);
}

/** cmdk filter: 0 hides the item; higher ranks it higher (within and across groups). */
export function paletteScore(value: string, search: string, keywords: readonly string[] = []): number {
  const terms = norm(search).split(/\s+/).filter(Boolean);
  if (!terms.length) return 1;
  const [primary = value, ...secondary] = keywords.length ? keywords : [value];
  let total = 0;
  for (const term of terms) {
    const s = termScore(term, primary, [...secondary, value]);
    if (!s) return 0;
    total += s;
  }
  const score = total / terms.length;
  return value.startsWith(COMMAND_PREFIX) ? Math.min(1, score + COMMAND_BOOST) : score;
}
