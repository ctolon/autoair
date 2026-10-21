import { describe, expect, it } from "vitest";

import {
  cleanToolset,
  isLlmDecorator,
  isLlmSchema,
  llmDecoratorKwargs,
  newOutputModel,
  outputModelErrors,
  outputModelPreview,
  timedelta,
  timedeltaSeconds,
} from "./llm";

describe("common.ai helpers", () => {
  it("previews the generated pydantic class", () => {
    const preview = outputModelPreview({
      $type: "output_model",
      name: "Sentiment",
      description: "Sentiment of a text",
      fields: [
        { name: "label", choices: ["positive", "negative"], description: "Overall" },
        { name: "score", type: "float" },
        { name: "keywords", type: "list[str]", required: false },
      ],
    });
    expect(preview).toBe(
      [
        "class Sentiment(BaseModel):",
        '    "Sentiment of a text"',
        '    label: Literal["positive", "negative"] = Field(description="Overall")',
        "    score: float",
        "    keywords: list[str] | None = Field(default=None)",
      ].join("\n"),
    );
  });

  it("reports the problems the server would reject", () => {
    expect(outputModelErrors(newOutputModel())).toEqual([]);
    expect(
      outputModelErrors({
        $type: "output_model",
        name: "lower",
        fields: [{ name: "a" }, { name: "a" }, { name: "1x" }, { name: "n", type: "int", choices: ["x"] }],
      }),
    ).toEqual(["name", "fields.1.name", "fields.2.name", "fields.3.choices"]);
    expect(outputModelErrors({ $type: "output_model", name: "Empty", fields: [] })).toEqual(["fields"]);
  });

  it("keeps only the options of the toolset kind", () => {
    expect(
      cleanToolset({
        $type: "toolset",
        kind: "mcp",
        conn_id: "m",
        max_rows: 5,
        allowed_tables: ["t"],
        tool_prefix: "d",
      }),
    ).toEqual({ $type: "toolset", kind: "mcp", conn_id: "m", tool_prefix: "d" });
    expect(
      cleanToolset({
        $type: "toolset",
        kind: "sql",
        conn_id: "db",
        allowed_tables: [],
        allow_writes: false,
        tool_prefix: "x",
      }),
    ).toEqual({ $type: "toolset", kind: "sql", conn_id: "db" });
  });

  it("detects LLM tasks and their decorator kwargs", () => {
    expect(isLlmSchema({ prompt: {}, llm_conn_id: {} })).toBe(true);
    expect(isLlmSchema({ prompt: {} })).toBe(false);
    expect(isLlmDecorator("agent")).toBe(true);
    expect(isLlmDecorator("task")).toBe(false);
    expect(llmDecoratorKwargs("agent")).toContain("toolsets");
    expect(llmDecoratorKwargs("llm")).toContain("require_approval");
    expect(llmDecoratorKwargs("llm")).not.toContain("toolsets");
    expect(llmDecoratorKwargs("llm_sql")).toContain("db_conn_id");
  });

  it("round-trips timedeltas", () => {
    expect(timedeltaSeconds(timedelta(90))).toBe(90);
    expect(timedeltaSeconds(30)).toBe(30);
    expect(timedeltaSeconds("x")).toBeNull();
    expect(timedelta(null)).toBeUndefined();
  });
});
