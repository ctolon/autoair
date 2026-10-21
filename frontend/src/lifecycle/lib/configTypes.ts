/**
 * Choices of a Literal type as the configuration API prints it ("'plugin' | 'standalone'",
 * "'a' | 'b' | None"); null for any other type.
 */
export function literalChoices(type: string): string[] | null {
  const parts = type.split("|").map((p) => p.trim());
  if (parts.length < 2) return /^'[^']*'$/.test(parts[0] ?? "") ? [(parts[0] ?? "").slice(1, -1)] : null;
  const out: string[] = [];
  for (const p of parts) {
    if (/^'[^']*'$/.test(p)) out.push(p.slice(1, -1));
    else if (p === "None") out.push("None");
    else return null;
  }
  return out.some((c) => c !== "None") ? out : null;
}

/** "features.ai" → "ai" for options of the features section. */
export function featureName(key: string): string {
  return key.startsWith("features.") ? key.slice("features.".length) : key;
}
