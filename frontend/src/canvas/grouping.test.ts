import { describe, expect, it } from "vitest";

import type { BuilderEdge, BuilderNode } from "../lib/types";
import { GROUP_PADDING } from "./geometry";
import { collapsedView, displayEdgeEndpoints, groupNodes, ungroupNode } from "./grouping";

const op = (id: string, x: number, y: number, parentId?: string): BuilderNode => ({
  id,
  type: "operator",
  position: { x, y },
  measured: { width: 200, height: 60 },
  ...(parentId ? { parentId } : {}),
  data: { spec: { kind: "operator", task_id: id, operator: "m.Op", params: {} } },
});
const group = (id: string, x: number, y: number, parentId?: string, collapsed = false): BuilderNode => ({
  id,
  type: "task_group",
  position: { x, y },
  style: { width: 600, height: 400 },
  ...(parentId ? { parentId } : {}),
  data: { spec: { kind: "task_group", group_id: id }, ...(collapsed ? { collapsed: true } : {}) },
});
const edge = (source: string, target: string): BuilderEdge => ({ id: `${source}-${target}`, source, target });

describe("groupNodes", () => {
  it("sizes the group around the members and keeps their absolute positions", () => {
    const nodes = [op("a", 100, 100), op("b", 400, 250), op("c", 900, 900)];
    const out = groupNodes(nodes, ["a", "b"], { id: "g", spec: { kind: "task_group", group_id: "g" } });
    expect(out).not.toBeNull();
    const byId = new Map(out?.map((n) => [n.id, n]));
    const g = byId.get("g");
    const order = out?.map((n) => n.id) ?? [];
    expect(order.indexOf("g")).toBeLessThan(order.indexOf("a")); // parents first
    expect(g?.position).toEqual({ x: 100 - GROUP_PADDING.x, y: 100 - GROUP_PADDING.top });
    expect(g?.style).toEqual({
      width: 300 + 200 + 2 * GROUP_PADDING.x,
      height: 150 + 60 + GROUP_PADDING.top + GROUP_PADDING.bottom,
    });
    expect(byId.get("a")).toMatchObject({ parentId: "g", position: { x: GROUP_PADDING.x, y: GROUP_PADDING.top } });
    expect(byId.get("b")).toMatchObject({
      parentId: "g",
      position: { x: 300 + GROUP_PADDING.x, y: 150 + GROUP_PADDING.top },
    });
    expect(byId.get("c")?.parentId).toBeUndefined();
  });

  it("creates the group inside the members' common parent", () => {
    const nodes = [group("outer", 1000, 1000), op("a", 50, 80, "outer"), op("b", 300, 80, "outer")];
    const out = groupNodes(nodes, ["a", "b"], { id: "g", spec: { kind: "task_group", group_id: "g" } });
    const g = out?.find((n) => n.id === "g");
    expect(g).toMatchObject({ parentId: "outer", position: { x: 50 - GROUP_PADDING.x, y: 80 - GROUP_PADDING.top } });
    expect(out?.find((n) => n.id === "a")?.position).toEqual({ x: GROUP_PADDING.x, y: GROUP_PADDING.top });
  });
});

describe("ungroupNode", () => {
  it("restores absolute positions and rewires group edges to entry/exit members", () => {
    const nodes = [
      op("src", 0, 0),
      group("g", 300, 100),
      op("a", 20, 60, "g"),
      op("b", 260, 60, "g"),
      op("dst", 1200, 0),
    ];
    const edges = [edge("src", "g"), edge("a", "b"), edge("g", "dst")];
    const out = ungroupNode(nodes, edges, "g");
    expect(out?.nodes.find((n) => n.id === "g")).toBeUndefined();
    expect(out?.nodes.find((n) => n.id === "a")).toMatchObject({ position: { x: 320, y: 160 } });
    expect(out?.nodes.find((n) => n.id === "a")?.parentId).toBeUndefined();
    expect(out?.edges.map((e) => `${e.source}->${e.target}`).sort()).toEqual(["a->b", "b->dst", "src->a"]);
  });

  it("moves members into the group's parent", () => {
    const nodes = [group("outer", 0, 0), group("g", 40, 50, "outer"), op("a", 10, 60, "g")];
    const out = ungroupNode(nodes, [], "g");
    expect(out?.nodes.find((n) => n.id === "a")).toMatchObject({ parentId: "outer", position: { x: 50, y: 110 } });
  });
});

describe("collapsed groups", () => {
  it("hides members and redirects their edges to the outermost collapsed group", () => {
    const nodes = [
      group("g", 0, 0, undefined, true),
      group("inner", 10, 10, "g"),
      op("a", 0, 0, "inner"),
      op("b", 900, 0),
    ];
    const view = collapsedView(nodes);
    expect([...view.hidden].sort()).toEqual(["a", "inner"]);
    expect(view.representative.get("a")).toBe("g");
    const ends = displayEdgeEndpoints([edge("a", "b"), edge("inner", "b")], view);
    expect(ends.get("a-b")).toEqual({ source: "g", target: "b", hidden: false });
    expect(ends.get("inner-b")?.hidden).toBe(true); // duplicate g->b
  });
});
