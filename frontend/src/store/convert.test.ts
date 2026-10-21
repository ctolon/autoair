import { describe, expect, it } from "vitest";

import type { DagSpec } from "../api/gen/types.gen";
import { fromSpec, toSpec } from "./convert";

const spec: DagSpec = {
  spec_version: 2,
  dag_id: "demo",
  output_mode: "python",
  schedule: { type: "cron", expression: "0 6 * * *" },
  nodes: [
    {
      kind: "operator",
      id: "a",
      task_id: "a",
      operator: "m.Op",
      params: { x: 1 },
      parent_group: "g",
      position: { x: 10, y: 50 },
    },
    { kind: "task_group", id: "g", group_id: "grp", position: { x: 100, y: 100, width: 400, height: 300 } },
  ],
  edges: [{ id: "e1", source: "g", target: "a", label: "go" }],
};

describe("convert", () => {
  it("puts parents first and maps hierarchy to parentId", () => {
    const { nodes } = fromSpec(spec);
    expect(nodes.map((n) => n.id)).toEqual(["g", "a"]);
    expect(nodes[1]?.parentId).toBe("g");
    expect(nodes[0]?.style).toEqual({ width: 400, height: 300 });
  });

  it("round-trips through React Flow state", () => {
    const { dag, nodes, edges } = fromSpec(spec);
    const back = toSpec(dag, nodes, edges);
    const byId = Object.fromEntries((back.nodes ?? []).map((n) => [n.id, n]));
    expect(byId.a).toMatchObject({ kind: "operator", parent_group: "g", params: { x: 1 }, position: { x: 10, y: 50 } });
    expect(byId.g).toMatchObject({
      kind: "task_group",
      group_id: "grp",
      parent_group: null,
      position: { width: 400, height: 300 },
    });
    expect(back.edges).toEqual([{ id: "e1", source: "g", target: "a", label: "go" }]);
    expect(back.schedule).toEqual(spec.schedule);
  });
});

describe("UI-only state", () => {
  it("never writes a group's collapsed flag into the spec", () => {
    const { dag, nodes, edges } = fromSpec(spec);
    const collapsed = nodes.map((n) =>
      n.type === "task_group" ? { ...n, data: { ...n.data, collapsed: true }, hidden: false } : { ...n, hidden: true },
    );
    const back = toSpec(dag, collapsed, edges);
    const g = back.nodes?.find((n) => n.id === "g");
    expect(g).toBeDefined();
    expect(JSON.stringify(back)).not.toContain("collapsed");
    expect(JSON.stringify(back)).not.toContain("hidden");
  });
});
