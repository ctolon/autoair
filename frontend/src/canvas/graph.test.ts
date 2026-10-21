import { describe, expect, it } from "vitest";

import type { BuilderEdge, BuilderNode } from "../lib/types";
import { checkConnection, hasCycle, upstreamTasks } from "./graph";

const op = (id: string, parentId?: string): BuilderNode => ({
  id,
  type: "operator",
  position: { x: 0, y: 0 },
  ...(parentId ? { parentId } : {}),
  data: { spec: { kind: "operator", task_id: id, operator: "x.y.Op", params: {} } },
});
const group = (id: string): BuilderNode => ({
  id,
  type: "task_group",
  position: { x: 0, y: 0 },
  data: { spec: { kind: "task_group", group_id: id } },
});
const edge = (source: string, target: string): BuilderEdge => ({ id: `${source}-${target}`, source, target });

describe("graph", () => {
  it("detects direct cycles", () => {
    expect(hasCycle([op("a"), op("b")], [edge("a", "b"), edge("b", "a")])).toBe(true);
    expect(hasCycle([op("a"), op("b")], [edge("a", "b")])).toBe(false);
  });

  it("detects cycles through task groups", () => {
    const nodes = [group("g"), op("a", "g"), op("b")];
    expect(checkConnection(nodes, [edge("g", "b")], "b", "a")).toEqual({
      ok: false,
      reason: "This connection would create a cycle",
    });
  });

  it("rejects self, duplicate and group-member connections", () => {
    const nodes = [group("g"), op("a", "g"), op("b")];
    expect(checkConnection(nodes, [], "a", "a").ok).toBe(false);
    expect(checkConnection(nodes, [edge("a", "b")], "a", "b").ok).toBe(false);
    expect(checkConnection(nodes, [], "g", "a").ok).toBe(false);
    expect(checkConnection(nodes, [], "a", "b").ok).toBe(true);
  });

  it("finds upstream tasks including those feeding the parent group", () => {
    const nodes = [op("src"), group("g"), op("a", "g"), op("x")];
    const ids = upstreamTasks(nodes, [edge("src", "g")], "a").map((n) => n.id);
    expect(ids).toEqual(["src"]);
  });
});
