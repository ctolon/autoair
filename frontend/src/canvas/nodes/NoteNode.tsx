import { Box, HStack, IconButton, Text, Textarea } from "@chakra-ui/react";
import { NodeResizer, NodeToolbar, type NodeProps, Position } from "@xyflow/react";
import { memo, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { LuPencil, LuTrash2 } from "react-icons/lu";

import type { NoteFlowNode } from "../../lib/types";
import { useBuilder } from "../../store/builder";
import { MiniMarkdown } from "./miniMarkdown";

/** Sticky-note colors (Chakra palettes); stored as the spec's `color`. */
const NOTE_COLORS = ["yellow", "orange", "pink", "purple", "blue", "teal", "green", "gray"] as const;
type NoteColor = (typeof NOTE_COLORS)[number];

const noteColor = (c: string | null | undefined): NoteColor =>
  (NOTE_COLORS as readonly string[]).includes(c ?? "") ? (c as NoteColor) : "yellow";

function NoteEditor({ initial, onDone }: { initial: string; onDone: (text: string | null) => void }) {
  const { t } = useTranslation("canvas");
  const [value, setValue] = useState(initial);
  const ref = useRef<HTMLTextAreaElement>(null);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.focus();
    el.setSelectionRange(el.value.length, el.value.length);
  }, []);
  return (
    <Textarea
      ref={ref}
      className="nodrag nowheel nopan"
      aria-label={t("note.editLabel")}
      value={value}
      onChange={(e) => setValue(e.target.value)}
      onBlur={() => onDone(value)}
      onKeyDown={(e) => {
        e.stopPropagation();
        if (e.key === "Escape") onDone(null);
        else if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) onDone(value);
      }}
      placeholder={t("note.placeholder")}
      resize="none"
      h="100%"
      fontSize="sm"
      bg="transparent"
      border="none"
      p={0}
      _focusVisible={{ outline: "none" }}
    />
  );
}

/** Sticky note (spec kind "note"): markdown-ish text, inline editing, color variants. Not connectable. */
export const NoteNodeView = memo(function NoteNodeView({ id, data, selected }: NodeProps<NoteFlowNode>) {
  const { t } = useTranslation("canvas");
  const [editing, setEditing] = useState(false);
  const color = noteColor(data.spec.color);
  const text = data.spec.text ?? "";

  const finish = (next: string | null) => {
    setEditing(false);
    if (next !== null && next !== text) useBuilder.getState().updateSpec<"note">(id, { text: next });
  };

  return (
    <Box
      w="100%"
      h="100%"
      colorPalette={color}
      bg="colorPalette.subtle"
      color="colorPalette.fg"
      borderWidth={selected ? "2px" : "1px"}
      borderColor={selected ? "blue.500" : "colorPalette.muted"}
      rounded="sm"
      shadow="sm"
      px={3}
      py={2}
      overflow="hidden"
      role="note"
      onDoubleClick={(e) => {
        e.stopPropagation();
        setEditing(true);
      }}
    >
      <NodeResizer isVisible={selected} minWidth={140} minHeight={80} />
      {selected && !editing ? (
        <NodeToolbar position={Position.Top} offset={8}>
          <HStack
            gap={1}
            p={1}
            bg="bg.panel"
            borderWidth="1px"
            borderColor="border.emphasized"
            rounded="md"
            shadow="md"
            role="toolbar"
            aria-label={t("note.toolbar")}
          >
            {NOTE_COLORS.map((c) => (
              <Box
                key={c}
                as="button"
                aria-label={t("note.color", { color: t(`note.colors.${c}`) })}
                aria-pressed={c === color}
                title={t(`note.colors.${c}`)}
                w="16px"
                h="16px"
                rounded="full"
                colorPalette={c}
                bg="colorPalette.solid"
                borderWidth="2px"
                borderColor={c === color ? "fg" : "transparent"}
                cursor="pointer"
                onClick={() => useBuilder.getState().updateSpec<"note">(id, { color: c })}
              />
            ))}
            <IconButton
              size="2xs"
              variant="ghost"
              aria-label={t("note.edit")}
              title={t("note.edit")}
              onClick={() => setEditing(true)}
            >
              <LuPencil />
            </IconButton>
            <IconButton
              size="2xs"
              variant="ghost"
              colorPalette="red"
              aria-label={t("actions.delete")}
              title={t("actions.delete")}
              onClick={() => useBuilder.getState().removeElements([id])}
            >
              <LuTrash2 />
            </IconButton>
          </HStack>
        </NodeToolbar>
      ) : null}
      {data.spec.label ? (
        <Text fontWeight="bold" fontSize="sm" mb={1} truncate>
          {data.spec.label}
        </Text>
      ) : null}
      {editing ? (
        <NoteEditor initial={text} onDone={finish} />
      ) : text ? (
        <Box fontSize="sm" h="100%" overflow="hidden">
          <MiniMarkdown source={text} />
        </Box>
      ) : (
        <Text fontSize="sm" color="colorPalette.fg" opacity={0.6} fontStyle="italic">
          {t("note.empty")}
        </Text>
      )}
    </Box>
  );
});
