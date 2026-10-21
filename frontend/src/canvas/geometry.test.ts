import { describe, expect, it } from "vitest";

import type { BuilderNode } from "../lib/types";

import { TASK_SIZE, freeSpot } from "./geometry";

const task = (id: string, x: number, y: number): BuilderNode =>
  ({ id, type: "operator", position: { x, y }, data: { spec: { kind: "operator" } } }) as unknown as BuilderNode;

describe("freeSpot", () => {
  it("keeps the requested spot when it is free", () => {
    expect(freeSpot([], { x: 10, y: 20 })).toEqual({ x: 10, y: 20 });
  });

  it("moves below occupied spots instead of stacking", () => {
    const nodes = [task("a", 0, 0)];
    const first = freeSpot(nodes, { x: 0, y: 0 });
    expect(first.y).toBeGreaterThanOrEqual(TASK_SIZE.height);
    const second = freeSpot([...nodes, task("b", first.x, first.y)], { x: 0, y: 0 });
    expect(second.y).toBeGreaterThan(first.y);
  });

  it("ignores task groups (a node may be placed inside one)", () => {
    const group = { ...task("g", 0, 0), type: "task_group", style: { width: 400, height: 300 } } as BuilderNode;
    expect(freeSpot([group], { x: 20, y: 60 })).toEqual({ x: 20, y: 60 });
  });
});
