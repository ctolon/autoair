import { describe, expect, it } from "vitest";

import { detectIndentUnit, indentUnitFor } from "./indent";

describe("detectIndentUnit", () => {
  it("finds 4 spaces in a Python module", () => {
    const py = "def f(x):\n    if x:\n        return 1\n    return 2\n";
    expect(detectIndentUnit(py)).toBe("    ");
  });
  it("finds 2 spaces even when blocks nest deeper", () => {
    expect(detectIndentUnit("a:\n  b:\n    c: 1\n  d: 2\n")).toBe("  ");
  });
  it("finds tabs", () => {
    expect(detectIndentUnit("func f() {\n\treturn\n}\n")).toBe("\t");
  });
  it("ignores blank lines and continuation jumps", () => {
    expect(detectIndentUnit("x = call(\n                 1)\nif y:\n\n    z = 1\n")).toBe("    ");
  });
  it("returns null without indented lines", () => {
    expect(detectIndentUnit("")).toBeNull();
    expect(detectIndentUnit("x = 1\ny = 2")).toBeNull();
  });
});

describe("indentUnitFor", () => {
  it("defaults to 4 spaces for Python and 2 elsewhere", () => {
    expect(indentUnitFor("python", "")).toBe("    ");
    expect(indentUnitFor("yaml", "")).toBe("  ");
    expect(indentUnitFor("json", "{}")).toBe("  ");
  });
  it("follows the content", () => {
    expect(indentUnitFor("python", "if x:\n  y = 1\n")).toBe("  ");
    expect(indentUnitFor("json", '{\n    "a": 1\n}')).toBe("    ");
  });
});
