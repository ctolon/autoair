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

  it("explains an unavailable import check service", () => {
    const closed = problemText({
      code: "import_service",
      field: null,
      message:
        "the import check service is unavailable (not reachable: ConnectError); deploys are blocked (fail closed)",
    });
    expect(closed).toBe(
      "The import check service is unavailable (not reachable: ConnectError). Deploys are blocked until it answers again; ask an administrator to check the validator service.",
    );
    const open = problemText({
      code: "import_service",
      field: "raw_code",
      message: "the import check service is unavailable (HTTP 503); the DAG was not imported (fail open)",
    });
    expect(open).toBe(
      "The import check service is unavailable (HTTP 503). The DAG was not imported with Airflow, so import errors are not detected.",
    );
  });

  it("explains cyclic task group dependencies", () => {
    const message =
      "task groups depend on each other in a cycle: group 'g', task 'bridge'. A dependency into or out of any " +
      "task of a group counts for the whole group; Airflow 3.4 deprecates this and Airflow 3.5 is planned to " +
      "reject it. Move tasks between groups so that each group depends on the others in one direction only";
    expect(problemText({ code: "task_group_cycle", field: null, message })).toMatch(
      /^These task groups depend on each other in a cycle: group 'g', task 'bridge'\. /,
    );
  });

  it("falls back to the server message", () => {
    expect(problemText({ code: "x", field: null, message: "something new" })).toBe("something new");
  });
});
