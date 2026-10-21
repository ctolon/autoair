import type { BuilderEdge, BuilderNode } from "./types";
import { isTaskNode, nodeKey } from "./types";

/**
 * Airflow task id of a node, including task-group prefixes: a group with
 * `prefix_group_id` (default true) prefixes its children with its own (full) group id.
 */
export function effectiveTaskId(nodes: BuilderNode[], id: string): string {
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const childPrefix = (groupId: string | undefined, depth = 0): string => {
    const g = groupId ? byId.get(groupId) : undefined;
    if (!g || g.type !== "task_group" || depth > 50) return "";
    const parentPrefix = childPrefix(g.parentId, depth + 1);
    return g.data.spec.prefix_group_id === false ? parentPrefix : `${parentPrefix}${g.data.spec.group_id}.`;
  };
  const node = byId.get(id);
  if (!node) return id;
  if (node.type === "task_group") return `${childPrefix(node.parentId)}${node.data.spec.group_id}`;
  return `${childPrefix(node.parentId)}${nodeKey(node)}`;
}

/**
 * Keys of a dict literal returned by a Python function body (best effort, for
 * `multiple_outputs`): `return {"a": 1, 'b': x}` → ["a", "b"]. Also understands
 * `return dict(a=1, b=2)`. Returns [] when no literal return is found.
 */
export function detectReturnKeys(code: string): string[] {
  const keys = new Set<string>();
  const returnRe = /^\s*return\s+(.*)$/gm;
  let m: RegExpExecArray | null;
  while ((m = returnRe.exec(code))) {
    const start = m.index + m[0].indexOf("return") + "return".length;
    const rest = code.slice(start).trimStart();
    if (rest.startsWith("{")) {
      const body = balanced(rest, "{", "}");
      if (body === null) continue;
      for (const k of topLevelDictKeys(body)) keys.add(k);
    } else if (/^dict\s*\(/.test(rest)) {
      const body = balanced(rest.slice(rest.indexOf("(")), "(", ")");
      if (body === null) continue;
      for (const part of splitTopLevel(body)) {
        const km = /^\s*([A-Za-z_]\w*)\s*=/.exec(part);
        if (km?.[1]) keys.add(km[1]);
      }
    }
  }
  return [...keys];
}

/** Content between the leading `open` bracket and its matching `close` (strings respected). */
function balanced(s: string, open: string, close: string): string | null {
  if (s[0] !== open) return null;
  const pairs: Record<string, string> = { "(": ")", "[": "]", "{": "}" };
  const stack: string[] = [];
  let quote: string | null = null;
  for (let i = 0; i < s.length; i++) {
    const c = s[i] as string;
    if (quote) {
      if (c === "\\") i++;
      else if (c === quote) quote = null;
      continue;
    }
    if (c === '"' || c === "'") quote = c;
    else if (c in pairs) stack.push(pairs[c] as string);
    else if (c === ")" || c === "]" || c === "}") {
      if (stack.pop() !== c) return null;
      if (stack.length === 0) return c === close ? s.slice(1, i) : null;
    }
  }
  return null;
}

/** Splits on commas that are not nested in brackets or strings. */
function splitTopLevel(s: string): string[] {
  const out: string[] = [];
  let depth = 0;
  let quote: string | null = null;
  let cur = "";
  for (let i = 0; i < s.length; i++) {
    const c = s[i] as string;
    if (quote) {
      cur += c;
      if (c === "\\" && i + 1 < s.length) cur += s[++i];
      else if (c === quote) quote = null;
      continue;
    }
    if (c === '"' || c === "'") quote = c;
    if ("([{".includes(c)) depth++;
    if (")]}".includes(c)) depth--;
    if (c === "," && depth === 0) {
      out.push(cur);
      cur = "";
    } else cur += c;
  }
  if (cur.trim()) out.push(cur);
  return out;
}

function topLevelDictKeys(body: string): string[] {
  const keys: string[] = [];
  for (const part of splitTopLevel(body)) {
    const km = /^\s*(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)')\s*:/.exec(part);
    const k = km?.[1] ?? km?.[2];
    if (k !== undefined) keys.push(k);
  }
  return keys;
}

/** Task nodes that `id` directly feeds (outgoing edges, groups expanded to member tasks). */
export function downstreamTasks(nodes: BuilderNode[], edges: BuilderEdge[], id: string): BuilderNode[] {
  const out = new Set<string>();
  const members = (gid: string): BuilderNode[] => {
    const res: BuilderNode[] = [];
    for (const n of nodes) {
      let p = n.parentId;
      while (p) {
        if (p === gid) {
          res.push(n);
          break;
        }
        p = nodes.find((x) => x.id === p)?.parentId;
      }
    }
    return res;
  };
  for (const e of edges) {
    if (e.source !== id) continue;
    const target = nodes.find((n) => n.id === e.target);
    if (!target) continue;
    if (target.type === "task_group") {
      // Branching into a group targets its root tasks: members without upstream inside the group.
      const inner = members(target.id).filter(isTaskNode);
      const innerIds = new Set(inner.map((n) => n.id));
      for (const n of inner) if (!edges.some((x) => x.target === n.id && innerIds.has(x.source))) out.add(n.id);
    } else if (isTaskNode(target)) out.add(target.id);
  }
  return nodes.filter((n) => out.has(n.id));
}
