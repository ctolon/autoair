import { describe, expect, it } from "vitest";

import { withPolicy } from "./sourcePolicy";

const dag = { source: { policy: "preserve" as const, origin: "python" as const, code: "print(1)\n", base_sha: "abc" } };

describe("withPolicy", () => {
  it("keeps the hand-written code by default", () => {
    expect(withPolicy(dag, "generate")).toEqual({ ...dag.source, policy: "generate" });
  });

  it("drops the kept file when asked (users without code:raw)", () => {
    expect(withPolicy(dag, "generate", { dropCode: true })).toEqual({ policy: "generate", origin: "python" });
  });

  it("tolerates a missing or malformed source", () => {
    expect(withPolicy({}, "generate", { dropCode: true })).toEqual({ policy: "generate" });
    expect(withPolicy({ source: null }, "auto")).toEqual({ policy: "auto" });
  });
});
