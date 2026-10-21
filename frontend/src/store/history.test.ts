import { afterEach, describe, expect, it, vi } from "vitest";

import type { DagSpec } from "../api/gen/types.gen";
import { undo } from "../canvas/actions";
import { useBuilder } from "./builder";

const spec = {
  spec_version: 2,
  dag_id: "demo",
  output_mode: "python",
  nodes: [{ kind: "operator", id: "a", task_id: "extract", operator: "m.Op", position: { x: 0, y: 0 } }],
  edges: [],
} as unknown as DagSpec;
const s = () => useBuilder.getState();
const t = () => useBuilder.temporal.getState();
const posX = (id: string) => s().nodes.find((n) => n.id === id)?.position.x;

describe("undo history", () => {
  afterEach(() => vi.useRealTimers());

  it("undo right after a drag reverts the drag, not the step before it", () => {
    vi.useFakeTimers();
    vi.stubGlobal("requestAnimationFrame", () => 0);
    s().load(spec, false);
    s().addNode({ kind: "operator", id: "x", task_id: "t2", operator: "m.Op", position: { x: 0, y: 0 } } as never, {
      x: 100,
      y: 100,
    });
    s().moveNodes(["a"], 50, 0); // drag still pending in the 250 ms group window
    undo();
    expect(posX("a")).toBe(0); // the drag is undone…
    expect(s().nodes).toHaveLength(2); // …the insert is kept
    vi.advanceTimersByTime(300); // a late timer must not resurrect anything
    expect(posX("a")).toBe(0);
    expect(t().futureStates.length).toBe(1); // redo still available
    vi.unstubAllGlobals();
  });

  it("separate edits are separate steps", () => {
    s().load(spec, false);
    s().addNode({ kind: "operator", id: "x", task_id: "t2", operator: "m.Op", position: { x: 0, y: 0 } } as never, {
      x: 100,
      y: 100,
    });
    s().addNode({ kind: "operator", id: "y", task_id: "t3", operator: "m.Op", position: { x: 0, y: 0 } } as never, {
      x: 200,
      y: 100,
    });
    expect(t().pastStates.length).toBe(2);
  });

  it("replacing the document (code mode apply) is one undoable step that keeps the saved identity", () => {
    s().load(spec, true, { etag: "e1" });
    const next = {
      ...spec,
      nodes: [
        ...(spec.nodes ?? []),
        { kind: "operator", id: "b", task_id: "load", operator: "m.Op", position: { x: 9, y: 9 } },
      ],
    } as unknown as DagSpec;
    s().replaceSpec(next);
    expect(s().nodes).toHaveLength(2);
    expect(s().savedDagId).toBe("demo");
    expect(s().baseEtag).toBe("e1");
    expect(s().dirty).toBe(true);
    undo();
    expect(s().nodes).toHaveLength(1);
  });
  it("groups a burst of typing in a raw code DAG into one step", () => {
    vi.useFakeTimers();
    s().reset("raw");
    useBuilder.setState((st) => ({ dag: { ...st.dag, raw_code: "" } }));
    useBuilder.temporal.getState().clear();
    for (const text of ["x", "x ", "x =", "x = 1"]) s().setDag({ raw_code: text });
    vi.advanceTimersByTime(300);
    expect(t().pastStates.length).toBe(1);
    undo();
    expect(s().dag.raw_code).toBe("");
  });
});
