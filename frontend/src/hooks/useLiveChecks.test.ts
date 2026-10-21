import { describe, expect, it } from "vitest";

import { mergeProblems } from "./useLiveChecks";

type P = { severity: "error" | "warning" | "info"; code: string; message: string; node_id?: string | null };

describe("mergeProblems", () => {
  it("drops the codegen error of a node whose syntax error is already reported", () => {
    const report: P[] = [{ severity: "error", code: "syntax", message: "line 1", node_id: "t" }];
    const codegen: P[] = [{ severity: "error", code: "codegen", message: "python syntax error", node_id: "t" }];
    expect(mergeProblems(report, codegen)).toEqual(report);
  });

  it("keeps codegen errors when validation passed or for other nodes", () => {
    const codegen: P[] = [{ severity: "error", code: "codegen", message: "boom", node_id: "u" }];
    expect(mergeProblems([], codegen)).toEqual(codegen);
    const report: P[] = [{ severity: "error", code: "syntax", message: "x", node_id: "t" }];
    expect(mergeProblems(report, codegen)).toHaveLength(2);
  });
});
