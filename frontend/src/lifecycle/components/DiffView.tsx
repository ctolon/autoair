import { Box } from "@chakra-ui/react";
import { MergeView } from "@codemirror/merge";
import { EditorState, EditorView, getDefaultExtensions } from "@uiw/react-codemirror";
import { useEffect, useRef } from "react";

import { type CodeLanguage, languageExtension } from "./CodeView";
import { useIsDark } from "./useIsDark";

/** Side-by-side read-only diff of two texts (CodeMirror merge view; loaded lazily). */
export default function DiffView({
  before,
  after,
  language,
  height = "360px",
  label,
}: {
  before: string;
  after: string;
  language: CodeLanguage;
  height?: string;
  label: string;
}) {
  const parent = useRef<HTMLDivElement>(null);
  const dark = useIsDark();
  useEffect(() => {
    if (!parent.current) return;
    const extensions = [
      ...getDefaultExtensions({ theme: dark ? "dark" : "light", readOnly: true, editable: false }),
      ...languageExtension(language),
      EditorState.readOnly.of(true),
      EditorView.editable.of(false),
    ];
    const view = new MergeView({
      a: { doc: before, extensions },
      b: { doc: after, extensions },
      parent: parent.current,
      gutter: true,
      highlightChanges: true,
      collapseUnchanged: { margin: 3, minSize: 6 },
    });
    return () => view.destroy();
  }, [before, after, language, dark]);
  return (
    <Box
      ref={parent}
      role="region"
      aria-label={label}
      h={height}
      overflow="auto"
      fontSize="xs"
      borderWidth="1px"
      rounded="md"
    />
  );
}
