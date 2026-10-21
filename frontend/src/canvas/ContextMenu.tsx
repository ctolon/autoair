import { Box, Kbd, Menu, Portal } from "@chakra-ui/react";
import type { ReactElement, ReactNode } from "react";
import { useTranslation } from "react-i18next";
import {
  LuBlocks,
  LuChevronsDownUp,
  LuChevronsUpDown,
  LuClipboardPaste,
  LuCopy,
  LuCopyPlus,
  LuFocus,
  LuLayoutDashboard,
  LuPackageCheck,
  LuPencilLine,
  LuScissors,
  LuSquareDashedMousePointer,
  LuStickyNote,
  LuTrash2,
  LuUngroup,
} from "react-icons/lu";

import { useAccess } from "../lib/access";
import { formatShortcut, getCommand } from "../lib/commands";
import { useLifecycle } from "../lifecycle/store";
import { useBuilder } from "../store/builder";
import * as act from "./actions";
import { screenToFlow } from "./flowInstance";
import { useCanvasUi } from "./uiStore";

/** Canvas position of the last right click ("paste here", "add note here"). */
let lastPoint: { x: number; y: number } | null = null;

function Shortcut({ command }: { command: string }) {
  const s = getCommand(command)?.shortcut;
  return s ? (
    <Menu.ItemCommand>
      <Kbd size="sm">{formatShortcut(s)}</Kbd>
    </Menu.ItemCommand>
  ) : null;
}

function Item({
  value,
  icon,
  label,
  command,
  disabled,
  danger,
  onSelect,
}: {
  value: string;
  icon: ReactNode;
  label: string;
  command?: string;
  disabled?: boolean;
  danger?: boolean;
  onSelect: () => void;
}) {
  return (
    <Menu.Item
      value={value}
      disabled={disabled}
      color={danger ? "fg.error" : undefined}
      _hover={danger ? { bg: "bg.error", color: "fg.error" } : undefined}
      onClick={onSelect}
    >
      {icon}
      <Box flex="1">{label}</Box>
      {command ? <Shortcut command={command} /> : null}
    </Menu.Item>
  );
}

/**
 * Right-click menu for nodes, edges and the pane. Wraps the canvas: React Flow's
 * `on*ContextMenu` handlers record the target before the event reaches the trigger.
 */
