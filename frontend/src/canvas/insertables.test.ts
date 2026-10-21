import { describe, expect, it } from "vitest";

import type { OperatorSummary } from "../api/gen/types.gen";

import { decoratorLabel, operatorGroups, operatorItem, taskflowEquivalent, unsupportedReason } from "./insertables";

const t = (key: string, o?: Record<string, unknown>) => `${key}|${JSON.stringify(o ?? {})}`;
const op = (over: Partial<OperatorSummary>): OperatorSummary => ({
  fqn: "airflow.providers.standard.operators.python.BranchPythonOperator",
  class_name: "BranchPythonOperator",
  module: "airflow.providers.standard.operators.python",
  provider: "apache-airflow-providers-standard",
  category: "operator",
  ...over,
});

const callableOp = op({
  requires_python_code: true,
  python_ref_params: ["python_callable"],
  equivalent_decorator: "branch",
});

describe("operators taking Python objects", () => {
  it("insert the operator itself (callables are Python references)", () => {
    const item = operatorItem(callableOp, t, { pythonCodeAllowed: true });
    expect(item.disabledReason).toBeNull();
    const payload = item.payload();
    expect(payload.kind === "spec" && payload.spec).toMatchObject({ kind: "operator", operator: callableOp.fqn });
  });

  it("suggest the TaskFlow alternative in the hint", () => {
    const hint = operatorItem(callableOp, t).hint;
    expect(hint).toContain("palette.callableWithAlternative");
    expect(hint).toContain("@task.branch");
    expect(taskflowEquivalent(callableOp)).toBe("branch");
    const plain = op({
      requires_python_code: true,
      python_ref_params: ["producer_function"],
      equivalent_decorator: null,
    });
    expect(operatorItem(plain, t).hint).toContain("palette.callableOperator");
  });

  it("are disabled only when the server forbids Python code", () => {
    expect(unsupportedReason(callableOp, t, { pythonCodeAllowed: false })).toContain("palette.needsPythonCode");
    expect(unsupportedReason(callableOp, t, { pythonCodeAllowed: true })).toBeNull();
    expect(unsupportedReason(callableOp, t)).toBeNull(); // meta not loaded yet
  });

  it("labels decorators correctly (@task, not @task.task)", () => {
    expect(decoratorLabel("task")).toBe("@task");
    expect(decoratorLabel("branch_external_python")).toBe("@task.branch_external_python");
    expect(taskflowEquivalent(op({ equivalent_decorator: "future_thing" }))).toBeNull();
  });

  it("leaves ordinary operators untouched", () => {
    const item = operatorItem(op({ summary: "Runs things" }), t, { pythonCodeAllowed: false });
    expect(item.disabledReason).toBeNull();
    expect(item.hint).toBe("Runs things");
  });
});

describe("palette groups", () => {
  const item = (fqn: string, over: Partial<OperatorSummary> = {}) =>
    operatorItem(op({ fqn, class_name: fqn.split(".").pop() ?? fqn, ...over }), t);

  it("groups by catalog group, else provider, ordered by group_order then title", () => {
    const groups = operatorGroups([
      item("a.ZetaOperator", { provider: "zeta-pkg" }),
      item("bifrost.ops.IngestOperator", { provider: "bifrost", group: "Bifrost", group_order: 10 }),
      item("b.AlphaOperator", { provider: "alpha-pkg", group_order: 100 }),
      item("bifrost.ops.PurgeOperator", { provider: "bifrost", group: "Bifrost", group_order: 10 }),
      item("c.FirstOperator", { provider: "x", group: "First", group_order: 1 }),
    ]);
    expect(groups.map((g) => [g.id, g.title])).toEqual([
      ["group:First", "First"],
      ["group:Bifrost", "Bifrost"],
      ["provider:alpha-pkg", "alpha-pkg"],
      ["provider:zeta-pkg", "zeta-pkg"],
    ]);
    // Input order (search ranking) is kept inside a group.
    expect(groups[1]?.items.map((i) => i.label)).toEqual(["IngestOperator", "PurgeOperator"]);
  });

  it("searches the group name", () => {
    expect(item("x.Y", { group: "Bifrost" }).keywords).toContain("Bifrost");
  });
});
