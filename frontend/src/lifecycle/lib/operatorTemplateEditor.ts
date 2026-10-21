import type { DagSpec, Problem, Template, TemplateInput } from "../../api/gen/types.gen";
import { type FieldMode, TASK_FIELDS, configurablePaths, getPath, makeBlueprint } from "../../canvas/operatorTemplates";
import { canonicalJson } from "../../lib/canonical";
import type { TaskSpec } from "../../lib/types";

/**
 * Pure logic of the standalone operator template editor (no canvas): editor state <->
 * `TemplateInput`, the throwaway one-task DAG used for live validation and the code
 * preview, JSON import/export and the text compared in the version diff.
 */

/** Same as the backend (`autoair.templates.BLUEPRINT_NODE_ID` / `PREVIEW_DAG_ID`). */
export const BLUEPRINT_NODE_ID = "template";
export const PREVIEW_DAG_ID = "operator_template_preview";
const TEMPLATE_ID_RE = /^[a-z0-9][a-z0-9_-]{0,80}$/;
const DOCS_URL_RE = /^https?:\/\/\S+$/i;
/** Marker of an exported operator template file. */
export const EXPORT_FORMAT = "autoair.operator-template/v1";

export type EditorMode = "new" | "edit" | "duplicate";

export type EditorMeta = {
  id: string;
  /** The id was typed by the user (otherwise it follows the title). */
  idTouched: boolean;
  title: string;
  description: string;
  ownerTeam: string;
  /** Comma separated. */
  tags: string;
  icon: string | null;
  docsUrl: string;
  /** Empty: the server assigns the next number. */
  version: string;
};

export type EditorState = {
  meta: EditorMeta;
  /** The task (no id, position or template link); null until an operator is picked. */
  spec: TaskSpec | null;
  /** Field path -> mode (absent = default). */
  modes: Record<string, FieldMode>;
};

const slugify = (s: string): string =>
  s
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 80);

export function emptyState(ownerTeam = ""): EditorState {
  return {
    meta: {
      id: "",
      idTouched: false,
      title: "",
      description: "",
      ownerTeam,
      tags: "",
      icon: null,
      docsUrl: "",
      version: "",
    },
    spec: null,
    modes: {},
  };
}

export const effectiveId = (meta: EditorMeta): string => (meta.idTouched ? meta.id : slugify(meta.title));

/** The task of a stored blueprint, without the per-task fields the canvas assigns. */
function specOfBlueprint(node: NonNullable<Template["blueprint"]>["node"]): TaskSpec {
  const raw = structuredClone(node) as Record<string, unknown>;
  delete raw.id;
  delete raw.position;
  delete raw.parent_group;
  delete raw.template_ref;
  return raw as unknown as TaskSpec;
}

/** Editor state of a saved template: `edit` publishes its next version, `duplicate` a copy. */
export function stateFromTemplate(tpl: Template, mode: Exclude<EditorMode, "new">): EditorState {
  const modes: Record<string, FieldMode> = {};
  for (const p of tpl.blueprint?.locked ?? []) modes[p] = "locked";
  for (const p of tpl.blueprint?.required ?? []) modes[p] = "required";
  const copy = mode === "duplicate";
  return {
    meta: {
      id: copy ? `${tpl.id}-copy`.slice(0, 81) : tpl.id,
      idTouched: true,
      title: copy ? `${tpl.title} (copy)`.slice(0, 120) : tpl.title,
      description: tpl.description ?? "",
      ownerTeam: tpl.owner_team ?? "",
      tags: (tpl.tags ?? []).join(", "),
      icon: tpl.icon ?? null,
      docsUrl: tpl.docs_url ?? "",
      version: "",
    },
    spec: tpl.blueprint ? specOfBlueprint(tpl.blueprint.node) : null,
    modes,
  };
}

/**
 * The fields shown in the mode table: every configured field, plus fields given a mode
 * (a required field has no stored value), plus `extra` fields the user added.
 */
