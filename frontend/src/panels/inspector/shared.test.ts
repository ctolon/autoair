import { describe, expect, it } from "vitest";

import { tabForProblem } from "./shared";

describe("tabForProblem", () => {
  it("routes fields to their tab", () => {
    expect(tabForProblem({ field: "task_id", code: "duplicate_task_id" })).toBeNull();
    expect(tabForProblem({ field: "retries", code: "policy" })).toBe("execution");
    expect(tabForProblem({ field: "callbacks.on_failure.params", code: "callback" })).toBe("behaviour");
    expect(tabForProblem({ field: "mapping.items", code: "mapping" })).toBe("data");
    expect(tabForProblem({ field: "bash_command", code: "param" })).toBe("parameters");
    expect(tabForProblem({ field: null, code: "teardown" })).toBe("behaviour");
    expect(tabForProblem({ field: "doc_md", code: "x" })).toBe("docs");
  });
});
