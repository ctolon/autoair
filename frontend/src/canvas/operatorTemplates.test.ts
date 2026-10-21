import { describe, expect, it } from "vitest";

import type { Template } from "../api/gen/types.gen";
import type { BuilderNode, OperatorSpec, PythonTaskSpec } from "../lib/types";
import {
  configurablePaths,
  enforceLocks,
  instantiateBlueprint,
  lockMap,
  makeBlueprint,
  requiredPaths,
  setPath,
} from "./operatorTemplates";

const BASH = "airflow.providers.standard.operators.bash.BashOperator";

const bash: OperatorSpec = {
  kind: "operator",
  task_id: "notify",
  operator: BASH,
  params: { bash_command: "echo hi", env: { A: "1" } },
  retries: 3,
  pool: "slack",
  template_ref: { id: "old", version: "9" },
};

const node = (id: string, task_id: string): BuilderNode =>
  ({
    id,
    type: "operator",
    position: { x: 0, y: 0 },
    data: { spec: { kind: "operator", task_id, operator: BASH, params: {} } },
  }) as BuilderNode;

function template(over: Partial<Template> = {}): Template {
  return {
    id: "notify-step",
    title: "Notify",
    kind: "operator",
    builtin: false,
    version: "2",
    icon: "builtin:slack",
    blueprint: makeBlueprint(bash, { retries: "locked", "params.env": "locked", "params.bash_command": "required" }),
    ...over,
  };
}

describe("operator templates", () => {
  it("lists the configured fields of a task", () => {
    expect(configurablePaths(bash)).toEqual(["params.bash_command", "params.env", "retries", "pool"]);
    const py = { kind: "python_task", task_id: "t", function_name: "t", code: "return 1" } as PythonTaskSpec;
    expect(configurablePaths(py)).toContain("code");
  });

  it("builds a blueprint without per-task identity; required values are left out", () => {
    const bp = template().blueprint;
    expect(bp?.locked).toEqual(["retries", "params.env"]);
    expect(bp?.required).toEqual(["params.bash_command"]);
    const stored = bp?.node as OperatorSpec & { id: string };
    expect(stored.id).toBe("template");
    expect(stored).not.toHaveProperty("template_ref");
    expect(stored.params).toEqual({ env: { A: "1" } });
  });

  it("instantiates with a fresh task id, the inputs and a template_ref", () => {
    const tpl = template();
    expect(requiredPaths(tpl)).toEqual(["params.bash_command"]);
    const spec = instantiateBlueprint(tpl, [node("a", "notify")], {
      "params.bash_command": "echo bye",
    }) as OperatorSpec;
    expect(spec.task_id).toBe("notify_1");
    expect(spec.params).toEqual({ env: { A: "1" }, bash_command: "echo bye" });
    expect(spec.retries).toBe(3);
    expect(spec.template_ref).toEqual({ id: "notify-step", version: "2" });
    expect(spec.icon).toBe("builtin:slack");
    expect(spec).not.toHaveProperty("id");
    expect(spec).not.toHaveProperty("position");
  });

  it("gives python tasks a unique function name", () => {
    const py = { kind: "python_task", task_id: "load", function_name: "load", code: "pass" } as PythonTaskSpec;
    const tpl = template({ blueprint: makeBlueprint(py, {}) });
    const existing = [{ id: "p", type: "python_task", position: { x: 0, y: 0 }, data: { spec: py } } as BuilderNode];
    const spec = instantiateBlueprint(tpl, existing) as PythonTaskSpec;
    expect(spec.task_id).toBe("load_1");
    expect(spec.function_name).toBe("load_1");
  });

  it("keeps locked values when the inspector patches them", () => {
    const locks = lockMap(template());
    expect([...locks.entries()]).toEqual([
      ["retries", 3],
      ["params.env", { A: "1" }],
    ]);
    const current = setPath(bash, "params.bash_command", "x");
    expect(enforceLocks({ retries: 0, pool: "p" }, locks, current)).toEqual({ retries: 3, pool: "p" });
    expect(enforceLocks({ params: { bash_command: "y" } }, locks, current)).toEqual({
      params: { bash_command: "y", env: { A: "1" } },
    });
    expect(enforceLocks({ queue: "q" }, new Map(), current)).toEqual({ queue: "q" });
  });
});
