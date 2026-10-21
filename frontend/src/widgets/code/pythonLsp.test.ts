import { describe, expect, it } from "vitest";

import { highlightParameter, insideCall } from "./pythonLsp";

describe("python signature help helpers", () => {
  it("detects an open call before the cursor (ignoring parentheses in strings)", () => {
    expect(insideCall("hook.get_records(")).toBe(true);
    expect(insideCall("f(a, g(b), ")).toBe(true);
    expect(insideCall("f(a)")).toBe(false);
    expect(insideCall('print(")")')).toBe(false);
    expect(insideCall('x = "("')).toBe(false);
  });

  it("marks the active parameter", () => {
    const sig = { label: "get_records(sql: str | list[str], parameters: dict[str, Any] = None)", active_parameter: 1 };
    expect(highlightParameter(sig)).toBe("get_records(sql: str | list[str], «parameters: dict[str, Any] = None»)");
    expect(highlightParameter({ ...sig, active_parameter: null })).toBe(sig.label);
  });
});
