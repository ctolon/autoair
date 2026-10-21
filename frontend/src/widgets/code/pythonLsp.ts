import type { Completion, CompletionContext, CompletionResult } from "@codemirror/autocomplete";
import { EditorState, type Extension, StateEffect, StateField } from "@codemirror/state";
import {
  type EditorView,
  type Tooltip,
  ViewPlugin,
  type ViewUpdate,
  hoverTooltip,
  showTooltip,
} from "@codemirror/view";

import { pythonComplete, pythonHover, pythonSignatures } from "../../api/gen/sdk.gen";
import type { SignatureInfo } from "../../api/gen/types.gen";

import type { PythonLspContext } from "./types";

const DEFAULT_CONTEXT: PythonLspContext = { functionName: "task_fn", arguments: [], usesLoopContext: false };

/** Dispatch to update the task's function context (arguments, loop context). */
export const setPythonContext = StateEffect.define<PythonLspContext>();

/** The current function context lives in the editor state (no React refs involved). */
const pythonContextField = StateField.define<PythonLspContext>({
  create: () => DEFAULT_CONTEXT,
  update: (value, tr) => tr.effects.reduce((v, e) => (e.is(setPythonContext) ? e.value : v), value),
});

const contextOf = (state: EditorState): PythonLspContext => state.field(pythonContextField, false) ?? DEFAULT_CONTEXT;

const TYPE_MAP: Record<string, string> = {
  function: "function",
  class: "class",
  module: "namespace",
  keyword: "keyword",
  variable: "variable",
  property: "property",
  param: "variable",
  text: "text",
};

function position(state: EditorView["state"], pos: number): { line: number; column: number } {
  const line = state.doc.lineAt(pos);
  return { line: line.number, column: pos - line.from };
}

function body(code: string, pos: { line: number; column: number }, c: PythonLspContext) {
  return {
    code,
    line: pos.line,
    column: pos.column,
    arguments: c.arguments,
    uses_loop_context: c.usesLoopContext,
    function_name: c.functionName,
    mode: c.mode ?? "task_body",
  };
}

