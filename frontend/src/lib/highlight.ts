import { pythonLanguage } from "@codemirror/lang-python";
import { jsonLanguage } from "@codemirror/lang-json";
import { StandardSQL } from "@codemirror/lang-sql";
import { yamlLanguage } from "@codemirror/lang-yaml";
import { classHighlighter, highlightCode } from "@lezer/highlight";

const PARSERS = {
  python: pythonLanguage.parser,
  py: pythonLanguage.parser,
  json: jsonLanguage.parser,
  sql: StandardSQL.language.parser,
  yaml: yamlLanguage.parser,
  yml: yamlLanguage.parser,
} as const;

type Token = { text: string; cls: string };

/**
 * Static syntax highlighting with the editor's own parsers (no second highlighter library):
 * code → lines of tokens carrying `tok-*` classes (styled by the Markdown renderer).
 * Unknown languages come back as plain text.
 */
export function highlightLines(code: string, language: string | undefined): Token[][] {
  const parser = language ? PARSERS[language.toLowerCase() as keyof typeof PARSERS] : undefined;
  if (!parser) return code.split("\n").map((line) => [{ text: line, cls: "" }]);
  const lines: Token[][] = [[]];
  highlightCode(
    code,
    parser.parse(code),
    classHighlighter,
    (text, cls) => lines[lines.length - 1]?.push({ text, cls }),
    () => lines.push([]),
  );
  return lines;
}
