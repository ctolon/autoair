/**
 * Order-insensitive comparison of JSON-like values (specs, mappings). `JSON.stringify` depends
 * on key insertion order, which differs between a spec loaded from the server and the same
 * spec rebuilt by the editor (`id` first vs. last, spread updates, …).
 */

type Options = {
  /** Treat `null` like a missing key (the server and the editor disagree on optional fields). */
  nullAsMissing?: boolean;
};

function normalize(value: unknown, opts: Options): unknown {
  if (Array.isArray(value)) return value.map((v) => (v === undefined ? null : normalize(v, opts)));
  if (value && typeof value === "object") {
    const out: Record<string, unknown> = {};
    for (const key of Object.keys(value).sort()) {
      const v = (value as Record<string, unknown>)[key];
      if (v === undefined || (opts.nullAsMissing && v === null)) continue;
      out[key] = normalize(v, opts);
    }
    return out;
  }
  return value;
}

/** JSON with object keys sorted recursively (`undefined` keys dropped, like `JSON.stringify`). */
export function canonicalJson(value: unknown, opts: Options = {}): string {
  return JSON.stringify(normalize(value, opts)) ?? "undefined";
}

/** Deep equality of JSON-like values, ignoring object key order. */
export function jsonEqual(a: unknown, b: unknown, opts: Options = {}): boolean {
  return a === b || canonicalJson(a, opts) === canonicalJson(b, opts);
}
