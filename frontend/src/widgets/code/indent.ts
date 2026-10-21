import type { CodeLanguage } from "./types";

/** PEP 8 for Python; 2 spaces stays CodeMirror's default elsewhere (YAML, JSON, Markdown…). */
const DEFAULT_UNIT: Partial<Record<CodeLanguage, string>> = { python: "    ", java: "    ", go: "\t" };

/**
 * The indentation a document already uses: a tab, or the smallest step (2–8 spaces) between
 * the indents of consecutive indented lines. Null when the text has no indented line.
 */
export function detectIndentUnit(text: string): string | null {
  let tabs = 0;
  let spaced = 0;
  const steps = new Map<number, number>();
  let previous = 0;
  for (const line of text.split("\n")) {
    if (!line.trim()) continue;
    const lead = /^[ \t]*/.exec(line)?.[0] ?? "";
    if (lead.startsWith("\t")) {
      tabs++;
      continue;
    }
    const width = lead.length;
    if (width > 0) spaced++;
    const step = width - previous;
    if (step >= 2 && step <= 8) steps.set(step, (steps.get(step) ?? 0) + 1);
    previous = width;
  }
  if (tabs > spaced) return "\t";
  if (!steps.size) return null;
  // The most frequent step; ties go to the smaller one (a 2-space file nests 4 deep too).
  const [best] = [...steps.entries()].sort(([a, n], [b, m]) => m - n || a - b);
  return " ".repeat(best?.[0] ?? 4);
}

/** The language's conventional indent unit (CodeMirror's own default is 2 spaces). */
export function defaultIndentUnit(language: CodeLanguage): string {
  return DEFAULT_UNIT[language] ?? "  ";
}

/** The indent unit for an editor: what the text uses, else the language's convention. */
export function indentUnitFor(language: CodeLanguage, text: string): string {
  return detectIndentUnit(text) ?? defaultIndentUnit(language);
}
