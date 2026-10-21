import { EditorState, type Extension } from "@codemirror/state";

/**
 * A task body copied out of a function keeps its indentation ("    x = 1"), which Python
 * rejects ("unexpected indent"). When a paste leaves *every* non-blank line of the body
 * indented by a common prefix, that prefix is removed (the server does the same before
 * validating and generating code, see autoair.codegen.body).
 */
export function dedentBody(text: string): string | null {
  const lines = text.split("\n");
  let common: string | null = null;
  for (const line of lines) {
    if (!line.trim()) continue;
    const indent = /^[ \t]*/.exec(line)?.[0] ?? "";
    if (!indent) return null;
    if (common === null) common = indent;
    else {
      let i = 0;
      while (i < common.length && i < indent.length && common[i] === indent[i]) i++;
      common = common.slice(0, i);
    }
    if (!common) return null;
  }
  if (!common) return null;
  const n = common.length;
  return lines.map((line) => (line.trim() ? line.slice(n) : line.slice(Math.min(n, line.length)))).join("\n");
}

/** Dedents the whole task body after a paste that left it uniformly indented. */
export function dedentOnPaste(): Extension {
  return EditorState.transactionFilter.of((tr) => {
    if (!tr.docChanged || !tr.isUserEvent("input.paste")) return tr;
    const next = tr.newDoc.toString();
    const dedented = dedentBody(next);
    if (dedented === null) return tr;
    return [tr, { changes: { from: 0, to: tr.newDoc.length, insert: dedented }, sequential: true }];
  });
}
