import { python } from "@codemirror/lang-python";
import { defaultHighlightStyle, syntaxHighlighting } from "@codemirror/language";
import { EditorState } from "@codemirror/state";
import { oneDarkHighlightStyle } from "@codemirror/theme-one-dark";
import { EditorView, lineNumbers } from "@codemirror/view";

import { i18n } from "../../i18n";

const PEEK_ID = "autoair-lsp-peek";

/** `file:///…/site-packages/airflow/sdk/__init__.py` → `airflow/sdk/__init__.py` (best effort). */
export function peekTitle(uri: string): string {
  const path = decodeURIComponent(uri.replace(/^file:\/\//, ""));
  const roots = /\/(?:site-packages|dist-packages|stdlib|lib\/python3\.\d+)(?=\/)/g;
  let start = -1;
  for (const m of path.matchAll(roots)) start = m.index + m[0].length + 1;
  return start >= 0 ? path.slice(start) : path.split("/").slice(-3).join("/");
}

let onKey: ((e: KeyboardEvent) => void) | null = null;

function closePeek(): void {
  document.getElementById(PEEK_ID)?.remove();
  if (onKey) document.removeEventListener("keydown", onKey, true);
  onKey = null;
}

/**
 * Read-only "peek" of a library file (go-to-definition outside the edited document): a panel
 * over the page with the file scrolled to the definition. Escape or the close button closes it.
 */
export function openPeek(uri: string, text: string): EditorView {
  closePeek();
  const panel = document.createElement("div");
  panel.id = PEEK_ID;
  panel.setAttribute("role", "dialog");
  panel.setAttribute("aria-label", peekTitle(uri));
  panel.className = "autoair-lsp-peek";
  Object.assign(panel.style, {
    position: "fixed",
    right: "24px",
    bottom: "24px",
    width: "min(760px, calc(100vw - 48px))",
    height: "min(420px, calc(100vh - 96px))",
    zIndex: "2000",
    display: "flex",
    flexDirection: "column",
    background: "var(--chakra-colors-bg-panel, #fff)",
    color: "var(--chakra-colors-fg, #111)",
    border: "1px solid var(--chakra-colors-border-emphasized, #ccc)",
    borderRadius: "8px",
    boxShadow: "0 12px 32px rgba(0,0,0,0.25)",
    overflow: "hidden",
  });
  const header = document.createElement("div");
  Object.assign(header.style, {
    display: "flex",
    alignItems: "center",
    gap: "8px",
    padding: "6px 10px",
    fontSize: "12px",
    fontFamily: "var(--chakra-fonts-mono, monospace)",
    borderBottom: "1px solid var(--chakra-colors-border, #ddd)",
    background: "var(--chakra-colors-bg-muted, #f4f4f5)",
  });
  const title = document.createElement("span");
  title.textContent = peekTitle(uri);
  title.title = decodeURIComponent(uri);
  Object.assign(title.style, { flex: "1", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" });
  const close = document.createElement("button");
  close.type = "button";
  close.textContent = "×";
  close.setAttribute("aria-label", i18n.t("common:close"));
  Object.assign(close.style, {
    border: "none",
    background: "transparent",
    color: "inherit",
    cursor: "pointer",
    fontSize: "18px",
    lineHeight: "1",
  });
  close.addEventListener("click", closePeek);
  header.append(title, close);
  const host = document.createElement("div");
  Object.assign(host.style, { flex: "1", minHeight: "0", overflow: "hidden" });
  panel.append(header, host);
  document.body.appendChild(panel);
  const root = document.documentElement;
  const dark = root.classList.contains("dark") || root.dataset.theme === "dark" || root.style.colorScheme === "dark";
  const view = new EditorView({
    parent: host,
    state: EditorState.create({
      doc: text,
      extensions: [
        EditorState.readOnly.of(true),
        EditorView.editable.of(false),
        lineNumbers(),
        python(),
        syntaxHighlighting(dark ? oneDarkHighlightStyle : defaultHighlightStyle),
        EditorView.theme(
          {
            "&": { height: "100%", fontSize: "12px", backgroundColor: "transparent" },
            ".cm-scroller": { fontFamily: "var(--chakra-fonts-mono, monospace)" },
            ".cm-gutters": {
              backgroundColor: "var(--chakra-colors-bg-muted, #f4f4f5)",
              color: "var(--chakra-colors-fg-muted, #71717a)",
              border: "none",
            },
          },
          { dark },
        ),
      ],
    }),
  });
  // Escape closes the peek wherever the focus is (the jump may leave it in the editor).
  onKey = (e: KeyboardEvent) => {
    if (e.key !== "Escape") return;
    e.stopPropagation();
    closePeek();
  };
  document.addEventListener("keydown", onKey, true);
  // jumpToDefinition then selects and scrolls to the definition in this view.
  queueMicrotask(() => view.focus());
  return view;
}
