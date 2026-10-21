import { describe, expect, it } from "vitest";

import {
  coerceInput,
  compactParam,
  defaultForType,
  looksLikeDagParam,
  moveKey,
  normalizeParam,
  paramToJsonSchema,
  paramTypes,
  paramsToTriggerForm,
  primaryType,
  renameKey,
} from "./dagParams";

describe("normalizeParam", () => {
  it("wraps bare values as defaults", () => {
    expect(normalizeParam(10)).toEqual({ default: 10 });
    expect(normalizeParam("x")).toEqual({ default: "x" });
    expect(normalizeParam({ foo: 1 })).toEqual({ default: { foo: 1 } });
    expect(normalizeParam([1, 2])).toEqual({ default: [1, 2] });
  });
  it("keeps DagParam objects", () => {
    expect(looksLikeDagParam({ default: 1, type: "integer" })).toBe(true);
    expect(normalizeParam({ default: 1, type: "integer" })).toEqual({ default: 1, type: "integer" });
  });
});

describe("types", () => {
  it("infers type from default", () => {
    expect(paramTypes({ default: 3 })).toEqual(["integer"]);
    expect(paramTypes({ default: 3.5 })).toEqual(["number"]);
    expect(paramTypes({ type: ["string", "null"] })).toEqual(["string", "null"]);
    expect(primaryType({ type: ["string", "null"] })).toBe("string");
    expect(primaryType({ type: ["string", "integer"] })).toBeNull();
  });
  it("gives defaults per type", () => {
    expect(defaultForType("array")).toEqual([]);
    expect(defaultForType("boolean")).toBe(false);
    expect(defaultForType("null")).toBeNull();
  });
  it("coerces typed inputs", () => {
    expect(coerceInput("5", "integer")).toEqual({ ok: true, value: 5 });
    expect(coerceInput("5.5", "integer").ok).toBe(false);
    expect(coerceInput("5.5", "number")).toEqual({ ok: true, value: 5.5 });
    expect(coerceInput("[1]", "array")).toEqual({ ok: true, value: [1] });
    expect(coerceInput("{}", "array").ok).toBe(false);
    expect(coerceInput("true", "boolean")).toEqual({ ok: true, value: true });
  });
});

describe("paramToJsonSchema", () => {
  it("maps snake_case keys to JSON Schema keywords", () => {
    expect(
      paramToJsonSchema({ default: "a", type: "string", min_length: 1, max_length: 5, section: "S", title: "T" }),
    ).toEqual({ default: "a", type: "string", minLength: 1, maxLength: 5, section: "S", title: "T" });
  });
});

describe("paramsToTriggerForm", () => {
  const { sections } = paramsToTriggerForm({
    limit: 10,
    env: { type: "string", enum: ["dev", "prod"], values_display: { prod: "Production" }, section: "Target" },
    note: { type: ["string", "null"], format: "multiline" },
    day: { type: "string", format: "date", default: "2024-01-01" },
  });
  it("groups by section, unsectioned first", () => {
    expect(sections.map((s) => s.name)).toEqual([null, "Target"]);
    expect(sections[0]?.names).toEqual(["limit", "note", "day"]);
  });
  it("builds fields with defaults and ui hints", () => {
    const main = sections[0];
    expect(main?.schema.properties?.limit?.type).toBe("integer");
    expect(main?.formData).toEqual({ limit: 10, day: "2024-01-01" });
    expect(main?.uiSchema.note?.["ui:widget"]).toBe("textarea");
    expect(main?.schema.properties?.day?.format).toBe("date");
    expect(main?.schema.required).toEqual(["limit", "day"]);
    const target = sections[1];
    expect(target?.uiSchema.env?.["ui:enumNames"]).toEqual(["dev", "Production"]);
  });
});

describe("object helpers", () => {
  it("renames and moves keys preserving order", () => {
    expect(Object.keys(renameKey({ a: 1, b: 2, c: 3 }, "b", "x"))).toEqual(["a", "x", "c"]);
    expect(Object.keys(moveKey({ a: 1, b: 2, c: 3 }, "c", -1))).toEqual(["a", "c", "b"]);
  });
  it("compacts empty fields", () => {
    expect(compactParam({ default: "", title: "", enum: [], type: "string" })).toEqual({ default: "", type: "string" });
  });
});
