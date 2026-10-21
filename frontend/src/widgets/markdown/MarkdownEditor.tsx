import { Box, Button, HStack, IconButton, Separator, Stack, Text } from "@chakra-ui/react";
import { type ReactNode, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  LuBold,
  LuCode,
  LuColumns2,
  LuEye,
  LuHeading,
  LuItalic,
  LuLink,
  LuList,
  LuListOrdered,
  LuMaximize2,
  LuPencil,
  LuQuote,
  LuTable,
} from "react-icons/lu";

import { DialogShell } from "../../lifecycle/components/DialogShell";
import { LazyEditor } from "../code/LazyEditor";
import { useEditorTitle } from "../code/title";
import type { CodeEditorHandle, CodeEditorProps } from "../code/types";
import { type MarkdownCommand, formatMarkdown } from "./format";
import { MarkdownPreview } from "./index";

type MarkdownView = "edit" | "split" | "preview";

const ICONS: Record<MarkdownCommand, ReactNode> = {
  heading: <LuHeading />,
  bold: <LuBold />,
  italic: <LuItalic />,
  bulletList: <LuList />,
  numberedList: <LuListOrdered />,
  quote: <LuQuote />,
  code: <LuCode />,
  link: <LuLink />,
  table: <LuTable />,
};
/** Groups of the toolbar (separated visually). */
const GROUPS: MarkdownCommand[][] = [
  ["heading", "bold", "italic"],
  ["bulletList", "numberedList", "quote"],
  ["code", "link", "table"],
];
const SHORTCUTS: Partial<Record<string, MarkdownCommand>> = { b: "bold", i: "italic", k: "link" };
const VIEW_ICONS: Record<MarkdownView, ReactNode> = { edit: <LuPencil />, split: <LuColumns2 />, preview: <LuEye /> };

function Preview({ text, minH, maxH }: { text: string; minH?: string | undefined; maxH?: string | undefined }) {
  const { t } = useTranslation("common");
  return (
    <Box
      borderWidth="1px"
      rounded="md"
      px={3}
      py={1}
      minH={minH}
      maxH={maxH}
      h={maxH ? undefined : "100%"}
      overflowY="auto"
      data-markdown-preview=""
    >
      {text.trim() ? (
        <MarkdownPreview text={text} />
      ) : (
        <Text fontSize="xs" color="fg.muted" py={2}>
          {t("markdown.empty")}
        </Text>
      )}
    </Box>
  );
}

/** Formatting buttons + Edit / Split / Preview switch (+ full screen). */
function Toolbar({
  view,
  onView,
  onCommand,
  onFullscreen,
  readOnly,
  extra,
}: {
  extra?: ReactNode;
  view: MarkdownView;
  onView: (v: MarkdownView) => void;
  onCommand: (c: MarkdownCommand) => void;
  onFullscreen?: (() => void) | undefined;
  readOnly?: boolean | undefined;
}) {
  const { t } = useTranslation("common");
  return (
    <HStack gap={1} mb={1} minW={0} flexWrap="wrap" rowGap={0.5} role="toolbar" aria-label={t("markdown.toolbar")}>
      {readOnly || view === "preview"
        ? null
        : GROUPS.map((group, i) => (
            <HStack key={group.join()} gap={0} flexShrink={0}>
              {i ? <Separator orientation="vertical" h="16px" mx={0.5} /> : null}
              {group.map((c) => (
                <IconButton
                  key={c}
                  size="2xs"
                  variant="ghost"
                  aria-label={t(`markdown.cmd.${c}`)}
                  title={t(`markdown.cmd.${c}`)}
                  // Keep the editor's selection: act on mouse down, don't take focus.
                  onMouseDown={(e) => e.preventDefault()}
                  onClick={() => onCommand(c)}
                >
                  {ICONS[c]}
                </IconButton>
              ))}
            </HStack>
          ))}
      <Box flex="1" />
      {extra}
      <HStack gap={0} role="group" aria-label={t("markdown.view")} flexShrink={0}>
        {(["edit", "split", "preview"] as const).map((v) => (
          <Button
            key={v}
            size="2xs"
            variant={view === v ? "subtle" : "ghost"}
            aria-pressed={view === v}
            title={t(`markdown.${v}`)}
            onClick={() => onView(v)}
          >
            {VIEW_ICONS[v]} {t(`markdown.${v}`)}
          </Button>
        ))}
      </HStack>
      {onFullscreen ? (
        <IconButton
          size="2xs"
          variant="ghost"
          aria-label={t("markdown.fullscreen")}
          title={t("markdown.fullscreen")}
          onClick={onFullscreen}
          flexShrink={0}
        >
          <LuMaximize2 />
        </IconButton>
      ) : null}
    </HStack>
  );
}

