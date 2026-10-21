import { describe, expect, it } from "vitest";

import type { Template } from "../../api/gen/types.gen";
import type { OperatorSpec } from "../../lib/types";
import {
  BLUEPRINT_NODE_ID,
  EXPORT_FORMAT,
  PREVIEW_DAG_ID,
  addableFields,
  conflictInfo,
  diffText,
  effectiveId,
  emptyState,
  exportJson,
  fieldRows,
  metaErrors,
  parseImport,
  previewSpec,
  problemTab,
  stateFromTemplate,
  templateProblems,
  toTemplateInput,
  usesInDag,
} from "./operatorTemplateEditor";

const BASH = "airflow.providers.standard.operators.bash.BashOperator";

const TEMPLATE: Template = {
  id: "notify",
  title: "Notify",
  kind: "operator",
  builtin: false,
  version: "3",
  etag: "abc",
  owner_team: "platform",
  tags: ["slack", "ops"],
  docs_url: "https://example.com",
  blueprint: {
    node: {
      id: "template",
      kind: "operator",
      task_id: "notify",
      operator: BASH,
      params: { env: { A: "1" } },
      retries: 4,
      position: { x: 0, y: 0 },
    },
    locked: ["retries"],
    required: ["params.bash_command"],
  },
};

describe("operator template editor state", () => {
  it("round-trips a saved template (edit keeps the id, the version is assigned by the server)", () => {
    const state = stateFromTemplate(TEMPLATE, "edit");
    expect(state.meta.id).toBe("notify");
    expect(state.meta.version).toBe("");
    expect(state.meta.tags).toBe("slack, ops");
    expect(state.modes).toEqual({ retries: "locked", "params.bash_command": "required" });
    expect(state.spec).not.toHaveProperty("id");
    expect(state.spec).not.toHaveProperty("position");
    const input = toTemplateInput(state);
    expect(input?.blueprint?.locked).toEqual(["retries"]);
    expect(input?.blueprint?.required).toEqual(["params.bash_command"]);
    expect(input?.blueprint?.node.id).toBe("template");
    expect(input?.docs_url).toBe("https://example.com");
    expect(input?.tags).toEqual(["slack", "ops"]);
    expect(input?.version).toBeNull();
  });

  it("duplicates under a new id and title", () => {
    const state = stateFromTemplate(TEMPLATE, "duplicate");
    expect(effectiveId(state.meta)).toBe("notify-copy");
    expect(state.meta.title).toBe("Notify (copy)");
  });

  it("drops the value of required fields and ignores default modes", () => {
    const state = stateFromTemplate(TEMPLATE, "edit");
    state.spec = { ...(state.spec as OperatorSpec), params: { bash_command: "echo", env: {} } };
    state.modes = { ...state.modes, pool: "default" };
    const node = toTemplateInput(state)?.blueprint?.node as OperatorSpec;
    expect(node.params).not.toHaveProperty("bash_command");
    expect(toTemplateInput(state)?.blueprint?.locked).toEqual(["retries"]);
  });

  it("lists configured fields, fields with a mode, and addable ones", () => {
    const state = stateFromTemplate(TEMPLATE, "edit");
    const spec = state.spec as OperatorSpec;
    const rows = fieldRows(spec, state.modes);
    expect(rows).toEqual(["params.env", "retries", "params.bash_command"]);
    const addable = addableFields(spec, ["bash_command", "cwd", "env"], rows);
    expect(addable).toContain("params.cwd");
    expect(addable).toContain("pool");
    expect(addable).not.toContain("params.env");
    expect(addable).not.toContain("code");
  });

  it("checks the metadata", () => {
    const meta = emptyState().meta;
    expect(metaErrors(meta)).toEqual(["title", "id"]);
    expect(metaErrors({ ...meta, title: "Notify Slack" })).toEqual([]);
    expect(effectiveId({ ...meta, title: "Notify Slack!" })).toBe("notify-slack");
    expect(metaErrors({ ...meta, title: "x", docsUrl: "javascript:alert(1)" })).toEqual(["docsUrl"]);
    expect(metaErrors({ ...meta, title: "x", id: "Bad Id", idTouched: true })).toEqual(["id"]);
  });
});

