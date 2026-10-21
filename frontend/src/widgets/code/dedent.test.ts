import { EditorState } from "@codemirror/state";
import { describe, expect, it } from "vitest";

import { dedentBody, dedentOnPaste } from "./dedent";

describe("dedentBody", () => {
  it("removes a common indentation", () => {
    expect(dedentBody("    x = 1\n\n    if x:\n        return x")).toBe("x = 1\n\nif x:\n    return x");
    expect(dedentBody("\tx = 1\n\ty = 2")).toBe("x = 1\ny = 2");
  });

  it("leaves bodies with an unindented line alone", () => {
    expect(dedentBody("x = 1\n    y")).toBeNull();
    expect(dedentBody("")).toBeNull();
    expect(dedentBody("  a\n\tb")).toBeNull();
  });
});

describe("dedentOnPaste", () => {
  const paste = (doc: string, text: string) => {
    const state = EditorState.create({ doc, extensions: [dedentOnPaste()] });
    return state.update({ changes: { from: doc.length, insert: text }, userEvent: "input.paste" }).state.doc.toString();
  };

  it("dedents a pasted indented body", () => {
    expect(paste("", "    import json\n    return json.dumps({})\n")).toBe("import json\nreturn json.dumps({})\n");
  });

  it("keeps pastes into an indented block", () => {
    expect(paste("if x:\n", "    y = 1")).toBe("if x:\n    y = 1");
  });

  it("ignores typing", () => {
    const state = EditorState.create({ doc: "", extensions: [dedentOnPaste()] });
    expect(state.update({ changes: { from: 0, insert: "    " }, userEvent: "input.type" }).state.doc.toString()).toBe(
      "    ",
    );
  });
});
