import { describe, expect, it } from "vitest";

import {
  componentRows,
  levelPalette,
  outcomePalette,
  parseVersions,
  progressPercent,
  safeHref,
  toggleComponent,
} from "./kbFormat";

describe("kb admin helpers", () => {
  it("parses versions and reports invalid entries", () => {
    expect(parseVersions("installed, 3.1.0  stable 3.1.0")).toEqual({
      versions: ["installed", "3.1.0", "stable"],
      invalid: [],
    });
    expect(parseVersions("ok bad/one").invalid).toEqual(["bad/one"]);
    expect(parseVersions(undefined as unknown as string)).toEqual({ versions: [], invalid: [] });
  });

  it("computes progress without dividing by zero", () => {
    expect(progressPercent(5, 10)).toBe(50);
    expect(progressPercent(5, 0)).toBeNull();
    expect(progressPercent(5, null)).toBeNull();
    expect(progressPercent(20, 10)).toBe(100);
  });

  it("sorts component rows and tolerates odd data", () => {
    expect(componentRows({ core: 2, providers: 5 }, { core: ["3.1.0"] })).toEqual([
      { component: "providers", pages: 5, versions: [] },
      { component: "core", pages: 2, versions: ["3.1.0"] },
    ]);
    expect(componentRows(null, null)).toEqual([]);
    expect(componentRows({ x: "a" as unknown as number }, {})).toEqual([]);
  });

  it("maps levels and outcomes to palettes", () => {
    expect(levelPalette("warning")).toBe("orange");
    expect(levelPalette("info")).toBe("blue");
    expect(levelPalette(undefined)).toBe("red");
    expect(outcomePalette("ok")).toBe("green");
    expect(outcomePalette("failed")).toBe("red");
    expect(outcomePalette(null)).toBe("gray");
  });

  it("toggles components", () => {
    expect(toggleComponent(["core"], "providers")).toEqual(["core", "providers"]);
    expect(toggleComponent(["core", "providers"], "core")).toEqual(["providers"]);
    expect(toggleComponent(null, "core")).toEqual(["core"]);
  });

  it("only links http(s) URLs", () => {
    expect(safeHref("https://airflow.apache.org/docs/")).toBe("https://airflow.apache.org/docs/");
    expect(safeHref("kb://local/x.md")).toBeNull();
    expect(safeHref("javascript:alert(1)")).toBeNull();
  });
});
