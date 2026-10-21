import { Box } from "@chakra-ui/react";
import type { CompletionContext, CompletionResult } from "@codemirror/autocomplete";
import { json } from "@codemirror/lang-json";
import { markdown } from "@codemirror/lang-markdown";
import { python } from "@codemirror/lang-python";
import { sql } from "@codemirror/lang-sql";
import { yaml } from "@codemirror/lang-yaml";
import { StreamLanguage, indentUnit, syntaxHighlighting } from "@codemirror/language";
import { java } from "@codemirror/legacy-modes/mode/clike";
import { go } from "@codemirror/legacy-modes/mode/go";
import { javascript, typescript } from "@codemirror/legacy-modes/mode/javascript";
import { jinja2 } from "@codemirror/legacy-modes/mode/jinja2";
import { shell } from "@codemirror/legacy-modes/mode/shell";
import { type Diagnostic, lintGutter, setDiagnostics } from "@codemirror/lint";
import { EditorState, type Extension } from "@codemirror/state";
import { oneDarkHighlightStyle } from "@codemirror/theme-one-dark";
import { Decoration, EditorView, MatchDecorator, ViewPlugin, type ViewUpdate } from "@codemirror/view";
import CodeMirror from "@uiw/react-codemirror";
import { useEffect, useMemo, useRef, useState } from "react";

import { useIsDark } from "../../lifecycle/components/useIsDark";
import { jinjaCompletions } from "../../lib/jinjaCompletion";
import { bashLanguageServices } from "./bashLsp";
import { dedentOnPaste } from "./dedent";
import { defaultIndentUnit, detectIndentUnit } from "./indent";
import { useLanguageServer } from "./lspSupport";
import { markdownCompletions } from "./markdownCompletion";
import { pythonLanguageServices, setPythonContext } from "./pythonLsp";
import type {
  CodeDiagnostic,
  CodeEditorHandle,
  CodeEditorProps,
  CodeLanguage,
  JinjaCompletionData,
  PythonLspContext,
} from "./types";

/** Server diagnostics (line/column) → CodeMirror ranges; out-of-range lines are clamped. */
export function toCmDiagnostics(state: EditorState, items: CodeDiagnostic[]): Diagnostic[] {
  const doc = state.doc;
  return items.map((d) => {
    const line = doc.line(Math.min(Math.max(1, d.line), doc.lines));
    const from = Math.min(line.from + Math.max(0, (d.column ?? 1) - 1), line.to);
    return { from, to: from < line.to ? line.to : from, severity: d.severity, message: d.message };
  });
}

function languageExtension(language: CodeLanguage, ide = false): Extension[] {
  switch (language) {
    case "python":
      return [python()];
    case "sql":
      return [sql()];
    case "yaml":
      return [yaml()];
    case "json":
      return [json()];
    case "bash":
      // Completion (keywords, commands, $VARS, snippets) + ShellCheck diagnostics.
      // With a language server (bash-language-server) it provides both instead.
      return [StreamLanguage.define(shell), ...(ide ? [] : bashLanguageServices())];
    case "jinja":
      return [StreamLanguage.define(jinja2)];
    case "go":
      return [StreamLanguage.define(go)];
    case "java":
      return [StreamLanguage.define(java)];
    case "javascript":
      return [StreamLanguage.define(javascript)];
    case "typescript":
      return [StreamLanguage.define(typescript)];
    case "markdown":
      // Enter continues lists/quotes; `/` offers block and inline snippets.
      return [markdown(), EditorState.languageData.of(() => [{ autocomplete: markdownCompletions }])];
    default:
      return [];
  }
}

