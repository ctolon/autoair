import type { DagSpec, Problem } from "../api/gen/types.gen";

type PydanticError = { loc?: (string | number)[]; msg?: string; type?: string };

/**
 * A 422 from the API (the document does not match the spec schema) → one problem per
 * Pydantic error, located like validation problems so the UI can jump to the field:
 * `["body","nodes",3,"python_task","retry_policy","rules",0,"exception"]` →
 * node_id = id of `spec.nodes[3]`, field = `retry_policy.rules.0.exception`.
 */
export function schemaProblems(err: unknown, spec: DagSpec): Problem[] | null {
  const detail = err && typeof err === "object" && "detail" in err ? (err as { detail: unknown }).detail : null;
  if (!Array.isArray(detail) || !detail.length) return null;
  return (detail as PydanticError[]).map((e) => {
    const loc = [...(e.loc ?? [])];
    if (loc[0] === "body") loc.shift();
    let nodeId: string | null = null;
    if (loc[0] === "nodes" && typeof loc[1] === "number") {
      const node = spec.nodes?.[loc[1]] as { id?: string; kind?: string } | undefined;
      nodeId = node?.id ?? null;
      loc.splice(0, 2);
      // Discriminated union: the tag (python_task, operator…) is part of the location.
      if (
        typeof loc[0] === "string" &&
        (loc[0] === node?.kind || (/^[a-z_]+$/.test(loc[0]) && loc.length > 1 && isKindTag(loc[0])))
      )
        loc.shift();
    }
    // Drop union branch markers such as "function-after[…]" / "tagged-union[…]".
    const path = loc.filter((p) => typeof p === "number" || !UNION_MARKER.test(p)).join(".");
    return {
      severity: "error" as const,
      code: "schema",
      message: e.msg ?? "invalid value",
      node_id: nodeId,
      field: path || null,
    };
  });
}

const KIND_TAGS = new Set(["operator", "python_task", "task_group", "note"]);
// Pydantic union/validator branch markers in error locations (not field names):
// "function-after[check()]", "tagged-union[…]", "str", "list[str]"…
const UNION_MARKER = /^(function-(after|before|wrap|plain)|tagged-union|union|chain|nullable|lax-or-strict)\[|\[.*\]$/;
function isKindTag(tag: string): boolean {
  return KIND_TAGS.has(tag);
}
