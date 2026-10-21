import { beforeEach, describe, expect, it } from "vitest";

import type { DagSpec } from "../api/gen/types.gen";
import { useBuilder } from "./builder";

const spec: DagSpec = {
  spec_version: 3,
  dag_id: "demo",
  output_mode: "python",
  nodes: [
    { kind: "operator", id: "a", task_id: "extract", operator: "m.Op", position: { x: 0, y: 0 } },
    {
      kind: "python_task",
      id: "b",
      task_id: "load",
      function_name: "load",
      code: "return x",
      arguments: { x: { $type: "xcom", node: "a" } },
      position: { x: 300, y: 0 },
    },
  ],
  edges: [{ id: "e1", source: "a", target: "b" }],
};

const s = () => useBuilder.getState();

describe("builder store canvas actions", () => {
  beforeEach(() => s().load(spec, false));

  it("copies and pastes with fresh ids, keys and remapped XCom refs", () => {
    const ids = s().insertFragment(s().exportFragment(["a", "b"]), { x: 1000, y: 1000 });
    expect(ids).toHaveLength(2);
    const added = s().nodes.filter((n) => ids.includes(n.id));
    const keys = added.map((n) =>
      n.data.spec.kind === "operator" || n.data.spec.kind === "python_task" ? n.data.spec.task_id : "",
    );
    expect(keys.sort()).toEqual(["extract_1", "load_1"]);
    const load = added.find((n) => n.data.spec.kind === "python_task");
    const extract = added.find((n) => n.data.spec.kind === "operator");
    expect(load?.data.spec).toMatchObject({ arguments: { x: { $type: "xcom", node: extract?.id } } });
    expect(s().edges.some((e) => e.source === extract?.id && e.target === load?.id)).toBe(true);
    expect(added.every((n) => n.selected)).toBe(true);
    expect(s().dirty).toBe(true);
  });

  it("groups, collapses (UI only) and ungroups", () => {
    const g = s().groupNodes(["a", "b"]);
    expect(g).toBeTruthy();
    expect(s().nodes.filter((n) => n.parentId === g)).toHaveLength(2);
    s().markSaved("demo");
    s().setCollapsed(g as string, true);
    expect(s().dirty).toBe(false);
    expect(JSON.stringify(s().toSpec())).not.toContain("collapsed");
    s().ungroup(g as string);
    expect(s().nodes.find((n) => n.id === "a")).toMatchObject({ position: { x: 0, y: 0 } });
    expect(s().nodes.some((n) => n.type === "task_group")).toBe(false);
  });

  it("inserts a node on an edge and rewires it", () => {
    const id = s().insertOnEdge("e1", { kind: "operator", task_id: "mid", operator: "m.Op", params: {} });
    expect(id).toBeTruthy();
    const pairs = s()
      .edges.map((e) => `${e.source}->${e.target}`)
      .sort();
    expect(pairs).toEqual([`${id}->b`, `a->${id}`].sort());
  });

  it("removes nodes and edges in one step", () => {
    s().removeElements(["a"]);
    expect(s().nodes.map((n) => n.id)).toEqual(["b"]);
    expect(s().edges).toEqual([]);
  });
});