/** Highlights `{{ … }}`, `{% … %}` and `{# … #}` on top of any language mode. */
const jinjaMarks = new MatchDecorator({
  regexp: /\{\{[\s\S]*?\}\}|\{%[\s\S]*?%\}|\{#[\s\S]*?#\}/g,
  decoration: (m) => Decoration.mark({ class: m[0].startsWith("{#") ? "cm-jinja-comment" : "cm-jinja" }),
});

const jinjaOverlay = ViewPlugin.fromClass(
  class {
    decorations;
    constructor(view: EditorView) {
      this.decorations = jinjaMarks.createDeco(view);
    }
    update(u: ViewUpdate) {
      this.decorations = jinjaMarks.updateDeco(u, this.decorations);
    }
  },
  { decorations: (v) => v.decorations },
);

const DEFAULT_LSP: PythonLspContext = { functionName: "task_fn", arguments: [], usesLoopContext: false };

const theme = EditorView.theme({
  "&": {
    fontSize: "12px",
    backgroundColor: "var(--chakra-colors-bg, transparent)",
    color: "var(--chakra-colors-fg, inherit)",
  },
  "&.cm-focused": { outline: "none" },
  ".cm-content": {
    fontFamily: "var(--chakra-fonts-mono, monospace)",
    caretColor: "var(--chakra-colors-fg, currentColor)",
  },
  ".cm-scroller": { fontFamily: "var(--chakra-fonts-mono, monospace)", lineHeight: "1.5" },
  ".cm-gutters": {
    backgroundColor: "var(--chakra-colors-bg-muted, #f4f4f5)",
    color: "var(--chakra-colors-fg-muted, #71717a)",
    borderRight: "1px solid var(--chakra-colors-border-emphasized, #d4d4d8)",
  },
  ".cm-foldGutter span": { color: "var(--chakra-colors-fg-muted, #71717a)" },
  ".cm-activeLine": { backgroundColor: "color-mix(in srgb, var(--chakra-colors-bg-muted, #eee) 50%, transparent)" },
  ".cm-activeLineGutter": {
    backgroundColor: "var(--chakra-colors-bg-emphasized, #e4e4e7)",
    color: "var(--chakra-colors-fg, inherit)",
  },
  ".cm-placeholder": { color: "var(--chakra-colors-fg-subtle, #999)" },
  ".cm-jinja": {
    backgroundColor: "color-mix(in srgb, var(--chakra-colors-purple-500, #805ad5) 14%, transparent)",
    borderRadius: "3px",
  },
  ".cm-jinja-comment": { color: "var(--chakra-colors-fg-muted, #888)", fontStyle: "italic" },
  ".cm-tooltip": {
    backgroundColor: "var(--chakra-colors-bg-panel, #fff)",
    color: "var(--chakra-colors-fg, inherit)",
    border: "1px solid var(--chakra-colors-border, #ddd)",
    borderRadius: "6px",
  },
  ".cm-tooltip-autocomplete > ul > li[aria-selected]": {
    // Not color-palette-solid: in dark mode the gray palette's solid colour is white.
    backgroundColor: "var(--chakra-colors-blue-600, #2563eb)",
    color: "white",
  },
  // Long signatures are cut (the docs panel shows them in full): the list stays near the cursor.
  ".cm-completionDetail": {
    opacity: 0.7,
    marginLeft: "0.75em",
    display: "inline-block",
    maxWidth: "32ch",
    overflow: "hidden",
    textOverflow: "ellipsis",
    whiteSpace: "nowrap",
    verticalAlign: "bottom",
  },
  // Lint tooltips/panel: themed, severity-coloured, readable in dark mode.
  ".cm-tooltip-lint": { padding: "2px 0", maxWidth: "46ch" },
  ".cm-diagnostic": {
    fontSize: "12px",
    padding: "4px 8px 4px 10px",
    borderLeftWidth: "3px",
    fontFamily: "var(--chakra-fonts-body, sans-serif)",
  },
  ".cm-diagnostic-error": { borderLeftColor: "var(--chakra-colors-red-500, #e53e3e)" },
  ".cm-diagnostic-warning": { borderLeftColor: "var(--chakra-colors-orange-400, #ed8936)" },
  ".cm-diagnostic-info": { borderLeftColor: "var(--chakra-colors-blue-400, #4299e1)" },
  ".cm-diagnosticSource": { opacity: 0.65, fontSize: "11px", marginLeft: "0.5em" },
  ".cm-lintRange-error": {
    backgroundImage: "none",
    textDecoration: "underline wavy var(--chakra-colors-red-500, #e53e3e)",
    textUnderlineOffset: "3px",
  },
  ".cm-lintRange-warning": {
    backgroundImage: "none",
    textDecoration: "underline wavy var(--chakra-colors-orange-400, #ed8936)",
    textUnderlineOffset: "3px",
  },
  ".cm-lintRange-info": {
    backgroundImage: "none",
    textDecoration: "underline dotted var(--chakra-colors-blue-400, #4299e1)",
    textUnderlineOffset: "3px",
  },
  ".cm-selectionBackground, &.cm-focused .cm-selectionBackground, ::selection": {
    backgroundColor: "color-mix(in srgb, var(--chakra-colors-blue-500, #3b82f6) 30%, transparent) !important",
  },
});

/** One Dark token colours on the Chakra dark background (the light default is unreadable there). */
const darkHighlight = [syntaxHighlighting(oneDarkHighlightStyle), EditorView.theme({}, { dark: true })];

/** Editor backed by CodeMirror 6 (default export: loaded lazily via ./index). */
export default function CodeEditor({
  value,
  onChange,
  language,
  jinja,
  ariaLabel,
  minHeight,
  maxHeight,
  height,
  readOnly,
  placeholder,
  invalid,
  compact,
  onFocus,
  onBlur,
  onReady,
  diagnostics,
  pythonLsp,
  focusHandoffRef,
}: CodeEditorProps) {
  const withLsp = !!pythonLsp && language === "python";
  const dark = useIsDark();
  const viewRef = useRef<EditorView | null>(null);
  const lspKey = pythonLsp ? JSON.stringify(pythonLsp) : "";
  // features.ide_language_server: a real language server replaces the built-in services.
  const ide = useLanguageServer(withLsp ? "python" : language === "bash" ? "bash" : null);
  // Keep the editor's Python context (arguments, loop) in sync without rebuilding it.
  useEffect(() => {
    const view = viewRef.current;
    if (!view || !lspKey) return;
    view.dispatch({ effects: setPythonContext.of(JSON.parse(lspKey) as PythonLspContext) });
  }, [lspKey]);
  const withLint = diagnostics !== undefined;
  const diagnosticsRef = useRef(diagnostics);
  useEffect(() => {
    diagnosticsRef.current = diagnostics;
  }, [diagnostics]);
  // Push server diagnostics into the editor (no lint source: results come from the API).
  useEffect(() => {
    const view = viewRef.current;
    if (!view || diagnostics === undefined) return;
    view.dispatch(setDiagnostics(view.state, toCmDiagnostics(view.state, diagnostics)));
  }, [diagnostics]);
  // Completion data changes often (store updates): read it through a ref so the
  // extensions (and the editor state) are not rebuilt.
  const dataRef = useRef<JinjaCompletionData | null | undefined>(jinja);
  useEffect(() => {
    dataRef.current = jinja;
  }, [jinja]);
  const withJinja = !!jinja;
  // Enter / Tab indent like the text already does (4 spaces for Python by default, not
  // CodeMirror's 2). Detected once the text has an indented line, then kept while typing.
  const [detectedIndent, setDetectedIndent] = useState<string | null>(() => detectIndentUnit(value));
  if (detectedIndent === null && value) {
    const found = detectIndentUnit(value);
    if (found) setDetectedIndent(found);
  }
  const unit = detectedIndent ?? defaultIndentUnit(language);

  const extensions = useMemo(() => {
    const ext: Extension[] = [
      theme,
      ...(dark ? darkHighlight : []),
      ...languageExtension(language, !!ide),
      indentUnit.of(unit),
      EditorView.contentAttributes.of({ "aria-label": ariaLabel }),
    ];
    if (language === "jinja" || language === "text" || language === "markdown" || compact)
      ext.push(EditorView.lineWrapping);
    if (withLsp) {
      // Start with the current context (a reconfigure must not fall back to the default).
      const context = lspKey ? (JSON.parse(lspKey) as PythonLspContext) : DEFAULT_LSP;
      if (ide) {
        ext.push(ide.languageServerExtension("python", ide.documentUri("python", context)));
      } else ext.push(...pythonLanguageServices(context));
      // A task body pasted with its function's indentation is dedented.
      if ((context.mode ?? "task_body") === "task_body") ext.push(dedentOnPaste());
    }
    if (ide && language === "bash") ext.push(ide.languageServerExtension("bash", ide.documentUri("bash")));
    if (withLint) {
      ext.push(lintGutter());
    }
    if (withJinja) {
      ext.push(jinjaOverlay);
      // A stable source: a new function per lookup makes CodeMirror restart its query.
      const jinjaData = [
        {
          autocomplete: (ctx: CompletionContext): CompletionResult | null => {
            const data = dataRef.current;
            if (!data) return null;
            const start = Math.max(0, ctx.pos - 2000);
            const result = jinjaCompletions(ctx.state.sliceDoc(start, ctx.pos), data, ctx.explicit);
            if (!result) return null;
            return {
              from: start + result.from,
              options: result.options.map((o) => ({
                label: o.label,
                type: o.type,
                ...(o.detail ? { detail: o.detail } : {}),
                ...(o.info ? { info: o.info } : {}),
                ...(o.apply ? { apply: o.apply } : {}),
              })),
              validFor: /^\w*$/,
            };
          },
        },
      ];
      ext.push(EditorState.languageData.of(() => jinjaData));
    }
    return ext;
  }, [language, ariaLabel, compact, withJinja, withLint, withLsp, dark, lspKey, ide, unit]);

  const handle: CodeEditorHandle = useMemo(
    () => ({
      insert: (text: string) => {
        const view = viewRef.current;
        if (!view) return;
        view.dispatch(view.state.replaceSelection(text), { scrollIntoView: true, userEvent: "input" });
        view.focus();
      },
      focus: () => viewRef.current?.focus(),
      edit: (fn) => {
        const view = viewRef.current;
        if (!view) return;
        const sel = view.state.selection.main;
        const e = fn(view.state.doc.toString(), sel.from, sel.to);
        view.dispatch({
          changes: { from: e.from, to: e.to, insert: e.insert },
          selection: { anchor: e.selectionFrom, head: e.selectionTo },
          scrollIntoView: true,
          userEvent: "input",
        });
        view.focus();
      },
    }),
    [],
  );

  return (
    <Box
      w="100%"
      h={height === "100%" ? "100%" : undefined}
      css={height === "100%" ? { "& > div": { height: "100%" } } : undefined}
      borderWidth="1px"
      borderColor={invalid ? "border.error" : "border"}
      rounded="md"
      overflow="hidden"
      _focusWithin={{ borderColor: invalid ? "border.error" : "blue.500" }}
      onFocus={() => onFocus?.(handle)}
      onBlur={(e) => {
        if (!e.currentTarget.contains(e.relatedTarget as Node | null)) onBlur?.();
      }}
    >
      <CodeMirror
        value={value}
        onChange={onChange}
        theme="none"
        extensions={extensions}
        readOnly={readOnly ?? false}
        editable={!readOnly}
        placeholder={placeholder}
        {...(height
          ? { height }
          : { minHeight: minHeight ?? (compact ? undefined : "4rem"), maxHeight: maxHeight ?? "24rem" })}
        indentWithTab={!compact}
        basicSetup={{
          lineNumbers: !compact,
          foldGutter: !compact,
          highlightActiveLine: !compact,
          highlightActiveLineGutter: !compact,
          autocompletion: true,
          closeBrackets: true,
          searchKeymap: !compact,
        }}
        onCreateEditor={(view) => {
          viewRef.current = view;
          // The user was typing in the loading placeholder: carry on where they were.
          const handoff = focusHandoffRef?.current;
          if (handoff && focusHandoffRef) {
            focusHandoffRef.current = null;
            const active = document.activeElement;
            if (!active || active === document.body) {
              const len = view.state.doc.length;
              view.dispatch({
                selection: { anchor: Math.min(handoff.anchor, len), head: Math.min(handoff.head, len) },
              });
              view.focus();
            }
          }
          onReady?.(handle);
          // Props given before the view existed (the effects above found no view).
          if (diagnosticsRef.current !== undefined)
            view.dispatch(setDiagnostics(view.state, toCmDiagnostics(view.state, diagnosticsRef.current)));
        }}
      />
    </Box>
  );
}
