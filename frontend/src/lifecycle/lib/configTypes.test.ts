import { describe, expect, it } from "vitest";

import { featureName, literalChoices } from "./configTypes";

describe("literalChoices", () => {
  it("splits Literal types into chips", () => {
    expect(literalChoices("'plugin' | 'standalone' | 'local'")).toEqual(["plugin", "standalone", "local"]);
    expect(literalChoices("'a' | 'b' | None")).toEqual(["a", "b", "None"]);
    expect(literalChoices("'only'")).toEqual(["only"]);
  });
  it("leaves other types alone", () => {
    expect(literalChoices("bool")).toBeNull();
    expect(literalChoices("int | None")).toBeNull();
    expect(literalChoices("list[str]")).toBeNull();
  });
  it("names features", () => {
    expect(featureName("features.code_editor")).toBe("code_editor");
  });
});