export function CanvasContextMenu({ children }: { children: ReactElement }) {
  const { t } = useTranslation("canvas");
  const target = useCanvasUi((s) => s.contextTarget);
  const nodeType = useBuilder((s) =>
    target.kind === "node" ? s.nodes.find((n) => n.id === target.id)?.type : undefined,
  );
  const collapsed = useBuilder((s) =>
    target.kind === "node"
      ? !!s.nodes.find((n) => n.id === target.id && n.type === "task_group" && n.data.collapsed)
      : false,
  );
  const selCount = useBuilder((s) => s.nodes.filter((n) => n.selected).length);
  const access = useAccess();
  const canSaveOperatorTemplate =
    access.allows("templates", "template:write") &&
    (nodeType === "operator" || nodeType === "python_task" || nodeType === "custom");

  return (
    <Menu.Root lazyMount unmountOnExit>
      <Menu.ContextTrigger
        asChild
        onContextMenuCapture={(e) => {
          useCanvasUi.getState().setContextTarget({ kind: "pane" });
          lastPoint = screenToFlow({ x: e.clientX, y: e.clientY });
        }}
      >
        {children}
      </Menu.ContextTrigger>
      <Portal>
        <Menu.Positioner>
          <Menu.Content minW="240px" aria-label={t("menu.label")}>
            {target.kind === "node" ? (
              <>
                <Item
                  value="copy"
                  icon={<LuCopy />}
                  label={t("actions.copy")}
                  command="edit.copy"
                  onSelect={() => void act.copySelection()}
                />
                <Item
                  value="cut"
                  icon={<LuScissors />}
                  label={t("actions.cut")}
                  command="edit.cut"
                  onSelect={() => void act.cutSelection()}
                />
                <Item
                  value="duplicate"
                  icon={<LuCopyPlus />}
                  label={t("actions.duplicate")}
                  command="edit.duplicate"
                  onSelect={act.duplicateSelection}
                />
                {canSaveOperatorTemplate ? (
                  <Item
                    value="saveOperatorTemplate"
                    icon={<LuPackageCheck />}
                    label={t("actions.saveOperatorTemplate")}
                    onSelect={() =>
                      useLifecycle.getState().openDialog({ kind: "saveOperatorTemplate", nodeId: target.id })
                    }
                  />
                ) : null}
                <Menu.Separator />
                <Item
                  value="group"
                  icon={<LuBlocks />}
                  label={t("actions.group", { count: selCount })}
                  command="canvas.group"
                  onSelect={act.groupSelection}
                />
                {nodeType === "task_group" ? (
                  <>
                    <Item
                      value="ungroup"
                      icon={<LuUngroup />}
                      label={t("actions.ungroup")}
                      command="canvas.ungroup"
                      onSelect={() => useBuilder.getState().ungroup(target.id)}
                    />
                    <Item
                      value="collapse"
                      icon={collapsed ? <LuChevronsUpDown /> : <LuChevronsDownUp />}
                      label={collapsed ? t("actions.expand") : t("actions.collapse")}
                      command="canvas.toggleCollapse"
                      onSelect={() => act.toggleCollapsed(target.id)}
                    />
                  </>
                ) : null}
                <Item
                  value="focus"
                  icon={<LuFocus />}
                  label={t("actions.focus")}
                  onSelect={() => act.focusNode(target.id)}
                />
                <Menu.Separator />
                <Item
                  value="delete"
                  icon={<LuTrash2 />}
                  label={t("actions.delete")}
                  command="edit.delete"
                  danger
                  onSelect={act.deleteSelection}
                />
              </>
            ) : target.kind === "edge" ? (
              <>
                <Item
                  value="label"
                  icon={<LuPencilLine />}
                  label={t("actions.editLabel")}
                  onSelect={() => useCanvasUi.getState().setEditingEdge(target.id)}
                />
                <Item
                  value="delete-edge"
                  icon={<LuTrash2 />}
                  label={t("actions.deleteEdge")}
                  danger
                  onSelect={() => useBuilder.getState().removeElements([], [target.id])}
                />
              </>
            ) : (
              <>
                <Item
                  value="paste"
                  icon={<LuClipboardPaste />}
                  label={t("actions.paste")}
                  command="edit.paste"
                  disabled={!act.canPaste()}
                  onSelect={() => void act.pasteFromClipboard(lastPoint ?? undefined)}
                />
                <Item
                  value="note"
                  icon={<LuStickyNote />}
                  label={t("actions.addNote")}
                  command="canvas.addNote"
                  onSelect={() => act.addNote(lastPoint ?? undefined)}
                />
                <Menu.Separator />
                <Item
                  value="select-all"
                  icon={<LuSquareDashedMousePointer />}
                  label={t("actions.selectAll")}
                  command="edit.selectAll"
                  onSelect={act.selectAll}
                />
                {selCount > 1 ? (
                  <Item
                    value="group-sel"
                    icon={<LuBlocks />}
                    label={t("actions.group", { count: selCount })}
                    command="canvas.group"
                    onSelect={act.groupSelection}
                  />
                ) : null}
                <Item
                  value="layout"
                  icon={<LuLayoutDashboard />}
                  label={t("actions.autoLayout")}
                  command="canvas.autoLayout"
                  onSelect={() => void act.runAutoLayout()}
                />
                <Item
                  value="fit"
                  icon={<LuFocus />}
                  label={t("actions.fitView")}
                  command="canvas.fitView"
                  onSelect={() => act.fitView()}
                />
              </>
            )}
          </Menu.Content>
        </Menu.Positioner>
      </Portal>
    </Menu.Root>
  );
}
