import { describe, expect, it } from "vitest";

import { keepsHandWrittenFile } from "./preserved";

describe("keepsHandWrittenFile", () => {
  it("follows the source policy", () => {
    expect(keepsHandWrittenFile({})).toBe(false); // built in AutoAir: generated
    expect(keepsHandWrittenFile({ source: { origin: "python", code: "x" } })).toBe(true); // auto
    expect(keepsHandWrittenFile({ source: { origin: "python" } })).toBe(true); // file in the target
    expect(keepsHandWrittenFile({ source: { origin: "python", policy: "generate" } })).toBe(false);
    expect(keepsHandWrittenFile({ source: { policy: "strict-preserve" } })).toBe(true);
  });

  it("never applies to loader, YAML or raw code DAGs, nor to unknown values", () => {
    expect(keepsHandWrittenFile({ output_mode: "yaml", source: { origin: "python" } })).toBe(false);
    expect(keepsHandWrittenFile({ raw_code: "x = 1", source: { policy: "preserve" } })).toBe(false);
    expect(keepsHandWrittenFile({ source: { policy: 3, origin: null } })).toBe(false);
    expect(keepsHandWrittenFile({ source: null })).toBe(false);
  });
});
