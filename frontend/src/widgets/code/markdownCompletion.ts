import { type CompletionContext, type CompletionResult, snippetCompletion } from "@codemirror/autocomplete";

/** Block snippets offered at the start of a line (type `/` or a few letters, or Ctrl+Space). */
const BLOCKS = [
  snippetCompletion("# ${Title}", { label: "/h1", detail: "Heading 1", type: "keyword" }),
  snippetCompletion("## ${Section}", { label: "/h2", detail: "Heading 2", type: "keyword" }),
  snippetCompletion("### ${Subsection}", { label: "/h3", detail: "Heading 3", type: "keyword" }),
  snippetCompletion("- ${item}", { label: "/list", detail: "Bullet list", type: "keyword" }),
  snippetCompletion("1. ${item}", { label: "/numbered", detail: "Numbered list", type: "keyword" }),
  snippetCompletion("- [ ] ${task}", { label: "/todo", detail: "Task list", type: "keyword" }),
  snippetCompletion("```python\n${code}\n```", { label: "/python", detail: "Python code block", type: "keyword" }),
  snippetCompletion("```sql\n${query}\n```", { label: "/sql", detail: "SQL code block", type: "keyword" }),
  snippetCompletion("```bash\n${command}\n```", { label: "/bash", detail: "Shell code block", type: "keyword" }),
  snippetCompletion("```${language}\n${code}\n```", { label: "/code", detail: "Code block", type: "keyword" }),
  snippetCompletion("| ${Column} | ${Column} |\n| --- | --- |\n| ${value} | ${value} |", {
    label: "/table",
    detail: "Table",
    type: "keyword",
  }),
  snippetCompletion("> ${quote}", { label: "/quote", detail: "Quote", type: "keyword" }),
  snippetCompletion("> **Note**\n> ${text}", { label: "/note", detail: "Note callout", type: "keyword" }),
  snippetCompletion("> **Warning**\n> ${text}", { label: "/warning", detail: "Warning callout", type: "keyword" }),
  snippetCompletion("---\n", { label: "/hr", detail: "Horizontal rule", type: "keyword" }),
  // Sections commonly found in DAG docs (doc_md): owners, inputs/outputs, runbooks.
  snippetCompletion(
    "## Owner\n${team} — ${contact}\n\n## Inputs\n- ${source}\n\n## Outputs\n- ${target}\n\n## On failure\n${runbook}",
    {
      label: "/dagdoc",
      detail: "DAG documentation outline",
      type: "keyword",
    },
  ),
];

/** Inline snippets offered anywhere. */
const INLINE = [
  snippetCompletion("[${text}](${https://})", { label: "/link", detail: "Link", type: "text" }),
  snippetCompletion("![${alt}](${https://})", { label: "/image", detail: "Image", type: "text" }),
  snippetCompletion("**${bold}**", { label: "/bold", detail: "Bold", type: "text" }),
  snippetCompletion("_${italic}_", { label: "/italic", detail: "Italic", type: "text" }),
  snippetCompletion("`${code}`", { label: "/inline-code", detail: "Inline code", type: "text" }),
];

/** Markdown authoring help: `/` commands for blocks (line start) and inline markup. */
export function markdownCompletions(ctx: CompletionContext): CompletionResult | null {
  const word = ctx.matchBefore(/\/[\w-]*$/);
  if (!word && !ctx.explicit) return null;
  const from = word ? word.from : ctx.pos;
  const line = ctx.state.doc.lineAt(ctx.pos);
  const atLineStart = ctx.state.sliceDoc(line.from, from).trim() === "";
  return {
    from,
    options: atLineStart ? [...BLOCKS, ...INLINE] : INLINE,
    validFor: /^\/[\w-]*$/,
  };
}
