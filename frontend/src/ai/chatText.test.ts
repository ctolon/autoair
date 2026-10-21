import { describe, expect, it } from "vitest";

import { argsLabel, citationHref, citedSources, insertTarget, splitSegments, visibleStreamText } from "./chatText";

describe("splitSegments", () => {
  it("separates Markdown from fenced code blocks", () => {
    const md = "Use this:\n\n```python\nprint('hi')\n```\nThen deploy.";
    expect(splitSegments(md)).toEqual([
      { kind: "text", text: "Use this:\n" },
      { kind: "code", code: "print('hi')", language: "python", open: false },
      { kind: "text", text: "Then deploy." },
    ]);
  });

  it("keeps an unterminated block open while streaming", () => {
    const out = splitSegments("Text\n```bash\necho 1");
    expect(out.at(-1)).toEqual({ kind: "code", code: "echo 1", language: "bash", open: true });
  });

  it("handles tildes and longer fences, and blocks without a language", () => {
    const out = splitSegments("~~~\na\n~~~\n````sql\nselect 1\n````");
    expect(out).toEqual([
      { kind: "code", code: "a", language: null, open: false },
      { kind: "code", code: "select 1", language: "sql", open: false },
    ]);
  });

  it("never throws on odd input", () => {
    for (const s of ["", "```", "```\n```\n```", "\n\n", "``` x y"]) expect(() => splitSegments(s)).not.toThrow();
  });
});

describe("visibleStreamText", () => {
  it("hides the follow-ups block, complete or partial", () => {
    expect(visibleStreamText("Answer.\n<follow_ups>\n- q\n</follow_ups>")).toBe("Answer.");
    expect(visibleStreamText("Answer.\n<follow_ups>\n- q")).toBe("Answer.");
    expect(visibleStreamText("Answer.\n<follo")).toBe("Answer.");
    expect(visibleStreamText("a < b")).toBe("a < b");
  });
});

describe("citations", () => {
  const citations = [
    { n: 1, kind: "doc" as const, title: "Retries", ref: "kb://retries" },
    { n: 2, kind: "web" as const, title: "Docs", ref: "https://airflow.apache.org/x" },
  ];
  it("keeps the cited ones, or all when none is cited", () => {
    expect(citedSources({ content: "See [2].", citations }).map((c) => c.n)).toEqual([2]);
    expect(citedSources({ content: "No refs", citations })).toHaveLength(2);
  });
  it("links web pages only", () => {
    expect(citationHref({ ref: "https://airflow.apache.org/x" })).toBe("https://airflow.apache.org/x");
    expect(citationHref({ ref: "kb://retries" })).toBeNull();
    expect(citationHref({ ref: "javascript:alert(1)" })).toBeNull();
  });
});

describe("insertTarget", () => {
  it("maps languages to the selected node", () => {
    expect(insertTarget("python", { id: "t", type: "python_task" })).toEqual({ kind: "code", nodeId: "t" });
    expect(insertTarget("sql", { id: "t", type: "python_task" })).toBeNull();
    expect(insertTarget("bash", { id: "b", type: "operator", params: { bash_command: "" } })).toEqual({
      kind: "param",
      nodeId: "b",
      param: "bash_command",
    });
    expect(insertTarget("sql", { id: "q", type: "operator", params: {} })).toEqual({
      kind: "param",
      nodeId: "q",
      param: "sql",
    });
    expect(insertTarget("yaml", { id: "q", type: "operator" })).toBeNull();
    expect(insertTarget("python", null)).toBeNull();
  });
});

describe("argsLabel", () => {
  it("summarises scalar arguments and truncates", () => {
    expect(argsLabel({ query: "bash", limit: 5, nested: { a: 1 } })).toBe("query=bash limit=5");
    expect(argsLabel({ q: "x".repeat(200) }, 20)).toHaveLength(20);
    expect(argsLabel(undefined)).toBe("");
  });
});
