import { afterEach, describe, expect, it, vi } from "vitest";

import type { DagSpec, Problem } from "../api/gen/types.gen";
import { undo } from "../canvas/actions";
import { stableCollapsedView } from "../canvas/grouping";
import { liveCheckDelay } from "../canvas/largeGraph";
import type { BuilderNode } from "../lib/types";
import { type RunOverlay, useBuilder } from "./builder";
import { historyEqual, liveDocEqual } from "./docEqual";
import { diffStatusOf, groupTaskMembers, nodeProblems, problemsByNode, structureKey } from "./indexes";

/** A spec with `groups` task groups of `per` operator tasks each, chained. */
function bigSpec(groups: number, per: number): DagSpec {
  const nodes: unknown[] = [];
  const edges: unknown[] = [];
  for (let g = 0; g < groups; g++) {
    nodes.push({ kind: "task_group", id: `g${g}`, group_id: `stage_${g}`, position: { x: g * 900, y: 0 } });
    for (let i = 0; i < per; i++) {
      const id = `n${g}_${i}`;
      nodes.push({
        kind: "operator",
        id,
        task_id: `t${g}_${i}`,
        operator: "airflow.providers.standard.operators.bash.BashOperator",
        params: { bash_command: `echo ${g} ${i}` },
        parent_group: `g${g}`,
        position: { x: 30 + i * 260, y: 60 },
      });
      if (i) edges.push({ source: `n${g}_${i - 1}`, target: id });
    }
    if (g) edges.push({ source: `g${g - 1}`, target: `g${g}` });
  }
  return { spec_version: 3, dag_id: "big", nodes, edges } as unknown as DagSpec;
}

const s = () => useBuilder.getState();
const json = (x: unknown) => JSON.stringify(x);

describe("docEqual", () => {
  it("historyEqual agrees with comparing JSON", () => {
    s().load(bigSpec(3, 4), false);
    const snap = () => ({
      dag: s().dag,
      nodes: s().nodes.map(({ id, type, position, parentId, data, style }) => ({
        id,
        type,
        position,
        parentId,
        data,
        style,
      })),
      edges: s().edges.map(({ id, source, target, label }) => ({ id, source, target, label })),
    });
    const a = snap();
    expect(historyEqual(a, snap())).toBe(true);
    // Equal content in new objects (e.g. a document rebuilt by collaboration) is equal.
    const copy = JSON.parse(json(a)) as typeof a;
    expect(historyEqual(a, copy)).toBe(json(a) === json(copy));
    const moved = { ...a, nodes: a.nodes.map((n, i) => (i === 2 ? { ...n, position: { x: 1, y: 2 } } : n)) };
    expect(historyEqual(a, moved)).toBe(false);
    const edited = {
      ...a,
      nodes: a.nodes.map((n, i) => (i === 3 ? { ...n, data: { ...n.data, spec: { ...n.data.spec, label: "x" } } } : n)),
    };
    expect(historyEqual(a, edited)).toBe(false);
    const relabelled = { ...a, edges: a.edges.map((e, i) => (i === 0 ? { ...e, label: "go" } : e)) };
    expect(historyEqual(a, relabelled)).toBe(false);
    expect(historyEqual(a, { ...a, dag: { ...a.dag, dag_id: "other" } })).toBe(false);
    expect(historyEqual(a, { ...a, nodes: a.nodes.slice(1) })).toBe(false);
  });

  it("liveDocEqual ignores positions and selection, not specs or edges", () => {
    s().load(bigSpec(2, 3), false);
    const before = s();
    s().moveNodes(["n0_1"], 40, 0);
    s().setSelection(["n1_0"]);
    expect(liveDocEqual(before, s())).toBe(true);
    s().updateSpec("n0_1", { label: "renamed" });
    expect(liveDocEqual(before, s())).toBe(false);
    const edited = s();
    s().setEdgeLabel(s().edges[0]?.id ?? "", "ready");
    expect(liveDocEqual(edited, s())).toBe(false);
  });
});