/** Minimal markdown (code fences + text) → DOM for hover tooltips; never uses innerHTML. */
function renderDoc(markdown: string): HTMLElement {
  const root = document.createElement("div");
  Object.assign(root.style, {
    maxWidth: "560px",
    maxHeight: "320px",
    overflow: "auto",
    padding: "6px 8px",
    fontSize: "12px",
    whiteSpace: "pre-wrap",
  });
  markdown.split(/```(?:python)?\n?/).forEach((part, i) => {
    if (!part.trim()) return;
    const el = document.createElement(i % 2 ? "code" : "div");
    if (i % 2) Object.assign(el.style, { display: "block", fontFamily: "monospace", marginBottom: "6px" });
    el.textContent = part.trim();
    root.appendChild(el);
  });
  return root;
}

/** CodeMirror `boost` (-99…99) preserving the server's ranking of completion items. */
export function serverBoost(index: number): number {
  return Math.max(-99, 50 - index);
}

/** Completion source backed by the server's jedi analysis (Airflow & providers aware). */
function pythonCompletionSource() {
  return async (ctx: CompletionContext): Promise<CompletionResult | null> => {
    const word = ctx.matchBefore(/[\w.]*/);
    if (!ctx.explicit && (!word || word.from === word.to)) return null;
    try {
      const { data } = await pythonComplete({
        body: body(ctx.state.doc.toString(), position(ctx.state, ctx.pos), contextOf(ctx.state)),
        throwOnError: true,
      });
      if (ctx.aborted || !data.items.length) return null;
      const options: Completion[] = data.items.map((it, i) => ({
        label: it.label,
        type: TYPE_MAP[it.type] ?? "text",
        // The server ranks keywords and prefix matches first: keep its order.
        boost: serverBoost(i),
        ...(it.detail ? { detail: it.detail.length > 80 ? `${it.detail.slice(0, 77)}…` : it.detail } : {}),
        ...(it.info ? { info: it.info } : {}),
      }));
      return { from: ctx.pos - (data.prefix_length ?? 0), options, validFor: /^\w*$/ };
    } catch {
      return null;
    }
  };
}

/** Hover documentation (signature + docstring) for the symbol under the pointer. */
function pythonHoverTooltip(): Extension {
  return hoverTooltip(async (view, pos, side) => {
    // Only identifiers: jedi answers punctuation/operators with generic language docs.
    const around = view.state.sliceDoc(Math.max(0, pos - 1), pos + 1);
    if (!/\w/.test(side < 0 ? around.charAt(0) : around.charAt(around.length - 1))) return null;
    try {
      const { data } = await pythonHover({
        body: body(view.state.doc.toString(), position(view.state, pos), contextOf(view.state)),
        throwOnError: true,
      });
      const markdown = data.markdown;
      if (!markdown) return null;
      return { pos, above: true, create: () => ({ dom: renderDoc(markdown) }) };
    } catch {
      return null;
    }
  });
}

/** True when the cursor sits inside an open call on the current line, e.g. `foo(a, |`. */
export function insideCall(lineTextBeforeCursor: string): boolean {
  let depth = 0;
  for (const ch of lineTextBeforeCursor.replace(/(["'])(?:\\.|(?!\1).)*\1/g, "")) {
    if (ch === "(") depth++;
    else if (ch === ")") depth = Math.max(0, depth - 1);
  }
  return depth > 0;
}

/** Signature label with the active parameter wrapped in «…» (plain text, no HTML). */
export function highlightParameter(sig: SignatureInfo): string {
  const index = sig.active_parameter;
  const open = sig.label.indexOf("(");
  const close = sig.label.lastIndexOf(")");
  if (index == null || open < 0 || close < open) return sig.label;
  const params: string[] = [];
  let depth = 0;
  let start = open + 1;
  for (let i = open + 1; i < close; i++) {
    const ch = sig.label[i];
    if (ch === "[" || ch === "(" || ch === "{") depth++;
    else if (ch === "]" || ch === ")" || ch === "}") depth--;
    else if (ch === "," && depth === 0) {
      params.push(sig.label.slice(start, i));
      start = i + 1;
    }
  }
  params.push(sig.label.slice(start, close));
  if (!params[index]) return sig.label;
  return (
    sig.label.slice(0, open + 1) +
    params.map((p, i) => (i === index ? `«${p.trim()}»` : p.trim())).join(", ") +
    sig.label.slice(close)
  );
}

const setSignature = StateEffect.define<Tooltip | null>();
const signatureField = StateField.define<Tooltip | null>({
  create: () => null,
  update: (value, tr) => {
    for (const e of tr.effects) if (e.is(setSignature)) return e.value;
    return tr.docChanged && value ? { ...value, pos: tr.changes.mapPos(value.pos) } : value;
  },
  provide: (f) => showTooltip.from(f),
});

/** Signature help while typing call arguments (debounced server request). */
const signatureHelp = ViewPlugin.fromClass(
  class {
    private timer: ReturnType<typeof setTimeout> | undefined;
    private seq = 0;
    constructor(private readonly view: EditorView) {}
    update(u: ViewUpdate) {
      if (!u.docChanged && !u.selectionSet) return;
      clearTimeout(this.timer);
      const { state } = u.view;
      const pos = state.selection.main.head;
      const line = state.doc.lineAt(pos);
      if (!u.view.hasFocus || !insideCall(line.text.slice(0, pos - line.from))) {
        if (state.field(signatureField)) queueMicrotask(() => this.view.dispatch({ effects: setSignature.of(null) }));
        return;
      }
      const seq = ++this.seq;
      this.timer = setTimeout(() => void this.request(pos, seq), 200);
    }
    async request(pos: number, seq: number) {
      const { state } = this.view;
      try {
        const { data } = await pythonSignatures({
          body: body(state.doc.toString(), position(state, pos), contextOf(state)),
          throwOnError: true,
        });
        if (seq !== this.seq) return;
        const sig = data.signatures[0];
        const tooltip: Tooltip | null = sig
          ? {
              pos,
              above: true,
              create: () => {
                const dom = document.createElement("div");
                Object.assign(dom.style, {
                  padding: "4px 8px",
                  fontFamily: "monospace",
                  fontSize: "12px",
                  maxWidth: "560px",
                  whiteSpace: "pre-wrap",
                });
                dom.className = "cm-autoair-signature";
                dom.textContent = highlightParameter(sig);
                return { dom };
              },
            }
          : null;
        this.view.dispatch({ effects: setSignature.of(tooltip) });
      } catch {
        /* the language service is optional */
      }
    }
    destroy() {
      clearTimeout(this.timer);
    }
  },
);

/** Completion, hover and signature help backed by the server's Python language services. */
export function pythonLanguageServices(initial: PythonLspContext): Extension[] {
  const completionData = [{ autocomplete: pythonCompletionSource() }];
  return [
    pythonContextField.init(() => initial),
    pythonHoverTooltip(),
    signatureField,
    signatureHelp,
    // One source object: a new function per languageData lookup makes CodeMirror restart the
    // (asynchronous) query on every update — an endless request loop without any popup.
    EditorState.languageData.of(() => completionData),
  ];
}
