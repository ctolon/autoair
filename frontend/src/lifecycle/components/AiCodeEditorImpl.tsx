import { Box } from "@chakra-ui/react";
import { json } from "@codemirror/lang-json";
import { markdown } from "@codemirror/lang-markdown";
import { python } from "@codemirror/lang-python";
import { sql } from "@codemirror/lang-sql";
import { StreamLanguage } from "@codemirror/language";
import { shell } from "@codemirror/legacy-modes/mode/shell";
import CodeMirror, { EditorView, type Extension } from "@uiw/react-codemirror";
import { useMemo } from "react";

import { useIsDark } from "./useIsDark";

export type AiCodeLanguage = "python" | "bash" | "sql" | "json" | "markdown" | "text";

function extension(lang: AiCodeLanguage): Extension[] {
  switch (lang) {
    case "python":
      return [python()];
    case "bash":
      return [StreamLanguage.define(shell)];
    case "sql":
      return [sql()];
    case "json":
      return [json()];
    case "markdown":
      return [markdown()];
    default:
      return [];
  }
}

/** CodeMirror for AI dialogs (Python, Bash, SQL, JSON, Markdown); loaded lazily. */
export default function AiCodeEditorImpl({
  value,
  language,
  onChange,
  readOnly = false,
  height = "280px",
  label,
}: {
  value: string;
  language: AiCodeLanguage;
  onChange?: (value: string) => void;
  readOnly?: boolean;
  height?: string;
  label: string;
}) {
  const dark = useIsDark();
  const extensions = useMemo(
    () => [...extension(language), EditorView.lineWrapping, EditorView.contentAttributes.of({ "aria-label": label })],
    [language, label],
  );
  return (
    <Box h={height} minH="6rem" fontSize="xs" overflow="hidden" borderWidth="1px" rounded="md" resize="vertical">
      <CodeMirror
        value={value}
        height="100%"
        theme={dark ? "dark" : "light"}
        readOnly={readOnly}
        editable={!readOnly}
        extensions={extensions}
        basicSetup={{ foldGutter: false, highlightActiveLine: !readOnly, autocompletion: false }}
        onChange={onChange}
        style={{ height: "100%" }}
      />
    </Box>
  );
}
