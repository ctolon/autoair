import { describe, expect, it } from "vitest";

import { type MarkdownCommand, applyEdit, formatMarkdown } from "./format";

/** Runs a command on `doc` where `[` `]` mark the selection; returns the result with markers. */
function run(command: MarkdownCommand, marked: string): string {
  const from = marked.indexOf("[");
  const to = marked.indexOf("]") - 1;
  const doc = marked.replace("[", "").replace("]", "");
  const edit = formatMarkdown(command, doc, from, to);
  const out = applyEdit(doc, edit);
  return `${out.slice(0, edit.selectionFrom)}[${out.slice(edit.selectionFrom, edit.selectionTo)}]${out.slice(edit.selectionTo)}`;
}

describe("formatMarkdown", () => {
  it("wraps and unwraps bold / italic", () => {
    expect(run("bold", "a [word] b")).toBe("a **[word]** b");
    expect(run("bold", "a **[word]** b")).toBe("a [word] b");
    expect(run("bold", "a [**word**] b")).toBe("a [word] b");
    expect(run("italic", "x[]")).toBe("x_[text]_");
  });

  it("toggles heading, list and quote prefixes on every selected line", () => {
    expect(run("heading", "[Title]")).toBe("[## Title]");
    expect(run("heading", "[### Title]")).toBe("[Title]");
    expect(run("bulletList", "[a\nb]")).toBe("[- a\n- b]");
    expect(run("bulletList", "[- a\n- b]")).toBe("[a\nb]");
    expect(run("numberedList", "[a\n\nb]")).toBe("[1. a\n\n2. b]");
    expect(run("quote", "x\n[y]")).toBe("x\n[> y]");
  });

  it("selects the placeholder on an empty line", () => {
    expect(run("heading", "[]")).toBe("## [Heading]");
    expect(run("bulletList", "a\n[]")).toBe("a\n- [text]");
  });

  it("uses inline code within a line and a fenced block for lines", () => {
    expect(run("code", "run [ls] now")).toBe("run `[ls]` now");
    expect(run("code", "text [a\nb]")).toBe("text \n```\n[a\nb]\n```");
    expect(run("code", "[]")).toBe("```\n[code]\n```");
  });

  it("builds links selecting what is left to fill in", () => {
    expect(run("link", "see [docs]")).toBe("see [docs]([https://])");
    expect(run("link", "[https://airflow.apache.org]")).toBe("[[text]](https://airflow.apache.org)");
  });

  it("inserts a table on its own paragraph", () => {
    const out = run("table", "Intro[]");
    expect(out.startsWith("Intro\n\n| [Column] | Column |\n| ------ | ------ |")).toBe(true);
  });
});