describe("live validation of the throwaway DAG", () => {
  it("builds a one-task preview DAG without the template link", () => {
    const spec = { kind: "operator", task_id: "t", operator: BASH, template_ref: { id: "x" } } as OperatorSpec;
    const dag = previewSpec(spec);
    expect(dag?.dag_id).toBe(PREVIEW_DAG_ID);
    expect(dag?.nodes?.[0]).toMatchObject({ id: BLUEPRINT_NODE_ID, operator: BASH });
    expect(dag?.nodes?.[0]).not.toHaveProperty("template_ref");
    expect(previewSpec(null)).toBeNull();
  });

  it("keeps the task's problems, minus required fields and DAG-level findings", () => {
    const problems = [
      { node_id: "template", field: "bash_command", severity: "error" as const },
      { node_id: "template", field: "cwd", severity: "error" as const },
      { node_id: null, field: null, severity: "warning" as const },
    ];
    expect(templateProblems(problems, { "params.bash_command": "required" })).toEqual([problems[1]]);
    expect(problemTab({ field: "retries" })).toBe("settings");
    expect(problemTab({ field: "bash_command" })).toBe("params");
  });
});

describe("import, export, diff and conflicts", () => {
  it("exports and imports the same template", () => {
    const input = toTemplateInput(stateFromTemplate(TEMPLATE, "edit"));
    if (!input) throw new Error("no input");
    const text = exportJson(input);
    expect(JSON.parse(text).format).toBe(EXPORT_FORMAT);
    const parsed = parseImport(text);
    expect(parsed.ok).toBe(true);
    if (parsed.ok) {
      expect(parsed.state.meta.id).toBe("notify");
      expect(parsed.state.modes).toEqual({ retries: "locked", "params.bash_command": "required" });
    }
  });

  it("rejects files that are not operator templates without throwing", () => {
    expect(parseImport("{not json")).toEqual({ ok: false, error: "json" });
    expect(parseImport("[]")).toEqual({ ok: false, error: "shape" });
    expect(parseImport(JSON.stringify({ kind: "dag", spec: {} }))).toEqual({ ok: false, error: "shape" });
    expect(parseImport(JSON.stringify({ blueprint: { node: { kind: "note" } } }))).toEqual({
      ok: false,
      error: "shape",
    });
    const odd = parseImport(JSON.stringify({ title: 5, tags: "x", blueprint: { node: { kind: "operator" } } }));
    expect(odd.ok).toBe(true);
    if (odd.ok) expect(odd.state.meta.tags).toBe("");
  });

  it("diffs content independent of key order and identity", () => {
    const a = diffText(TEMPLATE);
    const reordered = { ...TEMPLATE, etag: "other", version: "4", tags: ["slack", "ops"] };
    expect(diffText(reordered)).toBe(a);
    expect(a).toContain('"retries": 4');
    expect(diffText(null)).toBe("");
  });

  it("reads the 409 detail", () => {
    const err = { detail: { message: "changed", current_version: "4", current_etag: "e", updated_by: "bob" } };
    expect(conflictInfo(err)).toEqual({ message: "changed", currentVersion: "4", currentEtag: "e", updatedBy: "bob" });
    expect(conflictInfo({ detail: "plain" })).toBeNull();
    expect(conflictInfo(new Error("x"))).toBeNull();
  });

  it("counts tasks of the open DAG created from a template", () => {
    const nodes = [
      { data: { spec: { template_ref: { id: "notify" } } } },
      { data: { spec: { template_ref: { id: "other" } } } },
      { data: { spec: {} } },
    ];
    expect(usesInDag(nodes, "notify")).toBe(1);
  });
});
