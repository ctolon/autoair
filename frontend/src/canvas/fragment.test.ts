import { describe, expect, it } from "vitest";

import type { BuilderEdge, BuilderNode, FragmentInput } from "../lib/types";
import { exportFragment, freshKey, parseFragment, prepareFragment } from "./fragment";

const op = (id: string, extra: Partial<BuilderNode> = {}, params: Record<string, unknown> = {}): BuilderNode =>
  ({
    id,
    type: "operator",
    position: { x: 10, y: 20 },
    data: { spec: { kind: "operator", task_id: id, operator: "x.y.BashOperator", params } },
    ...extra,
  }) as BuilderNode;
const py = (id: string, fn = id): BuilderNode => ({
  id,
  type: "python_task",
  position: { x: 0, y: 0 },
  data: { spec: { kind: "python_task", task_id: id, function_name: fn, code: "return 1" } },
});
const group = (id: string, pos = { x: 100, y: 100 }): BuilderNode => ({
  id,
  type: "task_group",
  position: pos,
  style: { width: 300, height: 200 },
  data: { spec: { kind: "task_group", group_id: id } },
});

const counter = () => {
  let i = 0;
  return () => `new${++i}`;
};

describe("freshKey", () => {
  it("keeps free keys and numbers taken ones", () => {
    expect(freshKey(new Set(), "load")).toBe("load");
    expect(freshKey(new Set(["load"]), "load")).toBe("load_1");
    expect(freshKey(new Set(["load", "load_1"]), "load_1")).toBe("load_2");
  });
});

describe("exportFragment", () => {
  it("includes members of selected groups, internal edges only, absolute positions for orphans", () => {
    const nodes = [group("g"), op("a", { parentId: "g" }), op("b", { parentId: "g" }), op("c")];
    const edges: BuilderEdge[] = [
      { id: "e1", source: "a", target: "b" },
      { id: "e2", source: "b", target: "c" },
    ];
    const frag = exportFragment(nodes, edges, ["g"]);
    expect(frag.autoair).toBe("fragment");
    expect(frag.nodes.map((n) => n.id).sort()).toEqual(["a", "b", "g"]);
    expect(frag.edges.map((e) => e.id)).toEqual(["e1"]);

    // Copying only a member: it leaves its group and gets absolute coordinates.
    const single = exportFragment(nodes, edges, ["a"]);
    expect(single.nodes[0]).toMatchObject({ parent_group: null, position: { x: 110, y: 120 } });
  });
});

describe("prepareFragment (paste)", () => {
  const existing = [op("extract"), py("transform", "transform")];

  it("re-generates ids and keys, remaps edges, groups and XCom refs", () => {
    const frag: FragmentInput = {
      nodes: [
        { kind: "task_group", id: "g", group_id: "grp", position: { x: 0, y: 0, width: 300, height: 200 } },
        {
          kind: "operator",
          id: "extract",
          task_id: "extract",
          operator: "x.y.BashOperator",
          parent_group: "g",
          position: { x: 20, y: 60 },
        },
        {
          kind: "python_task",
          id: "transform",
          task_id: "transform",
          function_name: "transform",
          code: "return x",
          parent_group: "g",
          arguments: { x: { $type: "xcom", node: "extract" }, outside: { $type: "xcom", node: "elsewhere" } },
          position: { x: 260, y: 60 },
        },
      ],
      edges: [{ id: "e", source: "extract", target: "transform", label: "data" }],
    };
    const out = prepareFragment(existing, frag, { newId: counter(), at: { x: 500, y: 400 } });
    const byOld = (old: string) => out.nodes.find((n) => n.id === out.idMap.get(old));

    expect([...out.idMap.values()]).toEqual(["new1", "new2", "new3"]);
    expect(out.nodes[0]?.id).toBe("new1"); // parents first
    expect(byOld("g")?.parentId).toBeUndefined();
    expect(byOld("g")?.position).toEqual({ x: 500, y: 400 });
    expect(byOld("extract")).toMatchObject({ parentId: "new1", position: { x: 20, y: 60 } });
    expect(byOld("extract")?.data.spec).toMatchObject({ task_id: "extract_1" });
    const t = byOld("transform")?.data.spec;
    expect(t).toMatchObject({
      task_id: "transform_1",
      function_name: "transform_1",
      arguments: { x: { $type: "xcom", node: "new2" }, outside: { $type: "xcom", node: "elsewhere" } },
    });
    expect(byOld("g")?.data.spec).toMatchObject({ group_id: "grp" });
    expect(out.edges).toEqual([{ id: "e_new2__new3", source: "new2", target: "new3", label: "data" }]);
  });

  it("offsets from the original place without a target and re-parents into a group", () => {
    const frag: FragmentInput = {
      nodes: [{ kind: "operator", id: "a", task_id: "a", operator: "m.Op", position: { x: 300, y: 300 } }],
    };
    expect(prepareFragment([], frag, { newId: counter() }).nodes[0]?.position).toEqual({ x: 340, y: 340 });
    const into = prepareFragment([group("host")], frag, { newId: counter(), at: { x: 150, y: 180 }, parentId: "host" });
    expect(into.nodes[0]).toMatchObject({ parentId: "host", position: { x: 50, y: 80 } });
  });

  it("lays out unpositioned template nodes in a row", () => {
    const frag: FragmentInput = {
      nodes: [
        { kind: "operator", id: "a", task_id: "a", operator: "m.Op" },
        { kind: "operator", id: "b", task_id: "b", operator: "m.Op" },
      ],
    };
    const out = prepareFragment([], frag, { newId: counter(), at: { x: 0, y: 0 } });
    expect(out.nodes.map((n) => n.position.x)).toEqual([0, 280]);
  });
});

describe("parseFragment", () => {
  it("accepts our fragments and DAG specs, rejects anything else", () => {
    const node = { kind: "note", id: "n", text: "hi" };
    expect(parseFragment(JSON.stringify({ autoair: "fragment", nodes: [node], edges: [] }))?.nodes).toHaveLength(1);
    expect(parseFragment(JSON.stringify({ dag_id: "d", nodes: [node] }))?.nodes).toHaveLength(1);
    expect(parseFragment(JSON.stringify({ nodes: [node] }))).toBeNull();
    expect(parseFragment("plain text")).toBeNull();
    expect(parseFragment(JSON.stringify({ autoair: "fragment", nodes: [{ kind: "bogus", id: "x" }] }))).toBeNull();
  });
});
