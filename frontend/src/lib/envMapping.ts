/**
 * Per-environment connection / Variable mapping (`environments.<env>.connections|variables`):
 * the logical ids a DAG uses, so the environments editor can offer them, and immutable
 * helpers for the mapping rows. The backend substitutes them (autoair.spec.env_mapping).
 */

export type Mapping = Record<string, string>;
type References = { connections: string[]; variables: string[] };

const CONN_JINJA = /\bconn\s*(?:\.\s*([A-Za-z_][\w]*)|\[\s*['"]([^'"]+)['"]\s*\])/g;
const VAR_JINJA = /\bvar\s*\.\s*(?:value|json)\s*(?:\.\s*([A-Za-z_][\w]*)|\[\s*['"]([^'"]+)['"]\s*\])/g;
const VAR_GET = /Variable\.get\(\s*(?:key\s*=\s*)?['"]([^'"]+)['"]/g;
const CONN_CALL = /(?:get_connection|get_hook)\(\s*['"]([^'"]+)['"]/g;
const CONN_KWARG = /\b\w*conn_id\s*=\s*['"]([^'"]+)['"]/g;

function collect(text: string, re: RegExp, into: Set<string>) {
  for (const m of text.matchAll(re)) {
    const id = m[1] ?? m[2];
    if (id) into.add(id);
  }
}

/** Connection ids and Variable keys referenced literally anywhere in `spec` (sorted). */
export function logicalReferences(spec: unknown): References {
  const conns = new Set<string>();
  const vars = new Set<string>();
  const visit = (value: unknown, key: string, depth: number) => {
    if (depth > 40) return;
    if (typeof value === "string") {
      if ((key === "conn_id" || key.endsWith("_conn_id")) && value && !value.includes("{{")) conns.add(value);
      if (value.includes("{{") || value.includes("{%")) {
        collect(value, CONN_JINJA, conns);
        collect(value, VAR_JINJA, vars);
      }
      if (key === "source" || key === "code" || key === "until_code") {
        collect(value, VAR_GET, vars);
        collect(value, CONN_CALL, conns);
        collect(value, CONN_KWARG, conns);
      }
      return;
    }
    if (Array.isArray(value)) {
      for (const v of value) visit(v, key, depth + 1);
      return;
    }
    if (value && typeof value === "object") {
      for (const [k, v] of Object.entries(value)) {
        if (k === "environments" && depth === 0) continue;
        visit(v, k, depth + 1);
      }
    }
  };
  visit(spec, "", 0);
  return { connections: [...conns].sort(), variables: [...vars].sort() };
}

/** `mapping` with `logical` renamed/re-targeted; an empty actual id removes nothing (row kept). */
export function setEntry(mapping: Mapping, oldLogical: string | null, logical: string, actual: string): Mapping {
  const out: Mapping = {};
  for (const [k, v] of Object.entries(mapping)) {
    if (k === oldLogical) out[logical] = actual;
    else if (k !== logical) out[k] = v;
  }
  if (oldLogical === null || !(oldLogical in mapping)) out[logical] = actual;
  return out;
}

export function removeEntry(mapping: Mapping, logical: string): Mapping {
  return Object.fromEntries(Object.entries(mapping).filter(([k]) => k !== logical));
}

/** The spec's id syntax (`Key`): connection ids and Variable keys of a mapping. */
export const MAPPING_ID = /^[A-Za-z0-9_.-]{1,250}$/;

/** Entries the backend accepts (both ids valid); others stay local drafts. */
export function validEntries(mapping: Mapping): Mapping {
  return Object.fromEntries(Object.entries(mapping).filter(([k, v]) => MAPPING_ID.test(k) && MAPPING_ID.test(v)));
}
