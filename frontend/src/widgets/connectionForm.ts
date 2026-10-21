/**
 * Typed connection form helpers: how a connection type (Airflow's hook metadata, served by
 * `GET /api/airflow/connections/hook-meta`) relabels or hides the standard fields and which
 * typed `extra` fields it has. Pure functions so the dialog and tests share them.
 */
import type { ConnectionHookMeta, HookExtraField, HookFieldBehaviour } from "../api/gen/types.gen";

export type StandardField = "host" | "port" | "login" | "password" | "schema" | "description";

/** Airflow names the `schema` field `url_schema` in hook metadata. */
const META_KEY: Record<StandardField, keyof NonNullable<ConnectionHookMeta["standard_fields"]>> = {
  host: "host",
  port: "port",
  login: "login",
  password: "password",
  schema: "url_schema",
  description: "description",
};

interface FieldLook {
  hidden: boolean;
  /** Connection-type specific label (null: the generic label). */
  label: string | null;
  placeholder: string | null;
}

export function standardField(meta: ConnectionHookMeta | undefined, field: StandardField): FieldLook {
  const b: HookFieldBehaviour | null | undefined = meta?.standard_fields?.[META_KEY[field]];
  return { hidden: !!b?.hidden, label: b?.title || null, placeholder: b?.placeholder || null };
}

type ExtraKind = "boolean" | "integer" | "number" | "enum" | "password" | "multiline" | "json" | "string";

function types(schema: Record<string, unknown>): string[] {
  const t = schema.type;
  if (typeof t === "string") return [t];
  if (Array.isArray(t)) return t.filter((x): x is string => typeof x === "string" && x !== "null");
  return [];
}

export function extraKind(field: HookExtraField): ExtraKind {
  const schema = field.schema ?? {};
  if (Array.isArray(schema.enum) && schema.enum.length) return "enum";
  const ts = types(schema);
  if (ts.includes("boolean")) return "boolean";
  if (ts.includes("integer")) return "integer";
  if (ts.includes("number")) return "number";
  if (ts.includes("object") || ts.includes("array")) return "json";
  if (schema.format === "password") return "password";
  if (schema.format === "multiline") return "multiline";
  return "string";
}

export function extraLabel(name: string, field: HookExtraField): string {
  const title = field.schema?.title;
  return typeof title === "string" && title ? title : name;
}

/** Form state of one extra field: text for most kinds, boolean for checkboxes. */
export type ExtraInput = string | boolean;

/** Initial form state from the fields' defaults (absent defaults are empty). */
export function initialExtras(meta: ConnectionHookMeta | undefined): Record<string, ExtraInput> {
  const out: Record<string, ExtraInput> = {};
  for (const [name, field] of Object.entries(meta?.extra_fields ?? {})) {
    const kind = extraKind(field);
    const d = field.default;
    if (kind === "boolean") out[name] = d === true;
    else if (d === null || d === undefined) out[name] = "";
    else out[name] = kind === "json" ? JSON.stringify(d) : String(d);
  }
  return out;
}

type Coerced = { ok: true; value: unknown } | { ok: false };

/** The JSON value of one field's input; `undefined` means "not set" (left out of extra). */
export function coerceExtra(kind: ExtraKind, input: ExtraInput | undefined): Coerced {
  if (kind === "boolean") return { ok: true, value: input === true ? true : undefined };
  const text = typeof input === "string" ? input : "";
  if (!text.trim()) return { ok: true, value: undefined };
  if (kind === "integer") {
    return /^-?\d+$/.test(text.trim()) ? { ok: true, value: Number(text.trim()) } : { ok: false };
  }
  if (kind === "number") {
    const n = Number(text.trim());
    return Number.isFinite(n) ? { ok: true, value: n } : { ok: false };
  }
  if (kind === "json") {
    try {
      return { ok: true, value: JSON.parse(text) as unknown };
    } catch {
      return { ok: false };
    }
  }
  return { ok: true, value: text };
}

interface ExtraResult {
  /** The `extra` JSON string to send (undefined: no extra). */
  extra: string | undefined;
  /** Typed fields whose input is not a valid value of their type. */
  invalid: string[];
  /** The raw JSON (advanced) is not a JSON object. */
  rawInvalid: boolean;
}

/**
 * Build `extra`: the typed field values, then the raw JSON object (advanced) on top, so an
 * explicit raw key always wins. Empty typed inputs are left out.
 */
export function buildExtra(
  meta: ConnectionHookMeta | undefined,
  values: Record<string, ExtraInput>,
  raw: string,
): ExtraResult {
  const out: Record<string, unknown> = {};
  const invalid: string[] = [];
  for (const [name, field] of Object.entries(meta?.extra_fields ?? {})) {
    const r = coerceExtra(extraKind(field), values[name]);
    if (!r.ok) invalid.push(name);
    else if (r.value !== undefined) out[name] = r.value;
  }
  let rawInvalid = false;
  if (raw.trim()) {
    try {
      const parsed: unknown = JSON.parse(raw);
      if (typeof parsed === "object" && parsed !== null && !Array.isArray(parsed)) Object.assign(out, parsed);
      else rawInvalid = true;
    } catch {
      rawInvalid = true;
    }
  }
  return { extra: Object.keys(out).length ? JSON.stringify(out) : undefined, invalid, rawInvalid };
}

/** Hook metadata of a connection type (case-sensitive, as Airflow reports it). */
export function metaFor(metas: ConnectionHookMeta[] | undefined, connType: string): ConnectionHookMeta | undefined {
  return metas?.find((m) => m.connection_type === connType);
}
