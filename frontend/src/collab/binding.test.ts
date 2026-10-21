import { afterEach, beforeEach, describe, expect, it } from "vitest";
import * as Y from "yjs";

import type { DagSpec } from "../api/gen/types.gen";
import { useBuilder } from "../store/builder";

import { type Binding, bind, maps, readSaved } from "./binding";

const spec: DagSpec = {
  spec_version: 3,
  dag_id: "shared",
  output_mode: "python",
  nodes: [
    { kind: "operator", id: "a", task_id: "extract", operator: "m.Op", position: { x: 0, y: 0 } },
    { kind: "operator", id: "b", task_id: "load", operator: "m.Op", position: { x: 300, y: 0 } },
  ],
  edges: [{ id: "e1", source: "a", target: "b" }],
};

const s = () => useBuilder.getState();

/** Two docs wired together like two browser tabs behind the relay server. */
function pair(): [Y.Doc, Y.Doc] {
  const a = new Y.Doc();
  const b = new Y.Doc();
  a.on("update", (u: Uint8Array, origin: unknown) => origin !== "remote" && Y.applyUpdate(b, u, "remote"));
  b.on("update", (u: Uint8Array, origin: unknown) => origin !== "remote" && Y.applyUpdate(a, u, "remote"));
  return [a, b];
}

const nodeOf = (doc: Y.Doc, id: string) => {
  const json = maps(doc).nodes.get(id);
  return json ? (JSON.parse(json) as { data: { spec: { task_id?: string } }; position: { x: number } }) : undefined;
};

describe("collaboration binding", () => {
  let mine: Y.Doc;
  let theirs: Y.Doc;
  let binding: Binding;

  beforeEach(() => {
    s().load(spec, true, { etag: "v1" });
    [mine, theirs] = pair();
    binding = bind(mine);
  });
  afterEach(() => binding.destroy());

  it("seeds an empty room from the local document", () => {
    expect([...maps(theirs).nodes.keys()].sort()).toEqual(["a", "b"]);
    expect(maps(theirs).edges.has("e1")).toBe(true);
    expect(JSON.parse(maps(theirs).dag.get("settings") ?? "{}")).toMatchObject({ dag_id: "shared" });
  });

  it("joins a non-empty room by adopting its state", () => {
    binding.destroy();
    s().reset("other");
    binding = bind(mine);
    expect(
      s()
        .nodes.map((n) => n.id)
        .sort(),
    ).toEqual(["a", "b"]);
    expect(s().dag.dag_id).toBe("shared");
  });

  it("sends local edits and applies remote ones without touching local selection", () => {
    s().updateSpec<"operator">("a", { task_id: "extract_v2" });
    expect(nodeOf(theirs, "a")?.data.spec.task_id).toBe("extract_v2");

    s().setSelection(["a"]);
    const remote = JSON.parse(maps(theirs).nodes.get("b") ?? "{}") as { position: { x: number } };
    remote.position.x = 999;
    maps(theirs).nodes.set("b", JSON.stringify(remote));
    expect(s().nodes.find((n) => n.id === "b")?.position.x).toBe(999);
    expect(s().nodes.find((n) => n.id === "a")?.selected).toBe(true);
    expect(s().dirty).toBe(true);

    maps(theirs).edges.delete("e1");
    expect(s().edges).toHaveLength(0);
  });

  it("undo reverts only this tab's changes", () => {
    s().updateSpec<"operator">("a", { task_id: "mine" });
    binding.undoManager.stopCapturing();
    const remote = JSON.parse(maps(theirs).nodes.get("b") ?? "{}") as { data: { spec: { task_id: string } } };
    remote.data.spec.task_id = "theirs";
    maps(theirs).nodes.set("b", JSON.stringify(remote));

    binding.undoManager.undo();
    expect(nodeOf(theirs, "a")?.data.spec.task_id).toBe("extract");
    expect(nodeOf(theirs, "b")?.data.spec.task_id).toBe("theirs");
    const local = s().nodes.find((n) => n.id === "a")?.data.spec;
    expect(local && "task_id" in local ? local.task_id : null).toBe("extract");
  });

  it("shares deploys so nobody gets a stale ETag", () => {
    s().updateSpec<"operator">("a", { task_id: "x" });
    s().markSaved("shared", { etag: "v2" });
    expect(readSaved(theirs)).toMatchObject({ dagId: "shared", etag: "v2" });

    maps(theirs).saved.set("info", JSON.stringify({ dagId: "shared", etag: "v3", pendingPrUrl: null }));
    expect(s().baseEtag).toBe("v3");
    expect(s().dirty).toBe(false);
  });
  it("shares raw code as text and merges concurrent edits", () => {
    binding.destroy();
    s().reset("raw_shared");
    useBuilder.setState((st) => ({ dag: { ...st.dag, raw_code: "a = 1\nb = 2\n" } }));
    [mine, theirs] = pair();
    binding = bind(mine);
    expect(maps(theirs).raw.toString()).toBe("a = 1\nb = 2\n");
    expect(maps(theirs).dag.get("settings")).not.toContain("raw_code");
    // Someone else edits line 2 while this tab edits line 1: both survive.
    maps(theirs).raw.insert(maps(theirs).raw.length - 1, "0");
    expect(s().dag.raw_code).toBe("a = 1\nb = 20\n");
    s().setDag({ raw_code: "a = 10\nb = 20\n" });
    expect(maps(theirs).raw.toString()).toBe("a = 10\nb = 20\n");
    // Back to a visual DAG: the text is cleared.
    s().setDag({ raw_code: null });
    expect(maps(theirs).dag.has("raw")).toBe(false);
    expect(maps(theirs).raw.length).toBe(0);
  });
});
