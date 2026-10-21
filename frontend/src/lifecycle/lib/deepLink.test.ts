import { describe, expect, it } from "vitest";

import { parseDeepLink, withDeepLink } from "./deepLink";
import { mockKeys } from "./xcomRefs";

describe("parseDeepLink", () => {
  it("reads ?dag= and #dag=", () => {
    expect(parseDeepLink("?dag=my_dag", "")).toBe("my_dag");
    expect(parseDeepLink("", "#dag=etl.daily&x=1")).toBe("etl.daily");
    expect(parseDeepLink("?foo=1", "#dag=b")).toBe("b");
    expect(parseDeepLink("?dag=a", "#dag=b")).toBe("a");
  });

  it("rejects missing or invalid ids", () => {
    expect(parseDeepLink("", "")).toBeNull();
    expect(parseDeepLink("?dag=", "")).toBeNull();
    expect(parseDeepLink("?dag=bad%20id", "")).toBeNull();
    expect(parseDeepLink("?dag=..%2Fetc", "")).toBeNull();
  });
});

describe("withDeepLink", () => {
  it("sets and removes the query parameter", () => {
    expect(withDeepLink("http://h/ui/?x=1", "d1", "query")).toBe("http://h/ui/?x=1&dag=d1");
    expect(withDeepLink("http://h/ui/?dag=d1", null, "query")).toBe("http://h/ui/");
  });
  it("sets and removes the hash parameter", () => {
    expect(withDeepLink("http://h/plugin?tab=1", "d2", "hash")).toBe("http://h/plugin?tab=1#dag=d2");
    expect(withDeepLink("http://h/plugin#dag=d2", null, "hash")).toBe("http://h/plugin");
  });
});

describe("mockKeys", () => {
  it("collects XCom refs as runner mock keys", () => {
    const spec = {
      params: { a: { $type: "xcom", node: "n1" }, b: [{ $type: "xcom", node: "n2", key: "rows" }] },
      mapping: { expand: { x: { $type: "xcom", node: "n1", key: "return_value" } } },
    };
    expect(mockKeys(spec)).toEqual(["n1", "n2:rows"]);
  });
});

describe("keepPositions", async () => {
  const { keepPositions } = await import("./positions");
  it("copies positions by task id", () => {
    const prev = {
      dag_id: "d",
      nodes: [{ id: "x1", kind: "operator", operator: "o", task_id: "a", position: { x: 5, y: 7 } }],
    } as const;
    const next = {
      dag_id: "d",
      nodes: [
        { id: "y1", kind: "operator", operator: "o", task_id: "a" },
        { id: "y2", kind: "operator", operator: "o", task_id: "b" },
      ],
    } as const;
    const res = keepPositions(next as never, prev as never);
    expect(res.matched).toBe(1);
    expect(res.spec.nodes?.[0]?.position).toEqual({ x: 5, y: 7 });
    expect(res.spec.nodes?.[1]?.position).toBeUndefined();
  });
});
