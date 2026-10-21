import { EditorState } from "@codemirror/state";
import { describe, expect, it } from "vitest";

import { toCmDiagnostics } from "./CodeEditor";

describe("toCmDiagnostics", () => {
  const state = EditorState.create({ doc: "a = 1\nreturn b\n" });

  it("maps 1-based line/column to document offsets", () => {
    const [d] = toCmDiagnostics(state, [{ line: 2, column: 8, message: "undefined name", severity: "error" }]);
    expect(state.sliceDoc(d?.from, d?.to)).toBe("b");
    expect(d?.severity).toBe("error");
  });

  it("clamps out-of-range lines", () => {
    const [d] = toCmDiagnostics(state, [{ line: 99, message: "x", severity: "warning" }]);
    expect(d?.from).toBe(state.doc.line(state.doc.lines).from);
  });
});
