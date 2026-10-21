/**
 * DAG params (`airflow.sdk.Param`) helpers: normalisation of the spec's loose
 * `params` map, conversion to the JSON Schema Airflow renders its trigger form from,
 * and typed default-value coercion for the params editor.
 */
import type { DagParam } from "../api/gen/types.gen";
import type { JsonSchema, UiSchema } from "./formSchema";

export type ParamType = "string" | "number" | "integer" | "boolean" | "array" | "object" | "null";

export const PARAM_TYPES: ParamType[] = ["string", "number", "integer", "boolean", "array", "object", "null"];

/** Keys of `DagParam` (snake_case, as stored in the spec). */
const DAG_PARAM_KEYS = [
  "default",
  "type",
  "title",
  "description",
  "description_md",
  "section",
  "format",
  "enum",
  "values_display",
  "examples",
  "minimum",
  "maximum",
  "min_length",
  "max_length",
  "pattern",
  "items",
  "const",
] as const satisfies readonly (keyof DagParam)[];

/** String formats Airflow's trigger form renders with a dedicated widget. */
export const PARAM_FORMATS = ["date", "date-time", "time", "multiline", "email", "uri", "hostname", "ipv4", "ipv6"];

/** Valid param name (usable as `{{ params.<name> }}`). */
export const PARAM_NAME = /^[A-Za-z_][A-Za-z0-9_]*$/;

const isPlainObject = (v: unknown): v is Record<string, unknown> =>
  typeof v === "object" && v !== null && !Array.isArray(v);

/** True when `v` is shaped like a DagParam (only known keys) rather than a bare default value. */
export function looksLikeDagParam(v: unknown): v is DagParam {
  if (!isPlainObject(v)) return false;
  const known = new Set<string>(DAG_PARAM_KEYS);
  return Object.keys(v).every((k) => known.has(k));
}

/** A bare value in `params` is a param with only a default (`{"limit": 10}`). */
export function normalizeParam(v: unknown): DagParam {
  return looksLikeDagParam(v) ? { ...v } : { default: v };
}

export function normalizeParams(params: Record<string, unknown> | null | undefined): Record<string, DagParam> {
  const out: Record<string, DagParam> = {};
  for (const [k, v] of Object.entries(params ?? {})) out[k] = normalizeParam(v);
  return out;
}

/** JSON type of a value (integers are "integer"). */
function jsonTypeOf(v: unknown): ParamType | null {
  if (v === undefined) return null;
  if (v === null) return "null";
  if (Array.isArray(v)) return "array";
  if (typeof v === "number") return Number.isInteger(v) ? "integer" : "number";
  if (typeof v === "string") return "string";
  if (typeof v === "boolean") return "boolean";
  if (typeof v === "object") return "object";
  return null;
}

/** Declared type(s) of a param; inferred from the default when not declared. */
export function paramTypes(p: DagParam): ParamType[] {
  if (Array.isArray(p.type)) return [...p.type];
  if (p.type) return [p.type];
  const inferred = jsonTypeOf(p.default);
  return inferred ? [inferred] : [];
}

/** The type used to pick an input widget: the single non-null type, else null (free JSON). */
export function primaryType(p: DagParam): ParamType | null {
  const types = paramTypes(p).filter((t) => t !== "null");
  if (types.length === 1) return types[0] ?? null;
  if (types.length === 0 && paramTypes(p).includes("null")) return "null";
  return null;
}

export function defaultForType(type: ParamType): unknown {
  switch (type) {
    case "string":
      return "";
    case "number":
    case "integer":
      return 0;
    case "boolean":
      return false;
    case "array":
      return [];
    case "object":
      return {};
    case "null":
      return null;
  }
}

type Coerced = { ok: true; value: unknown } | { ok: false; error: string };

