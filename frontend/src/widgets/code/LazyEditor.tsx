import { Textarea } from "@chakra-ui/react";
import { Suspense, lazy, useRef } from "react";

import type { CodeEditorProps, FocusHandoff } from "./types";

// CodeMirror (+ language packs) is only fetched when an editor is first shown.
const CodeEditorImpl = lazy(() => import("./CodeEditor"));

/**
 * Editable textarea shown while CodeMirror loads (keeps the field usable). While it has focus
 * it records the selection, so CodeMirror can take over focus and caret when it replaces it:
 * otherwise the first keys typed into a fresh field lost focus and the caret mid-word.
 */
function Fallback({ value, onChange, ariaLabel, readOnly, placeholder, minHeight, focusHandoffRef }: CodeEditorProps) {
  const record = (el: HTMLTextAreaElement) => {
    if (!focusHandoffRef) return;
    const backward = el.selectionDirection === "backward";
    focusHandoffRef.current = {
      anchor: backward ? el.selectionEnd : el.selectionStart,
      head: backward ? el.selectionStart : el.selectionEnd,
    };
  };
  return (
    <Textarea
      size="sm"
      fontFamily="mono"
      fontSize="xs"
      aria-label={ariaLabel}
      value={value}
      readOnly={readOnly ?? false}
      placeholder={placeholder}
      minH={minHeight ?? "2.2rem"}
      spellCheck={false}
      onChange={(e) => {
        record(e.currentTarget);
        onChange(e.target.value);
      }}
      onFocus={(e) => record(e.currentTarget)}
      onSelect={(e) => record(e.currentTarget)}
      onBlur={(e) => {
        // Removed from the page (CodeMirror took its place): keep the hand-off.
        if (focusHandoffRef && e.currentTarget.isConnected) focusHandoffRef.current = null;
      }}
    />
  );
}

/** The bare CodeMirror editor, loaded lazily (no resize handle, expand or validate buttons). */
export function LazyEditor(props: CodeEditorProps) {
  const handoffRef = useRef<FocusHandoff | null>(null);
  return (
    <Suspense fallback={<Fallback {...props} focusHandoffRef={handoffRef} />}>
      <CodeEditorImpl {...props} focusHandoffRef={handoffRef} />
    </Suspense>
  );
}
