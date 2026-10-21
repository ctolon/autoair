import { describe, expect, it } from "vitest";

import en from "../i18n/locales/en/admin.json";
import tr from "../i18n/locales/tr/admin.json";
import { ADMIN_TABS } from "./store";

function keys(obj: Record<string, unknown>, prefix = ""): string[] {
  return Object.entries(obj).flatMap(([k, v]) =>
    v && typeof v === "object" ? keys(v as Record<string, unknown>, `${prefix}${k}.`) : [`${prefix}${k}`],
  );
}

describe("admin translations", () => {
  it("has the same keys in every language", () => {
    expect(keys(tr).sort()).toEqual(keys(en).sort());
  });

  it("names every tab", () => {
    for (const tab of ADMIN_TABS) {
      expect(en.tabs[tab]).toBeTruthy();
      expect(tr.tabs[tab]).toBeTruthy();
    }
  });
});
