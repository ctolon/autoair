import { describe, expect, it } from "vitest";

import type { DagSpec, TaskInstanceInfo } from "../../api/gen/types.gen";
import { aggregateState, buildRunOverlay, effectiveTaskIds, isRunActive, latestLogTry, nodeTaskId } from "./runOverlay";

type SpecNode = NonNullable<DagSpec["nodes"]>[number];

const op = (id: string, task_id: string, parent_group?: string): SpecNode => ({
  id,
  kind: "operator",
  operator: "airflow.providers.standard.operators.empty.EmptyOperator",
  task_id,
  ...(parent_group ? { parent_group } : {}),
});
const group = (id: string, group_id: string, parent_group?: string, prefix = true): SpecNode => ({
  id,
  kind: "task_group",
  group_id,
  prefix_group_id: prefix,
  ...(parent_group ? { parent_group } : {}),
});

const nodes: SpecNode[] = [
  group("g1", "outer"),
  group("g2", "inner", "g1"),
  group("g3", "flat", "g1", false),
  op("a", "extract"),
  op("b", "load", "g2"),
  op("c", "clean", "g3"),
  { id: "n", kind: "note", text: "hi" } as SpecNode,
];

describe("effectiveTaskIds", () => {
  it("prefixes nested group ids and honours prefix_group_id=false", () => {
    const ids = effectiveTaskIds(nodes);
    expect(ids.get("extract")).toBe("a");
    expect(ids.get("outer.inner.load")).toBe("b");
    expect(ids.get("outer.clean")).toBe("c");
    expect(ids.size).toBe(3);
    expect(nodeTaskId(nodes, "b")).toBe("outer.inner.load");
  });
});

describe("aggregateState", () => {
  const ti = (state: string | null, map_index = 0): TaskInstanceInfo => ({ task_id: "t", state, map_index });
  it("failed wins, then running, else success", () => {
    expect(aggregateState([ti("success"), ti("failed"), ti("running")])).toBe("failed");
    expect(aggregateState([ti("success"), ti("running")])).toBe("running");
    expect(aggregateState([ti("success"), ti("skipped")])).toBe("success");
    expect(aggregateState([ti("skipped")])).toBe("skipped");
    expect(aggregateState([ti(null)])).toBeNull();
  });
  it("keeps the exact state of a single instance", () => {
    expect(aggregateState([ti("upstream_failed")])).toBe("upstream_failed");
    expect(aggregateState([ti("up_for_retry")])).toBe("up_for_retry");
    expect(aggregateState([ti("success"), ti("up_for_retry")])).toBe("up_for_retry");
    expect(aggregateState([ti("success"), ti("upstream_failed")])).toBe("upstream_failed");
  });
});

describe("buildRunOverlay", () => {
  it("maps task ids to nodes and aggregates mapped instances", () => {
    const overlay = buildRunOverlay(nodes, "d", "r1", {
      run: { dag_run_id: "r1", state: "running" },
      task_instances: [
        { task_id: "extract", state: "success", try_number: 1, map_index: -1 },
        { task_id: "outer.inner.load", state: "success", try_number: 1, map_index: 0 },
        { task_id: "outer.inner.load", state: "running", try_number: 2, map_index: 1 },
        { task_id: "outer.clean", state: "upstream_failed", map_index: -1 },
        { task_id: "unknown", state: "failed" },
      ],
    });
    expect(overlay.dagId).toBe("d");
    expect(overlay.runId).toBe("r1");
    expect(overlay.states.a).toEqual({ state: "success", tryNumber: 1 });
    expect(overlay.states.b).toEqual({ state: "running", tryNumber: 2, mapped: 2 });
    expect(overlay.states.c?.state).toBe("upstream_failed");
    expect(Object.keys(overlay.states)).toHaveLength(3);
  });

  it("detects active runs", () => {
    expect(isRunActive({ run: { dag_run_id: "x", state: "success" }, task_instances: [] })).toBe(false);
    expect(isRunActive({ run: { dag_run_id: "x", state: "running" }, task_instances: [] })).toBe(true);
    expect(isRunActive(undefined)).toBe(false);
  });
});

describe("latestLogTry", () => {
  it("steps back from the upcoming try of an up_for_retry instance on Airflow 3.4", () => {
    expect(latestLogTry({ state: "up_for_retry", try_number: 2 }, true)).toBe(1);
    expect(latestLogTry({ state: "failed", try_number: 2 }, true)).toBe(2);
  });
  it("keeps try_number before Airflow 3.4 and for missing instances", () => {
    expect(latestLogTry({ state: "up_for_retry", try_number: 2 }, false)).toBe(2);
    expect(latestLogTry(undefined, true)).toBe(1);
    expect(latestLogTry({ state: "up_for_retry", try_number: 1 }, true)).toBe(1);
  });
});
