import { describe, expect, it } from "vitest";

import { logicalReferences, removeEntry, setEntry, validEntries } from "./envMapping";

describe("logicalReferences", () => {
  it("finds conn ids, Jinja references and Python lookups but skips overlays", () => {
    const spec = {
      dag_id: "x",
      nodes: [
        { params: { postgres_conn_id: "warehouse", sql: "select '{{ var.value.schema }}'" } },
        { params: { bash_command: "curl {{ conn.api.host }} {{ conn['s3-raw'].login }} {{ var.json['cfg'] }}" } },
        {
          kind: "python_task",
          code: 'Variable.get("bucket")\nBaseHook.get_connection("slack")\nH(http_conn_id="web")',
        },
        { params: { conn_id: "{{ params.conn }}" } },
      ],
      environments: { prod: { connections: { warehouse: "prod-warehouse" } } },
    };
    expect(logicalReferences(spec)).toEqual({
      connections: ["api", "s3-raw", "slack", "warehouse", "web"],
      variables: ["bucket", "cfg", "schema"],
    });
  });

  it("tolerates odd input", () => {
    expect(logicalReferences(null)).toEqual({ connections: [], variables: [] });
    expect(logicalReferences("conn_id")).toEqual({ connections: [], variables: [] });
  });
});

describe("mapping rows", () => {
  it("renames, retargets and removes entries without mutating", () => {
    const m = { a: "prod-a", b: "prod-b" };
    expect(setEntry(m, "a", "a", "x")).toEqual({ a: "x", b: "prod-b" });
    expect(setEntry(m, "a", "c", "prod-a")).toEqual({ c: "prod-a", b: "prod-b" });
    expect(setEntry(m, null, "d", "")).toEqual({ a: "prod-a", b: "prod-b", d: "" });
    expect(removeEntry(m, "a")).toEqual({ b: "prod-b" });
    expect(m).toEqual({ a: "prod-a", b: "prod-b" });
    expect(validEntries({ a: "", "": "x", b: "y", "has space": "z", c: "bad/id" })).toEqual({ b: "y" });
  });
});
