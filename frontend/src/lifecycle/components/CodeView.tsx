import { Box, IconButton } from "@chakra-ui/react";
import { json } from "@codemirror/lang-json";
import { python } from "@codemirror/lang-python";
import { yaml } from "@codemirror/lang-yaml";
import CodeMirror, { EditorView, type Extension } from "@uiw/react-codemirror";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { LuMaximize2 } from "react-icons/lu";

import { DialogShell } from "./DialogShell";
import { useIsDark } from "./useIsDark";

export type CodeLanguage = "python" | "yaml" | "json" | "text";

export function languageExtension(lang: CodeLanguage): Extension[] {
  switch (lang) {
    case "python":
      return [python()];
    case "yaml":
      return [yaml()];
    case "json":
      return [json()];
    case "text":
      return [];
  }
}

/** CodeMirror editor/viewer (loaded lazily: CodeMirror is a heavy chunk). */
export default function CodeView({
  value,
  language,
  onChange,
  readOnly = false,
  height = "100%",
  label,
  wrap = false,
  expandable = true,
}: {
  value: string;
  language: CodeLanguage;
  onChange?: (v: string) => void;
  readOnly?: boolean;
  height?: string;
  /** Accessible name of the editor. */
  label: string;
  wrap?: boolean;
  /** Offer a full-screen dialog (large monitors, long files). */
  expandable?: boolean;
}) {
  const { t } = useTranslation("common");
  const [expanded, setExpanded] = useState(false);
  const dark = useIsDark();
  const extensions = useMemo(
    () => [
      ...languageExtension(language),
      ...(wrap ? [EditorView.lineWrapping] : []),
      EditorView.contentAttributes.of({ "aria-label": label }),
    ],
    [language, wrap, label],
  );
  const editor = (h: string) => (
    <CodeMirror
      value={value}
      height={h}
      theme={dark ? "dark" : "light"}
      readOnly={readOnly}
      editable={!readOnly}
      extensions={extensions}
      basicSetup={{ foldGutter: true, highlightActiveLine: !readOnly, autocompletion: false }}
      onChange={onChange}
      style={{ height: "100%" }}
    />
  );
  return (
    <Box
      h={height}
      fontSize="xs"
      overflow="hidden"
      borderWidth="1px"
      rounded="md"
      position="relative"
      className="group"
    >
      {editor(height)}
      {expandable ? (
        <IconButton
          aria-label={t("codeEditor.expand", { name: label })}
          title={t("codeEditor.expand", { name: label })}
          size="2xs"
          variant="subtle"
          position="absolute"
          top="6px"
          right="14px"
          zIndex={1}
          opacity={0.6}
          _groupHover={{ opacity: 1 }}
          _focusVisible={{ opacity: 1 }}
          onClick={() => setExpanded(true)}
        >
          <LuMaximize2 />
        </IconButton>
      ) : null}
      {expanded ? (
        <DialogShell title={label} size="cover" onClose={() => setExpanded(false)}>
          <Box h="100%" minH="16rem" fontSize="sm" borderWidth="1px" rounded="md" overflow="hidden">
            {editor("100%")}
          </Box>
        </DialogShell>
      ) : null}
    </Box>
  );
}
