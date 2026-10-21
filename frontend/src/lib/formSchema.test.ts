import { describe, expect, it } from "vitest";

import {
  type JsonSchema,
  type ParamField,
  compactParams,
  fillRequired,
  hasJinja,
  paramFields,
  paramMode,
  toFormSchema,
  PYTHON_PATH_RE,
  isPythonRef,
} from "./formSchema";

describe("toFormSchema", () => {
  const { schema, uiSchema } = toFormSchema({
    type: "object",
    required: ["bash_command", "python_callable"],
    properties: {
      bash_command: { type: "string", "x-autoair-templated": true, "x-autoair-renderer": "bash" },
      env: { anyOf: [{ type: "object", additionalProperties: { type: "string" } }, { type: "null" }], default: null },
      skip_on_exit_code: { type: ["integer", "null"], default: 99 },
      postgres_conn_id: { type: "string", "x-autoair-widget": "connection" },
      python_callable: { "x-autoair-unsupported": "callable" },
    },
  });

  it("collapses nullable unions", () => {
    expect(schema.properties?.skip_on_exit_code?.type).toBe("integer");
    expect(schema.properties?.env?.type).toBe("object");
  });

  it("moves defaults into placeholders", () => {
    expect(schema.properties?.skip_on_exit_code?.default).toBeUndefined();
    expect(uiSchema.skip_on_exit_code?.["ui:placeholder"]).toBe("default: 99");
  });

  it("drops unsupported params, including from required", () => {
    expect(schema.properties?.python_callable).toBeUndefined();
    expect(schema.required).toEqual(["bash_command"]);
  });

  it("maps x-autoair hints to widgets", () => {
    expect(uiSchema.bash_command?.["ui:widget"]).toBe("code");
    expect(uiSchema.postgres_conn_id?.["ui:widget"]).toBe("connection");
    expect(uiSchema.env?.["ui:field"]).toBe("json");
    expect(schema.properties?.bash_command?.title).toBe("bash_command · Jinja");
  });

  it("compacts empty values", () => {
    expect(compactParams({ a: "", b: undefined, c: 0, d: false })).toEqual({ c: 0, d: false });
  });
});

describe("paramFields", () => {
  const schema = {
    type: "object",
    required: ["filepath"],
    properties: {
      mode: { type: "string", enum: ["poke", "reschedule"], default: "poke" },
      poke_interval: { type: ["number", "null"], default: 60 },
      filepath: { type: "string", "x-autoair-templated": true },
      fs_conn_id: { type: "string", "x-autoair-widget": "connection", "x-autoair-conn-type": ["fs"] },
      deferrable: { type: "boolean", default: false },
      sql: { type: "string", "x-autoair-templated": true, "x-autoair-renderer": "sql" },
      tags: { type: "array", items: { type: "string" } },
      callback: { "x-autoair-unsupported": "callable" },
      extra: { anyOf: [{ type: "object" }, { type: "integer" }] },
    },
  } satisfies JsonSchema;

  const fields = paramFields(schema, { isSensor: true, deferrable: true });
  const byName = Object.fromEntries(fields.map((f) => [f.name, f]));

  it("orders required params first and keeps callables as python references", () => {
    expect(fields[0]?.name).toBe("filepath");
    expect(byName.callback?.pythonRef).toBe("required");
  });

  it("groups sensor and deferrable params", () => {
    expect(byName.mode?.group).toBe("sensor");
    expect(byName.poke_interval?.group).toBe("sensor");
    expect(byName.deferrable?.group).toBe("deferrable");
    expect(byName.filepath?.group).toBe("main");
    expect(paramFields(schema).find((f) => f.name === "mode")?.group).toBe("main");
  });

  it("picks widgets from types and hints", () => {
    expect(byName.mode?.widget).toBe("enum");
    expect(byName.poke_interval?.widget).toBe("number");
    expect(byName.fs_conn_id?.widget).toBe("connection");
    expect(byName.fs_conn_id?.connTypes).toEqual(["fs"]);
    expect(byName.sql?.widget).toBe("code");
    expect(byName.sql?.renderer).toBe("sql");
    expect(byName.tags?.widget).toBe("string-list");
    expect(byName.extra?.widget).toBe("json");
    expect(byName.filepath?.templated).toBe(true);
  });

  it("fills required params with real defaults only (never empty placeholders)", () => {
    const { value: filled, unfilled } = fillRequired(
      [
        ...fields,
        { ...(byName.poke_interval as ParamField), name: "n", required: true, hasDefault: true, default: 5 },
        { ...(byName.deferrable as ParamField), name: "b", required: true, hasDefault: false },
      ],
      { mode: "reschedule" },
    );
    // `filepath` has no default: it stays missing (an empty string would hide the problem).
    expect(filled).toEqual({ mode: "reschedule", n: 5, b: false });
    expect(unfilled).toEqual(["filepath"]);
  });

  it("detects the value mode", () => {
    const templated = { templated: true };
    const plain = { templated: false };
    expect(paramMode(plain, { $type: "xcom", node: "a" })).toBe("xcom");
    expect(paramMode(plain, "{{ ds }}")).toBe("template");
    expect(paramMode(templated, "{{ ds }}")).toBe("literal");
    expect(paramMode(plain, 3)).toBe("literal");
    expect(paramMode(plain, { $type: "import", path: "pkg.mod.fn" })).toBe("python");
    // A callable-only param is edited as a Python reference even before it is set.
    expect(paramMode({ templated: false, pythonRef: "required" }, undefined)).toBe("python");
    expect(paramMode({ templated: false, pythonRef: "optional" }, undefined)).toBe("literal");
    expect(hasJinja("{% if x %}")).toBe(true);
    expect(hasJinja("plain")).toBe(false);
  });
});