export function fieldRows(spec: TaskSpec, modes: Readonly<Record<string, FieldMode>>): string[] {
  const out = configurablePaths(spec);
  for (const path of Object.keys(modes)) if (!out.includes(path)) out.push(path);
  return out;
}

/** Fields that can be added to the mode table (unset parameters and task settings). */
export function addableFields(spec: TaskSpec, paramNames: readonly string[], shown: readonly string[]): string[] {
  const taken = new Set(shown);
  const prefix = spec.kind === "operator" ? "params" : spec.kind === "python_task" ? "arguments" : "config";
  const params = paramNames.map((n) => `${prefix}.${n}`);
  const task = TASK_FIELDS.filter((f) => f !== "code" || spec.kind === "python_task");
  return [...params, ...task].filter((p) => !taken.has(p));
}

const splitTags = (tags: string) =>
  tags
    .split(",")
    .map((x) => x.trim())
    .filter(Boolean);

type MetaError = "title" | "id" | "docsUrl";

/** What blocks saving the metadata (the task is validated by the server). */
export function metaErrors(meta: EditorMeta): MetaError[] {
  const out: MetaError[] = [];
  if (!meta.title.trim() || meta.title.trim().length > 120) out.push("title");
  if (!TEMPLATE_ID_RE.test(effectiveId(meta))) out.push("id");
  if (meta.docsUrl.trim() && !DOCS_URL_RE.test(meta.docsUrl.trim())) out.push("docsUrl");
  return out;
}

/** Only the modes that differ from the default (and only for fields that still exist). */
function activeModes(modes: Readonly<Record<string, FieldMode>>): Record<string, FieldMode> {
  return Object.fromEntries(Object.entries(modes).filter(([, m]) => m !== "default"));
}

/** The request body of `saveTemplate`. */
export function toTemplateInput(state: EditorState): TemplateInput | null {
  if (!state.spec) return null;
  const { meta } = state;
  return {
    id: effectiveId(meta),
    title: meta.title.trim(),
    description: meta.description,
    category: "operator",
    tags: splitTags(meta.tags),
    kind: "operator",
    owner_team: meta.ownerTeam.trim() || null,
    version: meta.version.trim() || null,
    icon: meta.icon,
    docs_url: meta.docsUrl.trim() || null,
    blueprint: makeBlueprint(state.spec, activeModes(state.modes)),
  };
}

/** The throwaway one-task DAG validated and rendered while editing. */
export function previewSpec(spec: TaskSpec | null): DagSpec | null {
  if (!spec) return null;
  const node = { ...spec, id: BLUEPRINT_NODE_ID } as Record<string, unknown>;
  delete node.template_ref;
  return { dag_id: PREVIEW_DAG_ID, nodes: [node as NonNullable<DagSpec["nodes"]>[number]], edges: [] };
}

/** Problem field name of a field path (operator params are reported by name). */
const problemField = (path: string): string => path.replace(/^params\./, "");

/**
 * The problems that concern the template (mirrors `autoair.templates.blueprint_problems`):
 * the task's own, minus those of required fields (asked on insert, so empty on purpose);
 * findings about the throwaway DAG itself are dropped.
 */
export function templateProblems<T extends Pick<Problem, "node_id" | "field">>(
  problems: readonly T[],
  modes: Readonly<Record<string, FieldMode>>,
): T[] {
  const asked = new Set<string>();
  for (const [path, mode] of Object.entries(modes)) {
    if (mode !== "required") continue;
    asked.add(path);
    asked.add(problemField(path));
  }
  return problems.filter((p) => p.node_id === BLUEPRINT_NODE_ID && !(p.field && asked.has(p.field)));
}

const SETTINGS = new Set<string>([...TASK_FIELDS, "task_display_name", "executor_config"]);

/** Which editor tab shows a problem's field. */
export function problemTab(p: Pick<Problem, "field">): "params" | "settings" {
  return p.field && SETTINGS.has(p.field) && p.field !== "code" ? "settings" : "params";
}

