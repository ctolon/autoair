import { describe, expect, it } from "vitest";

import type { Conflict } from "../../api/gen/types.gen";
import { allResolved, applyResolutions, parseConflictPath } from "./conflicts";

describe("parseConflictPath", () => {
  it("parses top-level, node and edge paths", () => {
    expect(parseConflictPath("schedule")).toEqual([{ type: "key", key: "schedule" }]);
    expect(parseConflictPath("default_args.retries")).toEqual([
      { type: "key", key: "default_args" },
      { type: "key", key: "retries" },
    ]);
    expect(parseConflictPath("nodes[extract].params.sql")).toEqual([
      { type: "node", id: "extract" },
      { type: "key", key: "params" },
      { type: "key", key: "sql" },
    ]);
    expect(parseConflictPath("edges[a->b]")).toEqual([{ type: "edge", key: "a->b" }]);
    expect(parseConflictPath("")).toEqual([]);
  });
});

describe("applyResolutions", () => {
  const draft = {
    dag_id: "d",
    schedule: { type: "cron", cron: "0 1 * * *" },
    default_args: { retries: 1 },
    nodes: [
      { id: "extract", kind: "operator", task_id: "extract", params: { sql: "ours" } },
      { id: "load", kind: "operator", task_id: "load" },
    ],
    edges: [{ source: "extract", target: "load" }],
  };
  const conflicts: Conflict[] = [
    { path: "schedule", ours: { type: "cron", cron: "0 1 * * *" }, theirs: { type: "none" } },
    { path: "nodes[extract].params.sql", ours: "ours", theirs: "theirs" },
    { path: "default_args.retries", ours: 1, theirs: null, theirs_present: false },
    {
      path: "nodes[load]",
      ours: { id: "load", kind: "operator", task_id: "load" },
      theirs: null,
      theirs_present: false,
    },
    { path: "edges[extract->load]", ours: { source: "extract", target: "load" }, theirs: null, theirs_present: false },
    { path: "nodes[new]", ours: null, ours_present: false, theirs: { id: "new", kind: "note" } },
  ];

  it("applies theirs choices by path, deleting absent values", () => {
    const out = applyResolutions(draft, conflicts, {
      0: "theirs",
      1: "theirs",
      2: "theirs",
      3: "theirs",
      4: "theirs",
      5: "theirs",
    });
    expect(out.schedule).toEqual({ type: "none" });
    expect(out.default_args).toEqual({});
    const nodes = out.nodes as { id: string; params?: { sql: string } }[];
    expect(nodes.map((n) => n.id)).toEqual(["extract", "new"]);
    expect(nodes[0]?.params?.sql).toBe("theirs");
    expect(out.edges).toEqual([]);
    // The draft itself is untouched.
    expect(draft.nodes).toHaveLength(2);
  });

  it("keeps ours and leaves unresolved conflicts as drafted", () => {
    const out = applyResolutions(draft, conflicts, { 1: "ours", 5: "ours" });
    expect((out.nodes as { id: string }[]).map((n) => n.id)).toEqual(["extract", "load"]);
    expect(out.schedule).toEqual(draft.schedule);
  });

  it("reports whether all conflicts are resolved", () => {
    expect(allResolved(conflicts, { 0: "ours" })).toBe(false);
    expect(allResolved([{ path: "" }], {})).toBe(true);
  });
});