function Body({
  props,
  view,
  onView,
  fullscreen,
  onFullscreen,
  height,
  extra,
}: {
  extra?: ReactNode;
  props: CodeEditorProps;
  view: MarkdownView;
  onView: (v: MarkdownView) => void;
  fullscreen?: boolean;
  onFullscreen?: () => void;
  height?: string | undefined;
}) {
  const { t } = useTranslation("common");
  const handle = useRef<CodeEditorHandle | null>(null);
  const run = (c: MarkdownCommand) =>
    handle.current?.edit((doc, from, to) =>
      formatMarkdown(c, doc, from, to, {
        text: t("markdown.placeholder.text"),
        heading: t("markdown.placeholder.heading"),
        code: t("markdown.placeholder.code"),
      }),
    );
  const onKeyDown = (e: React.KeyboardEvent) => {
    if (!(e.ctrlKey || e.metaKey) || e.altKey || e.shiftKey || props.readOnly) return;
    const command = SHORTCUTS[e.key.toLowerCase()];
    if (!command) return;
    // Formatting wins over the builder's global shortcuts while typing here.
    e.preventDefault();
    e.stopPropagation();
    run(command);
  };
  const editorHeight = fullscreen ? "100%" : height;
  const editor = (
    <LazyEditor
      {...props}
      noExpand
      compact={false}
      {...(fullscreen ? { minHeight: undefined, maxHeight: undefined } : {})}
      {...(editorHeight ? { height: editorHeight } : {})}
      onReady={(h) => {
        handle.current = h;
        props.onReady?.(h);
      }}
    />
  );
  const previewMax = fullscreen ? undefined : (height ?? props.maxHeight ?? "24rem");
  const previewMin = fullscreen ? undefined : (props.minHeight ?? "4rem");
  return (
    <Stack gap={0} h={fullscreen ? "100%" : undefined} minW={0} onKeyDown={onKeyDown}>
      <Toolbar
        view={view}
        onView={onView}
        onCommand={run}
        onFullscreen={fullscreen ? undefined : onFullscreen}
        readOnly={props.readOnly}
        extra={extra}
      />
      {view === "preview" ? (
        <Box flex={fullscreen ? "1" : undefined} minH={0}>
          <Preview text={props.value} minH={previewMin} maxH={previewMax} />
        </Box>
      ) : view === "split" ? (
        // Side-by-side live preview, like VS Code's Markdown preview.
        <HStack align="stretch" gap={2} flex={fullscreen ? "1" : undefined} minH={0}>
          <Box flex="1" minW={0}>
            {editor}
          </Box>
          <Box flex="1" minW={0}>
            <Preview text={props.value} minH={previewMin} maxH={previewMax} />
          </Box>
        </HStack>
      ) : (
        <Box flex={fullscreen ? "1" : undefined} minH={0}>
          {editor}
        </Box>
      )}
    </Stack>
  );
}

/**
 * Markdown editor for documentation (DAG / task `doc_md`, descriptions): formatting toolbar
 * (heading, bold, italic, lists, quote, code, link, table; Ctrl/⌘+B / I / K), Edit / Split /
 * Preview views with a live GitHub-flavoured preview, and a full-screen mode.
 */
export function MarkdownEditor(
  props: CodeEditorProps & {
    /** Under the inline editor (e.g. the resize handle, validation result). */
    footer?: ReactNode;
    /** Extra toolbar controls (e.g. Validate), inline and full screen. */
    toolbarExtra?: ReactNode;
    /** Under the full-screen editor. */
    fullscreenFooter?: ReactNode;
  },
) {
  const [view, setView] = useState<MarkdownView>("edit");
  const [fullView, setFullView] = useState<MarkdownView>("split");
  const [fullscreen, setFullscreen] = useState(false);
  const { footer, toolbarExtra, fullscreenFooter, ...editorProps } = props;
  const title = useEditorTitle(props.expandTitle ?? props.ariaLabel);
  return (
    <Box w="100%" data-markdown-editor="">
      <Body
        props={editorProps}
        view={view}
        onView={setView}
        onFullscreen={() => setFullscreen(true)}
        height={props.height}
        extra={toolbarExtra}
      />
      {footer}
      {fullscreen ? (
        <DialogShell title={title} size="cover" onClose={() => setFullscreen(false)}>
          <Stack h="100%" minH="20rem" gap={2}>
            <Box flex="1" minH={0}>
              <Body props={editorProps} view={fullView} onView={setFullView} fullscreen extra={toolbarExtra} />
            </Box>
            {fullscreenFooter}
          </Stack>
        </DialogShell>
      ) : null}
    </Box>
  );
}
