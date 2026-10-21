import { describe, expect, it } from "vitest";

import { problemText } from "./problemMessages";

describe("problemText", () => {
  it("explains known validator messages", () => {
    const text = problemText({ code: "policy", field: "owner", message: "an owner is required (default_args.owner)" });
    expect(text).toBe("An owner is required (Defaults → Owner).");
  });

  it("keeps the organisation's own policy messages verbatim", () => {
    for (const code of ["policy_rule", "policy_opa"]) {
      const message = "an owner is required [policy rule owner-required]";
      expect(problemText({ code, field: "owner", message })).toBe(message);
    }
  });

  it("explains raw code DAG policy messages", () => {
    const at = (message: string) => problemText({ code: "policy", field: "raw_code", message });
    expect(at("BashOperator is forbidden by policy")).toBe("BashOperator is forbidden by policy.");
    expect(at("dag_id must start with 'etl_'")).toBe("The dag_id must start with 'etl_'.");
    expect(at("dag_id must start with the team prefix 'data_'")).toBe(
      "The dag_id must start with the team prefix 'data_'.",
    );
    expect(
      problemText({ code: "import", field: "raw_code", message: "the file did not define DAG 'a' (found: b)" }),
    ).toBe("Airflow imported the file but found no DAG 'a' (found: b).");
  });

  it("falls back to the server message", () => {
    expect(problemText({ code: "x", field: null, message: "something new" })).toBe("something new");
  });
});
