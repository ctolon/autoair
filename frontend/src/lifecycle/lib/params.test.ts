import { describe, expect, it } from "vitest";

import { asDagParam, paramDefaults, paramsToJsonSchema } from "./params";

describe("paramsToJsonSchema", () => {
  const params = {
    env: { default: "dev", type: "string", enum: ["dev", "prod"], title: "Environment", description: "where" },
    count: { default: 3, type: "integer", minimum: 1, maximum: 10 },
    name: { type: "string", min_length: 2, max_length: 5, pattern: "^[a-z]+$", format: "multiline" },
    day: { type: ["string", "null"], format: "date" },
    legacy: 42,
  };

  it("converts Param keywords to JSON Schema", () => {
    const schema = paramsToJsonSchema(params);
    expect(schema.type).toBe("object");
    expect(schema.properties?.env).toMatchObject({
      type: "string",
      enum: ["dev", "prod"],
      title: "Environment",
      description: "where",
      default: "dev",
    });
    expect(schema.properties?.count).toMatchObject({ type: "integer", minimum: 1, maximum: 10, title: "count" });
    expect(schema.properties?.name).toMatchObject({ minLength: 2, maxLength: 5, pattern: "^[a-z]+$" });
    expect(schema.properties?.name?.format).toBeUndefined();
    expect(schema.properties?.day?.format).toBe("date");
    expect(schema.properties?.legacy).toMatchObject({ type: "integer", default: 42 });
    expect(schema.required).toEqual(["name"]);
  });

  it("derives defaults", () => {
    expect(paramDefaults(params)).toEqual({ env: "dev", count: 3, legacy: 42 });
    expect(paramsToJsonSchema(undefined)).toEqual({ type: "object", properties: {} });
  });

  it("treats non-Param objects as bare defaults", () => {
    expect(asDagParam({ foo: 1 })).toEqual({ default: { foo: 1 } });
  });
});
