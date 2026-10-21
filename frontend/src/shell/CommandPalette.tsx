import { Box, Dialog, HStack, Portal, Text } from "@chakra-ui/react";
import { Command } from "cmdk";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { LuCrosshair } from "react-icons/lu";

import { focusNode, insertItem } from "../canvas/actions";
import { canvasActive } from "../codemode/store";
import { InsertGroups, cmdkCss } from "../canvas/InsertList";
import { isCommandEnabled, useCommandList } from "../lib/commands";
import { nodeKey } from "../lib/types";
import { useBuilder } from "../store/builder";
import { COMMAND_PREFIX, paletteScore } from "./paletteRank";
import { useShell } from "./shellStore";
import { ShortcutKeys } from "./ShortcutKeys";

function GoToGroup({ onDone }: { onDone: () => void }) {
  const { t } = useTranslation("canvas");
  const tasks = useBuilder((s) => s.nodes)
    .filter((n) => n.type !== "note")
    .map((n) => ({
      id: n.id,
      key: nodeKey(n),
      kind: n.type,
      detail:
        n.data.spec.kind === "operator"
          ? (n.data.spec.operator.split(".").pop() ?? "")
          : n.data.spec.kind === "python_task"
            ? `@task${n.data.spec.decorator && n.data.spec.decorator !== "task" ? `.${n.data.spec.decorator}` : ""}`
            : n.data.spec.kind === "task_group"
              ? t("group.badge")
              : n.data.spec.kind === "custom"
                ? n.data.spec.handler
                : n.data.spec.kind === "blueprint"
                  ? `${n.data.spec.blueprint} v${n.data.spec.version ?? 1}`
                  : "",
      label: n.data.spec.label ?? "",
    }));
  if (!tasks.length) return null;
  return (
    <Command.Group heading={t("palette.goTo")}>
      {tasks.map((task) => (
        <Command.Item
          key={task.id}
          value={`goto:${task.id}`}
          keywords={[task.key, task.label, task.detail]}
          onSelect={() => {
            onDone();
            focusNode(task.id);
          }}
        >
          <Box color="fg.muted" aria-hidden>
            <LuCrosshair size={15} />
          </Box>
          <Text fontFamily="mono" truncate flex="1">
            {task.key}
          </Text>
          <Text fontSize="xs" color="fg.muted" truncate>
            {task.detail}
          </Text>
        </Command.Item>
      ))}
    </Command.Group>
  );
}

function CommandsGroup({ onDone }: { onDone: () => void }) {
  const { t } = useTranslation("canvas");
  const commands = useCommandList().filter((c) => !c.hidden && isCommandEnabled(c));
  if (!commands.length) return null;
  return (
    <Command.Group heading={t("palette.commands")}>
      {commands.map((c) => (
        <Command.Item
          key={c.id}
          value={`${COMMAND_PREFIX}${c.id}`}
          keywords={[c.title, c.group ?? "", ...(c.keywords ?? [])]}
          onSelect={() => {
            onDone();
            // Let the dialog close (and focus return) before the command runs.
            requestAnimationFrame(() => void c.run());
          }}
        >
          <Box color="fg.muted" aria-hidden w="15px" display="inline-flex">
            {c.icon}
          </Box>
          <Text truncate flex="1">
            {c.title}
          </Text>
          {c.group ? (
            <Text fontSize="xs" color="fg.subtle">
              {c.group}
            </Text>
          ) : null}
          {c.shortcut ? <ShortcutKeys shortcut={c.shortcut} /> : null}
        </Command.Item>
      ))}
    </Command.Group>
  );
}

/**
 * Ctrl/⌘+K: insert nodes (catalog, python variants, groups, notes, template fragments),
 * jump to a task, or run any registered command. Opened in "insert" mode (with a canvas
 * position) by double-clicking the empty canvas.
 */
export function CommandPalette() {
  const { t } = useTranslation("canvas");
  const palette = useShell((s) => s.palette);
  const close = useShell((s) => s.closePalette);
  const [search, setSearch] = useState("");
  // Inserting nodes and jumping to tasks need the visible canvas (not code mode / raw code).
  const canvas = palette.open && canvasActive();
  const insertMode = palette.mode === "insert" && canvas;

  return (
    <Dialog.Root
      open={palette.open}
      onOpenChange={(e) => {
        if (!e.open) close();
      }}
      onExitComplete={() => setSearch("")}
      placement="top"
      size="lg"
      lazyMount
      unmountOnExit
    >
      <Portal>
        <Dialog.Backdrop />
        <Dialog.Positioner>
          <Dialog.Content
            mt="10vh"
            overflow="hidden"
            css={{ ...cmdkCss, "& [cmdk-list]": { ...cmdkCss["& [cmdk-list]"], maxH: "min(60vh, 520px)" } }}
          >
            <Dialog.Title srOnly>{insertMode ? t("palette.insertTitle") : t("palette.title")}</Dialog.Title>
            <Command label={insertMode ? t("palette.insertTitle") : t("palette.title")} loop filter={paletteScore}>
              <Command.Input
                value={search}
                onValueChange={setSearch}
                placeholder={insertMode ? t("palette.insertPlaceholder") : t("palette.placeholder")}
              />
              <Command.List>
                <Command.Empty>{t("palette.empty")}</Command.Empty>
                {/* While typing, matching app commands come first (Deploy, Trigger, theme…). */}
                {!insertMode && search.trim() ? <CommandsGroup onDone={close} /> : null}
                {canvas ? (
                  <InsertGroups
                    search={search}
                    onPick={(item) => {
                      close();
                      void insertItem(item, palette.at ?? undefined);
                    }}
                  />
                ) : null}
                {insertMode || !canvas ? null : <GoToGroup onDone={close} />}
                {insertMode || search.trim() ? null : <CommandsGroup onDone={close} />}
              </Command.List>
            </Command>
            <HStack
              px={3}
              py={1.5}
              borderTopWidth="1px"
              borderColor="border.muted"
              gap={3}
              fontSize="xs"
              color="fg.muted"
            >
              <HStack gap={1}>
                <ShortcutKeys shortcut="arrowup" />
                <ShortcutKeys shortcut="arrowdown" />
                <Text>{t("palette.hintNavigate")}</Text>
              </HStack>
              <HStack gap={1}>
                <ShortcutKeys shortcut="enter" />
                <Text>{t("palette.hintSelect")}</Text>
              </HStack>
              <HStack gap={1}>
                <ShortcutKeys shortcut="escape" />
                <Text>{t("palette.hintClose")}</Text>
              </HStack>
            </HStack>
          </Dialog.Content>
        </Dialog.Positioner>
      </Portal>
    </Dialog.Root>
  );
}
