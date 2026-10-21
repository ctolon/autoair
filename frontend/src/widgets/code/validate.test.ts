import { describe, expect, it } from "vitest";

import type { ValidateResult } from "../../api/gen/types.gen";
import { summarize, toCodeDiagnostics, validateLanguageOf } from "./validate";

const result = (over: Partial<ValidateResult>): ValidateResult => ({
  language: "python",
  ok: true,
  validated: true,
  tools: [{ name: "ast", available: true, ran: true, note: null }],
  diagnostics: [],
  ...over,
});

describe("code validation helpers", () => {
  it("maps editor languages to validator languages", () => {
    expect(validateLanguageOf("python")).toBe("python");
    expect(validateLanguageOf("typescript")).toBe("typescript");
    expect(validateLanguageOf("jinja")).toBeNull();
    expect(validateLanguageOf("text")).toBeNull();
  });

  it("summarises errors, warnings and tools", () => {
    const r = result({
      ok: false,
      tools: [
        { name: "ast", available: true, ran: true, note: null },
        { name: "ruff", available: false, ran: false, note: "ruff is not installed on the server" },
      ],
      diagnostics: [
        { line: 1, column: 2, severity: "error", message: "bad", source: "ast" },
        { line: 3, severity: "warning", message: "meh", code: "F841", source: "ruff" },
      ],
    });
    expect(summarize(r)).toEqual({
      status: "errors",
      errors: 1,
      warnings: 1,
      ran: ["ast"],
      missing: [{ name: "ruff", note: "ruff is not installed on the server" }],
    });
    expect(summarize(result({})).status).toBe("ok");
    expect(summarize(result({ validated: false, tools: [] })).status).toBe("unavailable");
  });

  it("turns diagnostics into editor markers naming the tool", () => {
    const [d] = toCodeDiagnostics(
      result({
        diagnostics: [{ line: 4, column: 7, severity: "warning", message: "x", code: "SC2086", source: "shellcheck" }],
      }),
    );
    expect(d).toEqual({ line: 4, column: 7, severity: "warning", message: "x (shellcheck SC2086)" });
  });
});
