import type { Conflict } from "../../api/gen/types.gen";

export type Side = "ours" | "theirs";

type PathSegment = { type: "node"; id: string } | { type: "edge"; key: string } | { type: "key"; key: string };

const LIST_SEGMENT = /^(nodes|edges)\[([^\]]*)\](?:\.(.*))?$/;

/** Parses merge conflict paths such as `schedule`, `nodes[extract].params.sql`, `edges[a->b]`. */
export function parseConflictPath(path: string): PathSegment[] {
  if (!path) return [];
  const m = LIST_SEGMENT.exec(path);
  const segments: PathSegment[] = [];
  let rest: string | undefined = path;
  if (m) {
    const [, list, key, tail] = m;
    segments.push(list === "nodes" ? { type: "node", id: key ?? "" } : { type: "edge", key: key ?? "" });
    rest = tail;
  }
  if (rest) for (const key of rest.split(".")) segments.push({ type: "key", key });
  return segments;
}

type Json = Record<string, unknown>;
const isObject = (v: unknown): v is Json => typeof v === "object" && v !== null && !Array.isArray(v);
const edgeKey = (e: Json) => `${String(e.source)}->${String(e.target)}`;

const MISSING: unique symbol = Symbol("missing");
type MaybeValue = unknown | typeof MISSING;

/** The value a side holds for a conflict (MISSING when that side deleted it). */
function sideValue(conflict: Conflict, side: Side): MaybeValue {
  const present = side === "ours" ? conflict.ours_present !== false : conflict.theirs_present !== false;
  if (!present) return MISSING;
  return side === "ours" ? conflict.ours : conflict.theirs;
}

function setIn(target: Json, segments: PathSegment[], value: MaybeValue): void {
  const [head, ...tail] = segments;
  if (!head) return;
  if (head.type === "key") {
    if (!tail.length) {
      if (value === MISSING) Reflect.deleteProperty(target, head.key);
      else target[head.key] = structuredClone(value);
      return;
    }
    let child = target[head.key];
    if (!isObject(child)) {
      if (value === MISSING) return;
      child = {};
      target[head.key] = child;
    }
    setIn(child as Json, tail, value);
    return;
  }
  const listName = head.type === "node" ? "nodes" : "edges";
  const list: unknown[] = Array.isArray(target[listName]) ? (target[listName] as unknown[]) : [];
  target[listName] = list;
  const matches = (item: unknown) =>
    isObject(item) && (head.type === "node" ? item.id === head.id : edgeKey(item) === head.key);
  const idx = list.findIndex(matches);
  if (!tail.length) {
    if (value === MISSING) {
      if (idx >= 0) list.splice(idx, 1);
    } else if (idx >= 0) list[idx] = structuredClone(value);
    else list.push(structuredClone(value));
    return;
  }
  const item = list[idx];
  if (!isObject(item)) return;
  setIn(item, tail, value);
}

/**
 * Builds the final document from the merge `draft` (ours wins on conflicts) by applying
 * the user's choice for each conflict. Conflicts without a choice keep the draft value.
 */
export function applyResolutions(draft: Json, conflicts: readonly Conflict[], choices: Record<number, Side>): Json {
  const out = structuredClone(draft);
  conflicts.forEach((c, i) => {
    const choice = choices[i];
    const segments = parseConflictPath(c.path);
    if (!choice || !segments.length) return;
    setIn(out, segments, sideValue(c, choice));
  });
  return out;
}

/** True when every resolvable conflict has a choice. Whole-document conflicts (`""`) cannot be chosen. */
export function allResolved(conflicts: readonly Conflict[], choices: Record<number, Side>): boolean {
  return conflicts.every((c, i) => !c.path || choices[i] !== undefined);
}
