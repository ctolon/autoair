import { describe, expect, it } from "vitest";

import { withTaskId } from "./title";

describe("full-screen editor title", () => {
  it("adds the task id once", () => {
    expect(withTaskId("bash_command", "extract")).toBe("bash_command — extract");
    expect(withTaskId("Docs — extract", "extract")).toBe("Docs — extract");
    expect(withTaskId("doc_md", null)).toBe("doc_md");
  });
});
