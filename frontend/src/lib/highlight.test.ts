import { describe, expect, it } from "vitest";

import { highlightLines } from "./highlight";

describe("highlightLines", () => {
  it("tokenizes python with tok-* classes", () => {
    const lines = highlightLines("def f(x):\n    return 1", "python");
    expect(lines).toHaveLength(2);
    expect(lines[0]?.find((t) => t.text === "def")?.cls).toContain("tok-keyword");
    expect(
      lines
        .flat()
        .map((t) => t.text)
        .join(""),
    ).toBe("def f(x):    return 1");
  });
  it("leaves unknown languages as text", () => {
    expect(highlightLines("a\nb", "cobol")).toEqual([[{ text: "a", cls: "" }], [{ text: "b", cls: "" }]]);
  });
});
