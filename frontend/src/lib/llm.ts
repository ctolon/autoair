/**
 * Values of apache-airflow-providers-common-ai tasks (LLMOperator, AgentOperator, @task.llm…):
 * the structured output model (`output_type`) and agent toolsets, as the spec's tagged values.
 */

export const OUTPUT_FIELD_TYPES = [
  "str",
  "int",
  "float",
  "bool",
  "list[str]",
  "list[int]",
  "list[float]",
  "dict",
] as const;
export type OutputFieldType = (typeof OUTPUT_FIELD_TYPES)[number];

export type OutputField = {
  name: string;
  type?: OutputFieldType;
  description?: string | null;
  required?: boolean;
  choices?: string[];
};

export type OutputModelValue = {
  $type: "output_model";
  name: string;
  description?: string | null;
  fields: OutputField[];
};

export type ToolsetValue = {
  $type: "toolset";
  kind: "sql" | "mcp";
  conn_id: string;
  allowed_tables?: string[] | null;
  db_schema?: string | null;
  allow_writes?: boolean;
  max_rows?: number | null;
  tool_prefix?: string | null;
};

/** Params of common.ai operators handled by the dedicated editor (others: generic form). */
export const LLM_PARAMS = [
  "prompt",
  "system_prompt",
  "output_type",
  "toolsets",
  "require_approval",
  "approval_timeout",
  "allow_modifications",
  "enable_hitl_review",
  "max_hitl_iterations",
  "hitl_timeout",
] as const;

/** `@task.<name>` decorators of the common.ai provider (the function returns the prompt). */
const LLM_DECORATORS = ["llm", "agent", "llm_branch", "llm_sql"] as const;
export const isLlmDecorator = (d: string | null | undefined): boolean =>
  (LLM_DECORATORS as readonly string[]).includes(d ?? "");

/** decorator_kwargs each common.ai decorator accepts that the editor shows. */
export function llmDecoratorKwargs(decorator: string): string[] {
  const base = ["llm_conn_id", "model_id", "system_prompt", "output_type"];
  if (decorator === "agent") return [...base, "toolsets", "enable_hitl_review", "max_hitl_iterations", "hitl_timeout"];
  if (decorator === "llm_sql")
    return [...base, "db_conn_id", "require_approval", "approval_timeout", "allow_modifications"];
  return [...base, "require_approval", "approval_timeout", "allow_modifications"];
}

/** An operator whose params look like a common.ai LLM task (prompt + llm_conn_id). */
export const isLlmSchema = (props: Record<string, unknown> | undefined): boolean =>
  !!props && "prompt" in props && "llm_conn_id" in props;

const IDENT = /^[A-Za-z_][A-Za-z0-9_]*$/;
export const CLASS_NAME = /^[A-Z][A-Za-z0-9_]{0,99}$/;

export function isOutputModel(v: unknown): v is OutputModelValue {
  return typeof v === "object" && v !== null && (v as { $type?: unknown }).$type === "output_model";
}

export function isToolset(v: unknown): v is ToolsetValue {
  return typeof v === "object" && v !== null && (v as { $type?: unknown }).$type === "toolset";
}

export function newOutputModel(): OutputModelValue {
  return { $type: "output_model", name: "Result", fields: [{ name: "answer", type: "str", required: true }] };
}

/** Problems the server would report (duplicate / invalid names, choices on non-str). */
export function outputModelErrors(m: OutputModelValue): string[] {
  const errors: string[] = [];
  if (!CLASS_NAME.test(m.name)) errors.push("name");
  if (!m.fields.length) errors.push("fields");
  const seen = new Set<string>();
  m.fields.forEach((f, i) => {
    if (!IDENT.test(f.name) || seen.has(f.name)) errors.push(`fields.${i}.name`);
    seen.add(f.name);
    if (f.choices?.length && (f.type ?? "str") !== "str") errors.push(`fields.${i}.choices`);
  });
  return errors;
}

/** Python preview of the class the code generator emits. */
export function outputModelPreview(m: OutputModelValue): string {
  const lines = [`class ${m.name || "Result"}(BaseModel):`];
  if (m.description) lines.push(`    ${JSON.stringify(m.description)}`);
  for (const f of m.fields) {
    let type: string = f.choices?.length
      ? `Literal[${f.choices.map((c) => JSON.stringify(c)).join(", ")}]`
      : (f.type ?? "str");
    const required = f.required !== false;
    if (!required) type += " | None";
    const kws = [
      ...(required ? [] : ["default=None"]),
      ...(f.description ? [`description=${JSON.stringify(f.description)}`] : []),
    ];
    lines.push(`    ${f.name || "field"}: ${type}${kws.length ? ` = Field(${kws.join(", ")})` : ""}`);
  }
  if (lines.length === 1) lines.push("    pass");
  return lines.join("\n");
}

/** Drop options the toolset kind does not take (the server rejects them). */
export function cleanToolset(t: ToolsetValue): ToolsetValue {
  if (t.kind === "mcp") {
    return {
      $type: "toolset",
      kind: "mcp",
      conn_id: t.conn_id,
      ...(t.tool_prefix ? { tool_prefix: t.tool_prefix } : {}),
    };
  }
  const out: ToolsetValue = { $type: "toolset", kind: "sql", conn_id: t.conn_id };
  if (t.allowed_tables?.length) out.allowed_tables = t.allowed_tables;
  if (t.db_schema) out.db_schema = t.db_schema;
  if (t.allow_writes) out.allow_writes = true;
  if (t.max_rows) out.max_rows = t.max_rows;
  return out;
}

/** Seconds of a `{"$type": "timedelta"}` value (or a plain number). */
export function timedeltaSeconds(v: unknown): number | null {
  if (typeof v === "number") return v;
  if (typeof v === "object" && v !== null && (v as { $type?: unknown }).$type === "timedelta") {
    const s = (v as { seconds?: unknown }).seconds;
    return typeof s === "number" ? s : null;
  }
  return null;
}

export const timedelta = (seconds: number | null) => (seconds == null ? undefined : { $type: "timedelta", seconds });
