import { type XComRef, isXComRef } from "../../lib/types";

/** All XCom references (`{$type: "xcom"}`) found anywhere inside a value. */
function collectXComRefs(value: unknown, out: XComRef[] = []): XComRef[] {
  if (isXComRef(value)) {
    out.push(value);
    return out;
  }
  if (Array.isArray(value)) for (const v of value) collectXComRefs(v, out);
  else if (value && typeof value === "object") for (const v of Object.values(value)) collectXComRefs(v, out);
  return out;
}

/** Key of the runner's `mock_xcoms` for a ref: `<node_id>` or `<node_id>:<key>`. */
function mockKey(ref: Pick<XComRef, "node" | "key">): string {
  return ref.key && ref.key !== "return_value" ? `${ref.node}:${ref.key}` : ref.node;
}

/** Distinct mock keys needed by a task spec. */
export function mockKeys(spec: unknown): string[] {
  return [...new Set(collectXComRefs(spec).map(mockKey))];
}
