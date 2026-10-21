/**
 * Markdown formatting commands of the editor toolbar, as pure functions of the document and
 * the selection: each returns the replaced range, the inserted text and the new selection.
 * Commands toggle (bold on bold text removes it; a list prefix on listed lines is removed).
 */

type TextEdit = {
  /** Range of the document replaced by `insert`. */
  from: number;
  to: number;
  insert: string;
  /** Selection after the edit (document offsets after the change). */
  selectionFrom: number;
  selectionTo: number;
};

export type MarkdownCommand =
  "bold" | "italic" | "heading" | "bulletList" | "numberedList" | "quote" | "code" | "link" | "table";

/** Placeholders inserted when nothing is selected (selected afterwards, ready to type over). */
type Placeholders = { text: string; url: string; code: string; heading: string };
const DEFAULT_PLACEHOLDERS: Placeholders = { text: "text", url: "https://", code: "code", heading: "Heading" };

function wrap(doc: string, from: number, to: number, mark: string, placeholder: string): TextEdit {
  const selected = doc.slice(from, to);
  const n = mark.length;
  // Toggle off: the selection is wrapped (inside or including the marks).
  if (selected.length >= 2 * n && selected.startsWith(mark) && selected.endsWith(mark)) {
    const inner = selected.slice(n, -n);
    return { from, to, insert: inner, selectionFrom: from, selectionTo: from + inner.length };
  }
  if (doc.slice(from - n, from) === mark && doc.slice(to, to + n) === mark) {
    return { from: from - n, to: to + n, insert: selected, selectionFrom: from - n, selectionTo: to - n };
  }
  const text = selected || placeholder;
  return {
    from,
    to,
    insert: `${mark}${text}${mark}`,
    selectionFrom: from + n,
    selectionTo: from + n + text.length,
  };
}

/** Whole lines covered by the selection. */
function lineRange(doc: string, from: number, to: number): [number, number] {
  const start = doc.lastIndexOf("\n", from - 1) + 1;
  // A selection ending at the start of a line does not include that line.
  const effectiveTo = to > from && doc[to - 1] === "\n" ? to - 1 : to;
  const nl = doc.indexOf("\n", effectiveTo);
  return [start, nl === -1 ? doc.length : nl];
}

function prefixLines(
  doc: string,
  from: number,
  to: number,
  prefix: (index: number) => string,
  existing: RegExp,
  placeholder = "",
): TextEdit {
  const [start, end] = lineRange(doc, from, to);
  const lines = doc.slice(start, end).split("\n");
  const content = lines.filter((l) => l.trim());
  const remove = content.length > 0 && content.every((l) => existing.test(l));
  let i = 0;
  const next = lines.map((line) => {
    if (remove) return line.replace(existing, "");
    if (!line.trim() && lines.length > 1) return line;
    const bare = line.replace(existing, "");
    return `${prefix(i++)}${bare || placeholder}`;
  });
  const insert = next.join("\n");
  if (lines.length === 1 && !remove && !lines[0]?.trim() && placeholder) {
    const p = prefix(0).length;
    return { from: start, to: end, insert, selectionFrom: start + p, selectionTo: start + p + placeholder.length };
  }
  return { from: start, to: end, insert, selectionFrom: start, selectionTo: start + insert.length };
}

const TABLE = "| Column | Column |\n| ------ | ------ |\n| Cell   | Cell   |\n";

/** The edit a toolbar command makes on `doc` with the selection `from`..`to`. */
export function formatMarkdown(
  command: MarkdownCommand,
  doc: string,
  from: number,
  to: number,
  placeholders: Partial<Placeholders> = {},
): TextEdit {
  const ph = { ...DEFAULT_PLACEHOLDERS, ...placeholders };
  const lo = Math.max(0, Math.min(from, to, doc.length));
  const hi = Math.min(doc.length, Math.max(from, to));
  switch (command) {
    case "bold":
      return wrap(doc, lo, hi, "**", ph.text);
    case "italic":
      return wrap(doc, lo, hi, "_", ph.text);
    case "heading":
      return prefixLines(doc, lo, hi, () => "## ", /^#{1,6}\s+/, ph.heading);
    case "bulletList":
      return prefixLines(doc, lo, hi, () => "- ", /^\s*[-*+]\s+(?!\[)/, ph.text);
    case "numberedList":
      return prefixLines(doc, lo, hi, (i) => `${i + 1}. `, /^\s*\d+[.)]\s+/, ph.text);
    case "quote":
      return prefixLines(doc, lo, hi, () => "> ", /^>\s?/, ph.text);
    case "code": {
      const selected = doc.slice(lo, hi);
      const [start, end] = lineRange(doc, lo, hi);
      // Inline code inside a line; a fenced block for several lines or on an empty line.
      if (!selected.includes("\n") && (selected || doc.slice(start, end).trim()))
        return wrap(doc, lo, hi, "`", ph.code);
      // Fenced block on its own lines.
      const body = selected.replace(/\n$/, "") || ph.code;
      const before = lo > 0 && doc[lo - 1] !== "\n" ? "\n" : "";
      const after = hi < doc.length && doc[hi] !== "\n" ? "\n" : "";
      const open = `${before}\`\`\`\n`;
      return {
        from: lo,
        to: hi,
        insert: `${open}${body}\n\`\`\`${after}`,
        selectionFrom: lo + open.length,
        selectionTo: lo + open.length + body.length,
      };
    }
    case "link": {
      const selected = doc.slice(lo, hi);
      const isUrl = /^https?:\/\/\S+$/.test(selected);
      const text = isUrl ? ph.text : selected || ph.text;
      const url = isUrl ? selected : ph.url;
      const insert = `[${text}](${url})`;
      // Select what is left to fill in: the URL, or the text when a URL was selected.
      const selectionFrom = isUrl ? lo + 1 : lo + text.length + 3;
      const selectionTo = isUrl ? lo + 1 + text.length : selectionFrom + url.length;
      return { from: lo, to: hi, insert, selectionFrom, selectionTo };
    }
    case "table": {
      const before = lo > 0 && doc[lo - 1] !== "\n" ? "\n\n" : lo > 1 && doc[lo - 2] !== "\n" ? "\n" : "";
      const insert = `${before}${TABLE}`;
      const first = lo + before.length + 2; // the first header cell
      return { from: lo, to: hi, insert, selectionFrom: first, selectionTo: first + "Column".length };
    }
  }
}

/** Applies an edit to a string (tests, and editors without CodeMirror). */
export function applyEdit(doc: string, edit: TextEdit): string {
  return doc.slice(0, edit.from) + edit.insert + doc.slice(edit.to);
}
