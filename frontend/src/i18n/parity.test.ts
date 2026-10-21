import { describe, expect, it } from "vitest";

import { NAMESPACES } from ".";

type Tree = { [key: string]: string | Tree };

const files = import.meta.glob<Tree>("./locales/*/*.json", { eager: true, import: "default" });
const locale = (lang: string) =>
  Object.fromEntries(
    Object.entries(files)
      .filter(([path]) => path.startsWith(`./locales/${lang}/`))
      .map(([path, tree]) => [path.slice(`./locales/${lang}/`.length, -".json".length), tree]),
  );
const en = locale("en");
const tr = locale("tr");

/** Leaf keys (`a.b.c`) with their string values. */
function leaves(tree: Tree, prefix = ""): Map<string, string> {
  const out = new Map<string, string>();
  for (const [k, v] of Object.entries(tree)) {
    if (typeof v === "string") out.set(`${prefix}${k}`, v);
    else for (const [kk, vv] of leaves(v, `${prefix}${k}.`)) out.set(kk, vv);
  }
  return out;
}

/** i18next interpolation variables (`{{name}}`, `{{count, number}}`), sorted and de-duplicated. */
const vars = (text: string) => [...new Set([...text.matchAll(/\{\{\s*([^},\s]+)[^}]*\}\}/g)].map((m) => m[1]))].sort();

describe("en/tr parity", () => {
  it("has the same namespaces in both languages, all registered", () => {
    expect(Object.keys(tr).sort()).toEqual(Object.keys(en).sort());
    expect(Object.keys(en).sort()).toEqual([...NAMESPACES].sort());
  });

  for (const ns of Object.keys(en).sort()) {
    describe(ns, () => {
      const enLeaves = leaves(en[ns] ?? {});
      const trLeaves = leaves(tr[ns] ?? {});

      it("has the same keys", () => {
        expect(enLeaves.size).toBeGreaterThan(0);
        const missing = [...enLeaves.keys()].filter((k) => !trLeaves.has(k));
        const extra = [...trLeaves.keys()].filter((k) => !enLeaves.has(k));
        expect({ missingInTr: missing, onlyInTr: extra }).toEqual({ missingInTr: [], onlyInTr: [] });
      });

      it("uses the same interpolation variables", () => {
        const mismatched = [...enLeaves]
          .filter(([k, v]) => trLeaves.has(k) && vars(v).join() !== vars(trLeaves.get(k) ?? "").join())
          .map(([k, v]) => `${k}: en {${vars(v).join(", ")}} tr {${vars(trLeaves.get(k) ?? "").join(", ")}}`);
        expect(mismatched).toEqual([]);
      });

      it("has no empty translations", () => {
        const empty = [...trLeaves].filter(([k, v]) => !v.trim() && enLeaves.get(k)?.trim()).map(([k]) => k);
        expect(empty).toEqual([]);
      });
    });
  }
});
