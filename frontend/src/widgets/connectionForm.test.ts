import { describe, expect, it } from "vitest";

import type { ConnectionHookMeta } from "../api/gen/types.gen";
import { buildExtra, coerceExtra, extraKind, initialExtras, metaFor, standardField } from "./connectionForm";

const SLACK: ConnectionHookMeta = {
  connection_type: "slack",
  hook_name: "Slack API",
  standard_fields: {
    url_schema: { hidden: true },
    host: { hidden: true },
    password: { hidden: false, title: "Slack API Token", placeholder: "xoxb" },
  },
  extra_fields: {
    timeout: { schema: { type: ["integer", "null"], title: "Timeout" }, default: 30 },
    verbose: { schema: { type: ["boolean", "null"] } },
    region: { schema: { type: "string", enum: ["eu", "us"] } },
    key: { schema: { type: ["string", "null"], format: "password" } },
    scopes: { schema: { type: ["array", "null"] } },
    ratio: { schema: { type: "number" } },
  },
};

describe("standard fields", () => {
  it("relabels and hides per connection type", () => {
    expect(standardField(SLACK, "schema").hidden).toBe(true);
    expect(standardField(SLACK, "host").hidden).toBe(true);
    expect(standardField(SLACK, "password")).toEqual({ hidden: false, label: "Slack API Token", placeholder: "xoxb" });
    expect(standardField(SLACK, "login")).toEqual({ hidden: false, label: null, placeholder: null });
    expect(standardField(undefined, "host").hidden).toBe(false);
  });
});

describe("extra fields", () => {
  it("derives the input kind from the JSON schema", () => {
    const kind = (name: string) => extraKind(SLACK.extra_fields?.[name] ?? {});
    expect(kind("timeout")).toBe("integer");
    expect(kind("verbose")).toBe("boolean");
    expect(kind("region")).toBe("enum");
    expect(kind("key")).toBe("password");
    expect(kind("scopes")).toBe("json");
    expect(kind("ratio")).toBe("number");
    expect(extraKind({})).toBe("string");
  });

  it("starts from the defaults", () => {
    expect(initialExtras(SLACK)).toEqual({
      timeout: "30",
      verbose: false,
      region: "",
      key: "",
      scopes: "",
      ratio: "",
    });
  });

  it("coerces typed inputs and rejects invalid ones", () => {
    expect(coerceExtra("integer", "12")).toEqual({ ok: true, value: 12 });
    expect(coerceExtra("integer", "1.5")).toEqual({ ok: false });
    expect(coerceExtra("number", "abc")).toEqual({ ok: false });
    expect(coerceExtra("json", "[1")).toEqual({ ok: false });
    expect(coerceExtra("string", "  ")).toEqual({ ok: true, value: undefined });
    expect(coerceExtra("boolean", false)).toEqual({ ok: true, value: undefined });
  });

  it("builds extra from typed values with the raw JSON on top", () => {
    const values = { ...initialExtras(SLACK), verbose: true, scopes: '["a"]', region: "eu" };
    const r = buildExtra(SLACK, values, '{"region": "us", "other": 1}');
    expect(r.invalid).toEqual([]);
    expect(r.rawInvalid).toBe(false);
    expect(JSON.parse(r.extra ?? "null")).toEqual({
      timeout: 30,
      verbose: true,
      scopes: ["a"],
      region: "us",
      other: 1,
    });
  });

  it("reports invalid inputs without throwing", () => {
    const r = buildExtra(SLACK, { timeout: "x" }, "[1, 2]");
    expect(r.invalid).toEqual(["timeout"]);
    expect(r.rawInvalid).toBe(true);
    expect(buildExtra(undefined, {}, "").extra).toBeUndefined();
    expect(buildExtra(undefined, {}, "{oops").rawInvalid).toBe(true);
  });

  it("finds the metadata of a type", () => {
    expect(metaFor([SLACK], "slack")?.hook_name).toBe("Slack API");
    expect(metaFor([SLACK], "http")).toBeUndefined();
    expect(metaFor(undefined, "slack")).toBeUndefined();
  });
});
