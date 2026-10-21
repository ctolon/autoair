import { describe, expect, it } from "vitest";

import { canonicalJson, jsonEqual } from "./canonical";

describe("canonicalJson", () => {
  it("ignores object key order at every depth", () => {
    expect(canonicalJson({ b: 1, a: { d: [1, { y: 2, x: 1 }], c: 3 } })).toBe(
      canonicalJson({ a: { c: 3, d: [1, { x: 1, y: 2 }] }, b: 1 }),
    );
  });
  it("keeps array order and drops undefined keys", () => {
    expect(jsonEqual([1, 2], [2, 1])).toBe(false);
    expect(jsonEqual({ a: 1, b: undefined }, { a: 1 })).toBe(true);
  });
  it("treats null as missing only when asked", () => {
    expect(jsonEqual({ a: 1, b: null }, { a: 1 })).toBe(false);
    expect(jsonEqual({ a: 1, b: null }, { a: 1 }, { nullAsMissing: true })).toBe(true);
    expect(jsonEqual({ a: 1 }, { a: 2 }, { nullAsMissing: true })).toBe(false);
  });
});