/** Per-field errors for the forms: `params` keys by name, settings by field. */
export function fieldErrors(
  problems: readonly Pick<Problem, "field" | "severity">[],
  text: (p: Problem) => string,
): Record<string, string> {
  const out: Record<string, string> = {};
  for (const p of problems as Problem[]) if (p.field && p.severity === "error" && !out[p.field]) out[p.field] = text(p);
  return out;
}

/** Downloadable JSON of a template (a `TemplateInput`, importable here or by the API). */
export function exportJson(input: TemplateInput): string {
  return `${JSON.stringify({ format: EXPORT_FORMAT, ...input, version: null }, null, 2)}\n`;
}

type ImportResult = { ok: true; state: EditorState } | { ok: false; error: "json" | "shape" };

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);

/** Parses an exported template (or a `Template` from the API) into editor state. */
export function parseImport(text: string): ImportResult {
  let data: unknown;
  try {
    data = JSON.parse(text);
  } catch {
    return { ok: false, error: "json" };
  }
  if (!isRecord(data) || (data.kind !== undefined && data.kind !== "operator")) return { ok: false, error: "shape" };
  const bp = data.blueprint;
  if (!isRecord(bp) || !isRecord(bp.node)) return { ok: false, error: "shape" };
  const kind = bp.node.kind;
  if (kind !== "operator" && kind !== "python_task" && kind !== "custom") return { ok: false, error: "shape" };
  const strings = (v: unknown) => (Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : []);
  const str = (v: unknown) => (typeof v === "string" ? v : "");
  const tpl = {
    id: str(data.id),
    title: str(data.title),
    description: str(data.description),
    tags: strings(data.tags),
    owner_team: str(data.owner_team) || null,
    icon: str(data.icon) || null,
    docs_url: str(data.docs_url) || null,
    kind: "operator",
    builtin: false,
    blueprint: { node: bp.node, locked: strings(bp.locked), required: strings(bp.required) },
  } as unknown as Template;
  const state = stateFromTemplate(tpl, "edit");
  state.meta.idTouched = !!tpl.id;
  return { ok: true, state };
}

/** The text compared by the version diff: the template's content, keys sorted. */
export function diffText(tpl: Template | null | undefined): string {
  if (!tpl) return "";
  const { title, description, owner_team, tags, icon, docs_url, blueprint } = tpl;
  const node = blueprint ? specOfBlueprint(blueprint.node) : null;
  const value = {
    title,
    description,
    owner_team,
    tags,
    icon,
    docs_url,
    node,
    locked: [...(blueprint?.locked ?? [])].sort(),
    required: [...(blueprint?.required ?? [])].sort(),
  };
  return `${JSON.stringify(JSON.parse(canonicalJson(value, { nullAsMissing: true })), null, 2)}\n`;
}

export type ConflictInfo = {
  message: string;
  currentVersion: string | null;
  currentEtag: string | null;
  updatedBy: string | null;
};

/** The structured detail of a 409 from `saveTemplate` / `deleteTemplate`, or null. */
export function conflictInfo(err: unknown): ConflictInfo | null {
  if (!isRecord(err) || !isRecord(err.detail)) return null;
  const d = err.detail;
  if (typeof d.message !== "string" || !("current_etag" in d)) return null;
  const s = (v: unknown) => (typeof v === "string" ? v : null);
  return {
    message: d.message,
    currentVersion: s(d.current_version),
    currentEtag: s(d.current_etag),
    updatedBy: s(d.updated_by),
  };
}

/** Tasks of the open DAG created from template `id` (the cheap usage check before delete). */
export function usesInDag(nodes: readonly { data: { spec: object } }[], id: string): number {
  let n = 0;
  for (const node of nodes) {
    const ref = (node.data.spec as { template_ref?: { id?: string } | null }).template_ref;
    if (ref?.id === id) n += 1;
  }
  return n;
}

/** Display value of a field in the mode table. */
export function valuePreview(spec: TaskSpec, path: string): string | null {
  const v = getPath(spec, path);
  if (v === undefined || v === null || v === "") return null;
  const text = typeof v === "string" ? v : JSON.stringify(v);
  return text.length > 60 ? `${text.slice(0, 57)}…` : text;
}
