import { describe, expect, it } from "vitest";

import en from "../i18n/locales/en/ai.json";
import tr from "../i18n/locales/tr/ai.json";
import { FEATURE_NAMESPACES, NAMESPACES } from "../i18n";
import { SCOPE_KEYS, clampScope } from "./TroubleshootDialog";

function keys(obj: Record<string, unknown>, prefix = ""): string[] {
  return Object.entries(obj).flatMap(([k, v]) =>
    v && typeof v === "object" ? keys(v as Record<string, unknown>, `${prefix}${k}.`) : [`${prefix}${k}`],
  );
}

describe("ai translations", () => {
  it("is a registered namespace", () => {
    expect(FEATURE_NAMESPACES).toContain("ai");
    expect(NAMESPACES).toContain("ai");
  });

  it("has the same keys in English and Turkish", () => {
    expect(keys(tr).sort()).toEqual(keys(en).sort());
  });

  it("names every troubleshooting scope", () => {
    for (const key of SCOPE_KEYS) {
      expect(en.troubleshoot.scopes[key]).toBeTruthy();
      expect(tr.troubleshoot.scopes[key]).toBeTruthy();
    }
  });
});

describe("clampScope", () => {
  it("never asks for more than the server allows", () => {
    const out = clampScope({ xcom_values: true, pools: true, filesystem: true }, { pools: true, xcom_values: false });
    expect(out.pools).toBe(true);
    expect(out.xcom_values).toBe(false);
    expect(out.filesystem).toBe(false);
    expect(Object.keys(out).sort()).toEqual([...SCOPE_KEYS].sort());
  });
});