/** Parses text typed into a default/const input according to the param type. */
export function coerceInput(raw: string, type: ParamType | null): Coerced {
  switch (type) {
    case "string":
      return { ok: true, value: raw };
    case "integer": {
      const n = Number(raw);
      return raw.trim() !== "" && Number.isInteger(n) ? { ok: true, value: n } : { ok: false, error: "integer" };
    }
    case "number": {
      const n = Number(raw);
      return raw.trim() !== "" && Number.isFinite(n) ? { ok: true, value: n } : { ok: false, error: "number" };
    }
    case "boolean":
      if (raw === "true") return { ok: true, value: true };
      if (raw === "false") return { ok: true, value: false };
      return { ok: false, error: "boolean" };
    case "null":
      return raw.trim() === "" || raw.trim() === "null" ? { ok: true, value: null } : { ok: false, error: "null" };
    default: {
      try {
        const v: unknown = JSON.parse(raw);
        if (type === "array" && !Array.isArray(v)) return { ok: false, error: "array" };
        if (type === "object" && !isPlainObject(v)) return { ok: false, error: "object" };
        return { ok: true, value: v };
      } catch {
        return { ok: false, error: "json" };
      }
    }
  }
}

/** True when `value` matches one of the param's declared types (ints satisfy "number"). */
export function matchesTypes(value: unknown, types: ParamType[]): boolean {
  if (!types.length || value === undefined) return true;
  const t = jsonTypeOf(value);
  if (!t) return false;
  return types.includes(t) || (t === "integer" && types.includes("number"));
}

/** Drops unset keys (null/undefined/empty strings, lists and maps) — keeps `default`/`const` as given. */
export function compactParam(p: DagParam): DagParam {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(p)) {
    if (k === "default" || k === "const") {
      if (v !== undefined) out[k] = v;
      continue;
    }
    if (v === undefined || v === null || v === "") continue;
    if (Array.isArray(v) && v.length === 0) continue;
    if (isPlainObject(v) && Object.keys(v).length === 0) continue;
    out[k] = v;
  }
  return out as DagParam;
}

/**
 * JSON Schema of a param as Airflow stores it (`Param(**kwargs).schema`): JSON Schema
 * keywords plus Airflow UI keys (`section`, `values_display`, `description_md`).
 */
export function paramToJsonSchema(p: DagParam): JsonSchema {
  const s: JsonSchema = {};
  if (p.type) s.type = Array.isArray(p.type) ? [...p.type] : p.type;
  if (p.default !== undefined) s.default = p.default;
  if (p.title) s.title = p.title;
  if (p.description) s.description = p.description;
  if (p.description_md) s.description_md = p.description_md;
  if (p.section) s.section = p.section;
  if (p.format) s.format = p.format;
  if (p.enum?.length) s.enum = [...p.enum];
  if (p.values_display && Object.keys(p.values_display).length) s.values_display = { ...p.values_display };
  if (p.examples?.length) s.examples = [...p.examples];
  if (p.minimum != null) s.minimum = p.minimum;
  if (p.maximum != null) s.maximum = p.maximum;
  if (p.min_length != null) s.minLength = p.min_length;
  if (p.max_length != null) s.maxLength = p.max_length;
  if (p.pattern) s.pattern = p.pattern;
  if (p.items) s.items = p.items as JsonSchema;
  if (p.const !== undefined) s.const = p.const;
  return s;
}

type TriggerFormSection = {
  /** Section title (null: params without a section, rendered first). */
  name: string | null;
  names: string[];
  schema: JsonSchema;
  uiSchema: UiSchema;
  formData: Record<string, unknown>;
};

const STRING_WIDGET_FORMATS = new Set(["date", "date-time", "time", "email", "uri"]);

