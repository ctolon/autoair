import { describe, expect, it } from "vitest";

import type { DagSpec } from "../api/gen/types.gen";
import { schemaProblems } from "./schemaErrors";

const spec = {
  dag_id: "d",
  nodes: [
    { kind: "operator", id: "a", task_id: "a", operator: "x.Y" },
    { kind: "python_task", id: "py", task_id: "py", function_name: "f", code: "" },
  ],
} as unknown as DagSpec;

describe("schemaProblems", () => {
  it("locates node fields through the discriminator", () => {
    const err = {
      detail: [
        {
          loc: ["body", "nodes", 1, "python_task", "retry_policy", "rules", 0, "exception"],
          msg: "String should match pattern",
          type: "string_pattern_mismatch",
        },
      ],
    };
    expect(schemaProblems(err, spec)).toEqual([
      {
        severity: "error",
        code: "schema",
        message: "String should match pattern",
        node_id: "py",
        field: "retry_policy.rules.0.exception",
      },
    ]);
  });

  it("keeps DAG-level paths and skips union markers", () => {
    const err = { detail: [{ loc: ["body", "schedule", "function-after[check()]", "expression"], msg: "bad" }] };
    expect(schemaProblems(err, spec)?.[0]).toMatchObject({ node_id: null, field: "schedule.expression" });
  });

  it("returns null for non-validation errors", () => {
    expect(schemaProblems(new Error("boom"), spec)).toBeNull();
    expect(schemaProblems({ detail: "nope" }, spec)).toBeNull();
  });

  it("keeps dict keys with dashes or brackets that are real field names", () => {
    const err = { detail: [{ loc: ["body", "nodes", 0, "operator", "params", "my-key"], msg: "bad" }] };
    expect(schemaProblems(err, spec)?.[0]).toMatchObject({ node_id: "a", field: "params.my-key" });
  });
});
