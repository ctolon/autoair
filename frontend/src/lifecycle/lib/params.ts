import type { DagParam } from "../../api/gen/types.gen";
import type { JsonSchema } from "../../lib/formSchema";

const PARAM_KEYS = new Set<string>([
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
]);
/** Formats RJSF / ajv-formats understand; others (e.g. "multiline") are dropped. */
const FORMATS = new Set(["date", "date-time", "time", "email", "uri", "duration"]);

/** A DAG param entry as a `DagParam`; any other value (partial or hand-edited input) is taken as its default. */
export function asDagParam(value: unknown): DagParam {
  if (typeof value === "object" && value !== null && !Array.isArray(value)) {
    const keys = Object.keys(value);
    if (keys.length && keys.every((k) => PARAM_KEYS.has(k))) return value as DagParam;
  }
  return { default: value };
}

function inferType(value: unknown): JsonSchema["type"] {
  if (typeof value === "string") return "string";
  if (typeof value === "boolean") return "boolean";
  if (typeof value === "number") return Number.isInteger(value) ? "integer" : "number";
  if (Array.isArray(value)) return "array";
  if (value && typeof value === "object") return "object";
  return undefined;
}

/** JSON Schema for one DAG param (Airflow's `Param` keywords). */
function paramSchema(name: string, raw: unknown): JsonSchema {
  const p = asDagParam(raw);
  const schema: JsonSchema = {};
  const type = p.type ?? inferType(p.default);
  if (type) schema.type = type;
  schema.title = p.title ?? name;
  const description = p.description ?? p.description_md;
  if (description) schema.description = description;
  if (p.enum) schema.enum = p.enum;
  if (p.format && FORMATS.has(p.format)) schema.format = p.format;
  if (p.minimum !== null && p.minimum !== undefined) schema.minimum = p.minimum;
  if (p.maximum !== null && p.maximum !== undefined) schema.maximum = p.maximum;
  if (p.min_length !== null && p.min_length !== undefined) schema.minLength = p.min_length;
  if (p.max_length !== null && p.max_length !== undefined) schema.maxLength = p.max_length;
  if (p.pattern) schema.pattern = p.pattern;
  if (p.items) schema.items = p.items as JsonSchema;
  if (p.examples) schema.examples = p.examples;
  if (p.const !== undefined && p.const !== null) schema.const = p.const;
  if (p.section) schema["x-section"] = p.section;
  if (p.default !== undefined) schema.default = p.default;
  return schema;
}

/** DAG `params` → an object JSON Schema for the trigger form. */
export function paramsToJsonSchema(params: Record<string, unknown> | undefined): JsonSchema {
  const properties: Record<string, JsonSchema> = {};
  const required: string[] = [];
  for (const [name, raw] of Object.entries(params ?? {})) {
    const s = paramSchema(name, raw);
    properties[name] = s;
    // A param without a usable default must be provided at trigger time.
    const t = s.type;
    const nullable = Array.isArray(t) ? t.includes("null") : t === "null";
    if ((s.default === undefined || s.default === null) && !nullable) required.push(name);
  }
  const schema: JsonSchema = { type: "object", properties };
  if (required.length) schema.required = required;
  return schema;
}

/** Default conf derived from the params (what Airflow would use without overrides). */
export function paramDefaults(params: Record<string, unknown> | undefined): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [name, raw] of Object.entries(params ?? {})) {
    const d = asDagParam(raw).default;
    if (d !== undefined && d !== null) out[name] = d;
  }
  return out;
}