describe("python reference params", () => {
  const schema = {
    type: "object",
    required: ["python_callable"],
    properties: {
      python_callable: { title: "Python callable", "x-autoair-python-ref": "required" as const },
      producer: { type: "string", "x-autoair-python-ref": "optional" as const },
      legacy: { "x-autoair-unsupported": "callable" },
    },
  };

  it("keeps callable params as python-reference fields", () => {
    const fields = paramFields(schema);
    const byName = Object.fromEntries(fields.map((f) => [f.name, f]));
    expect(byName.python_callable?.pythonRef).toBe("required");
    expect(byName.producer?.pythonRef).toBe("optional");
    expect(byName.legacy?.pythonRef).toBe("required");
  });

  it("does not invent an empty literal for a required reference", () => {
    const filled = fillRequired(paramFields(schema), {});
    expect(filled).not.toHaveProperty("python_callable");
  });

  it("validates dotted import paths", () => {
    expect(PYTHON_PATH_RE.test("acme.hooks.run")).toBe(true);
    expect(PYTHON_PATH_RE.test("json")).toBe(false);
    expect(PYTHON_PATH_RE.test("os.system; rm")).toBe(false);
    expect(isPythonRef({ $type: "import", path: "a.b" })).toBe(true);
  });
});

describe("catalog widget overrides (x-autoair-widget)", () => {
  const overridden: JsonSchema = {
    type: "object",
    required: ["query"],
    properties: {
      query: { type: "string", "x-autoair-widget": "sql", "x-autoair-renderer": "sql" },
      script: { type: "string", "x-autoair-widget": "python" },
      plain: { type: "string", "x-autoair-widget": "text", "x-autoair-renderer": "bash" },
      token: { type: "string", "x-autoair-widget": "secret" },
      var_name: { type: "string", "x-autoair-widget": "variable" },
      payload: { type: "string", "x-autoair-widget": "json" },
      mode: { type: "string", "x-autoair-widget": "enum", enum: ["append", "overwrite"] },
      bad_enum: { type: "string", "x-autoair-widget": "enum" },
      internal: { type: "string", "x-autoair-hidden": true },
    },
  };
  const byName = Object.fromEntries(paramFields(overridden).map((f) => [f.name, f]));

  it("maps every override widget", () => {
    expect(byName.query?.widget).toBe("code");
    expect(byName.query?.renderer).toBe("sql");
    expect(byName.script?.widget).toBe("code");
    expect(byName.script?.renderer).toBe("python");
    expect(byName.plain?.widget).toBe("text");
    expect(byName.plain?.renderer).toBeUndefined();
    expect(byName.token?.widget).toBe("secret");
    expect(byName.var_name?.widget).toBe("variable");
    expect(byName.payload?.widget).toBe("json");
    expect(byName.mode?.widget).toBe("enum");
    // An enum widget without values degrades to text instead of an empty select.
    expect(byName.bad_enum?.widget).toBe("text");
  });

  it("marks hidden params and leaves them out of the RJSF schema", () => {
    expect(byName.internal?.hidden).toBe(true);
    expect(byName.query?.hidden).toBe(false);
    const { schema, uiSchema } = toFormSchema(overridden);
    expect(schema.properties?.internal).toBeUndefined();
    expect(uiSchema.query?.["ui:widget"]).toBe("code");
    expect(uiSchema.token?.["ui:widget"]).toBe("password");
    expect(uiSchema.var_name?.["ui:widget"]).toBe("variable");
    expect(uiSchema.plain?.["ui:widget"]).toBeUndefined();
  });
});
