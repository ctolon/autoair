import { Box, HStack, IconButton, Stack } from "@chakra-ui/react";
import { useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { LuMaximize2 } from "react-icons/lu";

import { DialogShell } from "../../lifecycle/components/DialogShell";
import { MarkdownEditor } from "../markdown/MarkdownEditor";
import { LazyEditor } from "./LazyEditor";
import { useEditorTitle } from "./title";
import type { CodeDiagnostic, CodeEditorProps } from "./types";
import { ValidateButton, ValidateIconButton, ValidationResultLine, useCodeValidation } from "./validate";

export type { CodeDiagnostic, CodeEditorHandle, CodeLanguage, JinjaCompletionData } from "./types";

const MIN_HEIGHT = 48;

const Editor = LazyEditor;

/** Drag handle under an editor: resizes it (drag, arrow keys; double-click resets). */
function ResizeHandle({
  box,
  height,
  onHeight,
}: {
  box: React.RefObject<HTMLDivElement | null>;
  height: number | null;
  onHeight: (h: number | null) => void;
}) {
  const { t } = useTranslation("common");
  const measure = () => box.current?.querySelector(".cm-editor")?.getBoundingClientRect().height ?? 120;
  const startResize = (e: React.PointerEvent<HTMLDivElement>) => {
    e.preventDefault();
    const handle = e.currentTarget;
    handle.setPointerCapture(e.pointerId);
    const startY = e.clientY;
    const start = measure();
    const move = (ev: PointerEvent) => onHeight(Math.max(MIN_HEIGHT, Math.round(start + ev.clientY - startY)));
    const up = () => {
      handle.removeEventListener("pointermove", move);
      handle.removeEventListener("pointerup", up);
      handle.removeEventListener("pointercancel", up);
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", up);
    handle.addEventListener("pointercancel", up);
  };
  const resizeByKey = (e: React.KeyboardEvent) => {
    if (e.key !== "ArrowUp" && e.key !== "ArrowDown") return;
    e.preventDefault();
    onHeight(Math.max(MIN_HEIGHT, (height ?? measure()) + (e.key === "ArrowDown" ? 40 : -40)));
  };
  return (
    <Box
      role="separator"
      aria-orientation="horizontal"
      aria-label={t("codeEditor.resize")}
      tabIndex={0}
      h="8px"
      mt="-4px"
      position="relative"
      cursor="row-resize"
      touchAction="none"
      onPointerDown={startResize}
      onKeyDown={resizeByKey}
      onDoubleClick={() => onHeight(null)}
      _after={{
        content: '""',
        position: "absolute",
        left: "50%",
        top: "3px",
        w: "32px",
        h: "3px",
        ml: "-16px",
        rounded: "full",
        bg: "border.emphasized",
        opacity: 0,
        transition: "opacity 0.15s",
      }}
      _hover={{ _after: { opacity: 1 } }}
      _focusVisible={{ outline: "none", _after: { opacity: 1, bg: "blue.500" } }}
      css={{ ".group:hover &::after": { opacity: 0.7 } }}
    />
  );
}

/** Field diagnostics plus the result of the last "Validate" (while the text is unchanged). */
function mergeDiagnostics(own: CodeDiagnostic[] | undefined, validated: CodeDiagnostic[] | undefined) {
  if (!validated) return own;
  return [...(own ?? []), ...validated];
}

/**
 * Lazy CodeMirror 6 editor (language modes, Jinja highlighting + completion). Multi-line
 * editors can be resized from the bottom edge, validated on the server ("Validate": syntax
 * and lint diagnostics for Python, JSON, YAML, shell, SQL, Go, Java, JS/TS, Markdown) and
 * opened in a large dialog. Markdown gets a formatting toolbar and a live preview.
 */
export function CodeEditor(props: CodeEditorProps) {
  const { t } = useTranslation("common");
  const box = useRef<HTMLDivElement>(null);
  const [height, setHeight] = useState<number | null>(null);
  const [expanded, setExpanded] = useState(false);
  const validation = useCodeValidation(props.value, props.language, props.pythonLsp);
  const title = useEditorTitle(props.expandTitle ?? props.ariaLabel);
  if (props.noExpand) return <Editor {...props} />;
  const canValidate = validation.available && !props.compact && !props.noValidate;
  const diagnostics = canValidate ? mergeDiagnostics(props.diagnostics, validation.diagnostics) : props.diagnostics;
  const editorProps: CodeEditorProps = { ...props, ...(diagnostics !== undefined ? { diagnostics } : {}) };
  const result = canValidate ? <ValidationResultLine validation={validation} /> : null;

  if (props.language === "markdown" && !props.compact) {
    return (
      <Box ref={box} w="100%" className="group" data-code-editor="">
        <MarkdownEditor
          {...editorProps}
          {...(height ? { height: `${height}px` } : {})}
          toolbarExtra={canValidate ? <ValidateButton validation={validation} compact /> : null}
          footer={
            <>
              <ResizeHandle box={box} height={height} onHeight={setHeight} />
              {result}
            </>
          }
          fullscreenFooter={result}
        />
      </Box>
    );
  }

  return (
    <Box ref={box} position="relative" w="100%" className="group" data-code-editor="">
      <Editor {...editorProps} {...(height ? { height: `${height}px` } : {})} />
      {canValidate ? <ValidateIconButton validation={validation} name={title} /> : null}
      <IconButton
        aria-label={t("codeEditor.expand", { name: title })}
        title={t("codeEditor.expand", { name: title })}
        size="2xs"
        variant="subtle"
        position="absolute"
        top="4px"
        right="4px"
        zIndex={1}
        opacity={props.compact ? 0 : 0.55}
        _groupHover={{ opacity: 1 }}
        _groupFocusWithin={{ opacity: 1 }}
        _focusVisible={{ opacity: 1 }}
        onClick={() => setExpanded(true)}
      >
        <LuMaximize2 />
      </IconButton>
      {props.compact ? null : <ResizeHandle box={box} height={height} onHeight={setHeight} />}
      {result}
      {expanded ? (
        <DialogShell title={title} size="cover" onClose={() => setExpanded(false)}>
          <Stack h="100%" gap={2}>
            {props.expandToolbar || canValidate ? (
              <HStack justify="flex-end" gap={1}>
                {props.expandToolbar}
                {canValidate ? <ValidateButton validation={validation} /> : null}
              </HStack>
            ) : null}
            <Box flex="1" minH="16rem" minW={0}>
              <Editor
                {...editorProps}
                compact={false}
                noExpand
                minHeight={undefined}
                maxHeight={undefined}
                height="100%"
              />
            </Box>
            {result}
            {props.expandFooter ? (
              <Box maxH="30%" overflowY="auto">
                {props.expandFooter}
              </Box>
            ) : null}
          </Stack>
        </DialogShell>
      ) : null}
    </Box>
  );
}
