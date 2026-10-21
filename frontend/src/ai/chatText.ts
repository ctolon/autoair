import type { ChatMessage, Citation } from "../api/gen/types.gen";

/** One rendered piece of an answer: Markdown text or a fenced code block. */
export type Segment =
  { kind: "text"; text: string } | { kind: "code"; code: string; language: string | null; open: boolean };

const FENCE = /^(\s*)(`{3,}|~{3,})\s*([\w+#.-]*)\s*$/;

/**
 * Splits Markdown into text and fenced code blocks so code blocks can get their own
 * actions (copy / insert). An unterminated fence (still streaming) becomes an open block.
 */
export function splitSegments(markdown: string): Segment[] {
  const out: Segment[] = [];
  const lines = markdown.split("\n");
  let text: string[] = [];
  let code: string[] | null = null;
  let fence = "";
  let language: string | null = null;
  const flushText = () => {
    const joined = text.join("\n");
    if (joined.trim()) out.push({ kind: "text", text: joined });
    text = [];
  };
  for (const line of lines) {
    const m = FENCE.exec(line);
    if (code === null) {
      if (m) {
        flushText();
        code = [];
        fence = m[2] ?? "```";
        language = m[3] ? m[3].toLowerCase() : null;
      } else {
        text.push(line);
      }
    } else if (m && (m[2] ?? "").startsWith(fence[0] ?? "`") && (m[2] ?? "").length >= fence.length && !m[3]) {
      out.push({ kind: "code", code: code.join("\n"), language, open: false });
      code = null;
    } else {
      code.push(line);
    }
  }
  if (code !== null) out.push({ kind: "code", code: code.join("\n"), language, open: true });
  else flushText();
  return out;
}

const FOLLOW_UPS = /<follow_ups>[\s\S]*?(<\/follow_ups>|$)/i;

/** Streamed text without the trailing `<follow_ups>` block (also while it is incomplete). */
export function visibleStreamText(text: string): string {
  const tag = "<follow_ups>";
  const last = text.lastIndexOf("<");
  const tail = last >= 0 ? text.slice(last).toLowerCase() : "";
  const cut = tail && tail.length < tag.length && tag.startsWith(tail) ? text.slice(0, last) : text;
  return cut.replace(FOLLOW_UPS, "").trimEnd();
}

/** The citations an answer refers to as [n] (all of them when it cites none). */
export function citedSources(message: Pick<ChatMessage, "content" | "citations">): Citation[] {
  const all = message.citations ?? [];
  const used = all.filter((c) => message.content.includes(`[${c.n}]`));
  return used.length ? used : all;
}

/** A link for a citation when it points to something openable (web pages). */
export function citationHref(c: Pick<Citation, "ref">): string | null {
  return /^https?:\/\//.test(c.ref) ? c.ref : null;
}

/** Where a code block can be inserted in the selected node, by language. */
type InsertTarget = { kind: "code"; nodeId: string } | { kind: "param"; nodeId: string; param: string } | null;

export function insertTarget(
  language: string | null,
  node: { id: string; type?: string; params?: Record<string, unknown> } | null | undefined,
): InsertTarget {
  if (!node) return null;
  const lang = (language ?? "").toLowerCase();
  if (node.type === "python_task") {
    return lang === "" || lang === "python" || lang === "py" ? { kind: "code", nodeId: node.id } : null;
  }
  if (node.type !== "operator") return null;
  const params = node.params ?? {};
  if (["bash", "sh", "shell"].includes(lang)) {
    const name = "bash_command" in params ? "bash_command" : "command" in params ? "command" : "bash_command";
    return { kind: "param", nodeId: node.id, param: name };
  }
  if (lang === "sql") return { kind: "param", nodeId: node.id, param: "sql" };
  return null;
}

/** Short label of a tool call's arguments for the chips (never longer than `max`). */
export function argsLabel(args: Record<string, unknown> | undefined, max = 80): string {
  if (!args) return "";
  const parts = Object.entries(args)
    .filter(([, v]) => v !== null && v !== undefined && typeof v !== "object")
    .map(([k, v]) => `${k}=${String(v)}`);
  const text = parts.join(" ");
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}