/** Form field schema + ui hints of one param, as the trigger form renders it. */
function fieldOf(name: string, p: DagParam): { schema: JsonSchema; ui: Record<string, unknown>; required: boolean } {
  const types = paramTypes(p);
  const nonNull = types.filter((t) => t !== "null");
  const required = types.length > 0 && !types.includes("null");
  const s: JsonSchema = {};
  const ui: Record<string, unknown> = {};
  s.title = p.title || name;
  const description = p.description_md || p.description;
  if (description) s.description = description;

  let type: ParamType | null = nonNull.length === 1 ? (nonNull[0] ?? null) : null;
  if (!types.length) type = jsonTypeOf(p.default) ?? "string";
  if (type === "null") type = null;

  if (type === null) {
    // Multi-type / untyped values: edited as JSON (Airflow shows a JSON editor too).
    ui["ui:field"] = "json";
  } else {
    s.type = type;
    if (type === "object" && !p.items) ui["ui:field"] = "json";
    if (type === "array") {
      const items = (p.items as JsonSchema | null | undefined) ?? { type: "string" };
      s.items = items;
      if (items.type !== "string" && items.type !== "number" && items.type !== "integer") ui["ui:field"] = "json";
    }
  }
  if (p.enum?.length) {
    s.enum = [...p.enum];
    if (p.values_display && Object.keys(p.values_display).length) {
      ui["ui:enumNames"] = p.enum.map((v) => p.values_display?.[String(v)] ?? String(v));
    }
  }
  if (type === "string") {
    if (p.format === "multiline") ui["ui:widget"] = "textarea";
    else if (p.format && STRING_WIDGET_FORMATS.has(p.format)) s.format = p.format;
    if (p.min_length != null) s.minLength = p.min_length;
    if (p.max_length != null) s.maxLength = p.max_length;
    if (p.pattern) s.pattern = p.pattern;
    if (p.examples?.length && !p.enum?.length) s.examples = p.examples.map(String);
  }
  if (type === "number" || type === "integer") {
    if (p.minimum != null) s.minimum = p.minimum;
    if (p.maximum != null) s.maximum = p.maximum;
  }
  if (p.const !== undefined) {
    s.const = p.const;
    ui["ui:readonly"] = true;
  }
  if (p.default !== undefined) s.default = p.default;
  return { schema: s, ui, required };
}

/** Airflow's trigger form: params grouped by `section` (unsectioned first), with defaults as form data. */
export function paramsToTriggerForm(params: Record<string, unknown> | null | undefined): {
  sections: TriggerFormSection[];
} {
  const order: (string | null)[] = [];
  const bySection = new Map<string | null, TriggerFormSection>();
  for (const [name, raw] of Object.entries(params ?? {})) {
    const p = normalizeParam(raw);
    const key = p.section || null;
    let sec = bySection.get(key);
    if (!sec) {
      sec = { name: key, names: [], schema: { type: "object", properties: {} }, uiSchema: {}, formData: {} };
      bySection.set(key, sec);
      order.push(key);
    }
    const { schema, ui, required } = fieldOf(name, p);
    sec.names.push(name);
    (sec.schema.properties as Record<string, JsonSchema>)[name] = schema;
    if (Object.keys(ui).length) sec.uiSchema[name] = ui;
    if (required) sec.schema.required = [...(sec.schema.required ?? []), name];
    if (p.default !== undefined) sec.formData[name] = p.default;
  }
  // Unsectioned params first, then sections in declaration order.
  order.sort((a, b) => (a === null ? -1 : b === null ? 1 : 0));
  return { sections: order.map((k) => bySection.get(k) as TriggerFormSection) };
}

/** Renames a key in an object, preserving entry order. */
export function renameKey<T>(obj: Record<string, T>, from: string, to: string): Record<string, T> {
  const out: Record<string, T> = {};
  for (const [k, v] of Object.entries(obj)) out[k === from ? to : k] = v;
  return out;
}

/** Moves the entry `key` by `delta` positions, preserving the other entries' order. */
export function moveKey<T>(obj: Record<string, T>, key: string, delta: number): Record<string, T> {
  const entries = Object.entries(obj);
  const i = entries.findIndex(([k]) => k === key);
  const j = i + delta;
  if (i < 0 || j < 0 || j >= entries.length) return obj;
  const [item] = entries.splice(i, 1);
  if (item) entries.splice(j, 0, item);
  return Object.fromEntries(entries);
}