describe("indexes", () => {
  const p = (node_id: string | null, code: string, severity: Problem["severity"] = "error"): Problem =>
    ({ node_id, code, severity, message: code }) as Problem;

  it("groups problems per node and keeps unchanged lists across validations", () => {
    const first = [p("a", "x"), p("b", "y"), p("a", "z", "warning"), p(null, "dag")];
    expect(nodeProblems(first, "a").map((q) => q.code)).toEqual(["x", "z"]);
    expect(nodeProblems(first, "c")).toBe(nodeProblems(first, "d")); // shared empty list
    const aList = nodeProblems(first, "a");
    // A new validation: same problems for "a", new ones for "b".
    const second = [p("a", "x"), p("a", "z", "warning"), p("b", "other")];
    expect(nodeProblems(second, "a")).toBe(aList);
    expect(nodeProblems(second, "b").map((q) => q.code)).toEqual(["other"]);
    expect(problemsByNode(second)).toBe(problemsByNode(second));
  });

  it("diff status: added wins over changed", () => {
    const diff = { label: "v1", added: ["a", "c"], removed: [], changed: ["b", "c"] };
    expect(diffStatusOf(diff, "a")).toBe("added");
    expect(diffStatusOf(diff, "b")).toBe("changed");
    expect(diffStatusOf(diff, "c")).toBe("added");
    expect(diffStatusOf(diff, "z")).toBeNull();
    expect(diffStatusOf(null, "a")).toBeNull();
  });

  it("group members are the tasks at any depth", () => {
    const nodes = [
      { id: "g", type: "task_group", data: { spec: { kind: "task_group", group_id: "g" } } },
      { id: "h", type: "task_group", parentId: "g", data: { spec: { kind: "task_group", group_id: "h" } } },
      { id: "a", type: "operator", parentId: "g", data: { spec: { kind: "operator", task_id: "a" } } },
      { id: "b", type: "operator", parentId: "h", data: { spec: { kind: "operator", task_id: "b" } } },
      { id: "note", type: "note", parentId: "g", data: { spec: { kind: "note" } } },
    ] as unknown as BuilderNode[];
    const members = groupTaskMembers(nodes);
    expect(members.get("g")).toEqual(["a", "b"]);
    expect(members.get("h")).toEqual(["b"]);
    expect(groupTaskMembers(nodes)).toBe(members);
  });

  it("structure key ignores positions, follows ids, keys and parents", () => {
    s().load(bigSpec(2, 2), false);
    const key = structureKey(s().nodes);
    s().moveNodes(["n0_0"], 10, 10);
    expect(structureKey(s().nodes)).toBe(key);
    s().updateSpec("n0_0", { task_id: "renamed" });
    expect(structureKey(s().nodes)).not.toBe(key);
  });

  it("the collapsed view is reused while no parent or collapsed flag changes", () => {
    s().load(bigSpec(2, 2), false);
    s().setCollapsed("g0", true);
    const view = stableCollapsedView(s().nodes);
    expect(view.hidden.has("n0_0")).toBe(true);
    s().moveNodes(["g1"], 5, 5);
    expect(stableCollapsedView(s().nodes)).toBe(view);
    s().setCollapsed("g0", false);
    expect(stableCollapsedView(s().nodes).hidden.size).toBe(0);
  });
});

describe("run overlay", () => {
  it("keeps unchanged per-node entries (and the overlay) when polled again", () => {
    s().load(bigSpec(1, 3), false);
    const overlay = (state: string): RunOverlay => ({
      dagId: "big",
      runId: "r1",
      states: { n0_0: { state: "success", tryNumber: 1 }, n0_1: { state, tryNumber: 1 } },
    });
    s().setRunOverlay(overlay("running"));
    const first = s().runOverlay;
    s().setRunOverlay(overlay("running"));
    expect(s().runOverlay).toBe(first);
    s().setRunOverlay(overlay("success"));
    expect(s().runOverlay).not.toBe(first);
    expect(s().runOverlay?.states.n0_0).toBe(first?.states.n0_0);
    expect(s().runOverlay?.states.n0_1?.state).toBe("success");
    s().setRunOverlay({ ...overlay("success"), runId: "r2" });
    expect(s().runOverlay?.states.n0_0).not.toBe(first?.states.n0_0);
    s().setRunOverlay(null);
    expect(s().runOverlay).toBeNull();
  });
});

describe("large documents", () => {
  afterEach(() => vi.useRealTimers());

  it("live check delay grows with the document, capped", () => {
    expect(liveCheckDelay(600, 10)).toBe(600);
    expect(liveCheckDelay(600, 200)).toBe(600);
    expect(liveCheckDelay(600, 500)).toBe(900);
    expect(liveCheckDelay(600, 1000)).toBe(1400);
    expect(liveCheckDelay(600, 50_000)).toBe(2000);
    expect(liveCheckDelay(5000, 50_000)).toBe(5000); // never shorter than asked
  });

  it("a drag of one node in a 1000-node document is one undo step", () => {
    vi.useFakeTimers();
    vi.stubGlobal("requestAnimationFrame", () => 0);
    s().load(bigSpec(40, 25), false);
    expect(s().nodes).toHaveLength(1040);
    const x0 = s().nodes.find((n) => n.id === "n3_3")?.position.x;
    for (let i = 1; i <= 30; i++) {
      s().onNodesChange([{ type: "position", id: "n3_3", position: { x: (x0 ?? 0) + i * 5, y: 60 }, dragging: true }]);
    }
    vi.advanceTimersByTime(300);
    expect(useBuilder.temporal.getState().pastStates).toHaveLength(1);
    undo();
    expect(s().nodes.find((n) => n.id === "n3_3")?.position.x).toBe(x0);
    vi.unstubAllGlobals();
  });

  it("history comparison of a 1000-node document stays cheap", () => {
    s().load(bigSpec(40, 25), false);
    const start = performance.now();
    for (let i = 0; i < 50; i++) s().moveNodes(["n1_1"], 1, 0);
    // 50 updates with history snapshots + equality: generous ceiling (JSON took ~10x longer).
    expect(performance.now() - start).toBeLessThan(2_000);
  });
});
