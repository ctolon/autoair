import { useEffect } from "react";
import { useTranslation } from "react-i18next";
import {
  LuBlocks,
  LuChevronsDownUp,
  LuClipboardPaste,
  LuCopy,
  LuCopyPlus,
  LuFocus,
  LuGrid3X3,
  LuLayoutDashboard,
  LuMap,
  LuRedo2,
  LuScissors,
  LuSquareDashedMousePointer,
  LuStickyNote,
  LuTrash2,
  LuUndo2,
  LuUngroup,
} from "react-icons/lu";

import { canvasActive } from "../codemode/store";
import { type Command, registerCommands } from "../lib/commands";
import { useBuilder } from "../store/builder";
import * as act from "./actions";
import { useCanvasUi } from "./uiStore";

const NUDGE = 1;
const NUDGE_FAST = 16;

/** Text selected on the page (keyboard copy must not hijack it). */
const hasTextSelection = () => {
  const sel = typeof window !== "undefined" ? window.getSelection() : null;
  return !!sel && !sel.isCollapsed && sel.toString().length > 0;
};

/** Registers the canvas' core commands (ids are stable; titles follow the UI language). */
export function useCanvasCommands(): void {
  const { t } = useTranslation("canvas");
  useEffect(() => {
    const edit = t("groups.edit");
    const canvas = t("groups.canvas");
    const temporal = () => useBuilder.temporal.getState();
    const nudge = (dir: "Left" | "Right" | "Up" | "Down", dx: number, dy: number): Command[] => [
      {
        id: `canvas.nudge${dir}`,
        title: t(`commands.nudge${dir}`),
        group: canvas,
        shortcut: `arrow${dir.toLowerCase()}`,
        hidden: true,
        repeat: true,
        when: act.hasSelection,
        run: () => act.nudgeSelection(dx * NUDGE, dy * NUDGE),
      },
      {
        id: `canvas.nudge${dir}Fast`,
        title: t(`commands.nudge${dir}Fast`),
        group: canvas,
        shortcut: `shift+arrow${dir.toLowerCase()}`,
        hidden: true,
        repeat: true,
        when: act.hasSelection,
        run: () => act.nudgeSelection(dx * NUDGE_FAST, dy * NUDGE_FAST),
      },
    ];
    return registerCommands(
      ...canvasOnly([
        {
          id: "edit.undo",
          title: t("commands.undo"),
          group: edit,
          shortcut: "mod+z",
          icon: <LuUndo2 />,
          repeat: true,
          when: () => temporal().pastStates.length > 0,
          run: act.undo,
        },
        {
          id: "edit.redo",
          title: t("commands.redo"),
          group: edit,
          shortcut: "mod+shift+z",
          altShortcuts: ["mod+y"],
          icon: <LuRedo2 />,
          repeat: true,
          when: () => temporal().futureStates.length > 0,
          run: act.redo,
        },
        {
          id: "edit.copy",
          title: t("commands.copy"),
          group: edit,
          shortcut: "mod+c",
          handledNatively: true,
          icon: <LuCopy />,
          when: () => act.hasSelection() && !hasTextSelection(),
          run: () => void act.copySelection(),
        },
        {
          id: "edit.cut",
          title: t("commands.cut"),
          group: edit,
          shortcut: "mod+x",
          handledNatively: true,
          icon: <LuScissors />,
          when: act.hasSelection,
          run: () => void act.cutSelection(),
        },
        {
          id: "edit.paste",
          title: t("commands.paste"),
          group: edit,
          shortcut: "mod+v",
          handledNatively: true,
          icon: <LuClipboardPaste />,
          when: act.canPaste,
          run: () => void act.pasteFromClipboard(),
        },
        {
          id: "edit.duplicate",
          title: t("commands.duplicate"),
          group: edit,
          shortcut: "mod+d",
          icon: <LuCopyPlus />,
          when: act.hasSelection,
          run: act.duplicateSelection,
        },
        {
          id: "edit.delete",
          title: t("commands.delete"),
          group: edit,
          shortcut: "delete",
          altShortcuts: ["backspace"],
          icon: <LuTrash2 />,
          when: act.hasAnySelection,
          run: act.deleteSelection,
        },
        {
          id: "edit.selectAll",
          title: t("commands.selectAll"),
          group: edit,
          shortcut: "mod+a",
          icon: <LuSquareDashedMousePointer />,
          when: () => useBuilder.getState().nodes.length > 0,
          run: act.selectAll,
        },
        {
          id: "edit.clearSelection",
          title: t("commands.clearSelection"),
          group: edit,
          shortcut: "escape",
          hidden: true,
          when: act.hasAnySelection,
          run: act.clearSelection,
        },
        {
          id: "canvas.group",
          title: t("commands.group"),
          group: canvas,
          shortcut: "mod+g",
          icon: <LuBlocks />,
          when: act.hasSelection,
          run: act.groupSelection,
        },
        {
          id: "canvas.ungroup",
          title: t("commands.ungroup"),
          group: canvas,
          shortcut: "mod+shift+g",
          icon: <LuUngroup />,
          when: act.canUngroup,
          run: act.ungroupSelection,
        },
        {
          id: "canvas.toggleCollapse",
          title: t("commands.toggleCollapse"),
          group: canvas,
          shortcut: "alt+c",
          icon: <LuChevronsDownUp />,
          when: () => act.selectedGroups().length > 0,
          run: () => act.selectedGroups().forEach(act.toggleCollapsed),
        },
        {
          id: "canvas.addNote",
          title: t("commands.addNote"),
          group: canvas,
          shortcut: "shift+n",
          icon: <LuStickyNote />,
          keywords: ["sticky", "comment"],
          run: () => void act.addNote(),
        },
        {
          id: "canvas.autoLayout",
          title: t("commands.autoLayout"),
          group: canvas,
          shortcut: "shift+l",
          icon: <LuLayoutDashboard />,
          when: () => useBuilder.getState().nodes.length > 0,
          run: act.runAutoLayout,
        },
        {
          id: "canvas.fitView",
          title: t("commands.fitView"),
          group: canvas,
          shortcut: "shift+f",
          icon: <LuFocus />,
          run: () => act.fitView(),
        },
        {
          id: "canvas.toggleSnap",
          title: t("commands.toggleSnap"),
          group: canvas,
          shortcut: "shift+s",
          icon: <LuGrid3X3 />,
          run: () => useCanvasUi.getState().toggleSnap(),
        },
        {
          id: "canvas.toggleMinimap",
          title: t("commands.toggleMinimap"),
          group: canvas,
          shortcut: "shift+m",
          icon: <LuMap />,
          run: () => useCanvasUi.getState().toggleMinimap(),
        },
        ...nudge("Left", -1, 0),
        ...nudge("Right", 1, 0),
        ...nudge("Up", 0, -1),
        ...nudge("Down", 0, 1),
      ]),
    );
  }, [t]);
}

/** Undo/redo act on the document in every view; everything else needs the visible canvas. */
const DOCUMENT_COMMANDS = new Set(["edit.undo", "edit.redo"]);

/**
 * Canvas commands stay registered while the canvas is mounted but hidden (code mode, a raw
 * code DAG): they must not act on what the user cannot see (Delete, arrows, Shift+A…).
 */
function canvasOnly(list: Command[]): Command[] {
  return list.map((c) => {
    if (DOCUMENT_COMMANDS.has(c.id)) return c;
    const when = c.when;
    return { ...c, when: () => canvasActive() && (!when || when()) };
  });
}

const isEditable = (el: EventTarget | null): boolean => {
  if (!(el instanceof HTMLElement)) return false;
  return (
    el.isContentEditable ||
    ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName) ||
    !!el.closest(".cm-editor, [contenteditable='true']")
  );
};

/**
 * Keyboard copy / cut / paste through the browser's clipboard events: they work without
 * clipboard permissions. Only active when focus is on the canvas (or nowhere in particular).
 */
export function useClipboardEvents(root: () => HTMLElement | null): void {
  useEffect(() => {
    const ours = (e: Event) => {
      if (isEditable(e.target) || isEditable(document.activeElement)) return false;
      const el = root();
      const target = e.target instanceof Node ? e.target : null;
      return (
        target === document.body || target === document.documentElement || (!!el && !!target && el.contains(target))
      );
    };
    const onCopy = (e: ClipboardEvent, cut: boolean) => {
      if (!ours(e) || hasTextSelection() || !act.hasSelection()) return;
      const ids = act.selection();
      const json = act.serializeSelection();
      if (!json || !e.clipboardData) return;
      e.clipboardData.setData("text/plain", json);
      e.preventDefault();
      if (cut) useBuilder.getState().removeElements(ids);
    };
    const copy = (e: ClipboardEvent) => onCopy(e, false);
    const cut = (e: ClipboardEvent) => onCopy(e, true);
    const paste = (e: ClipboardEvent) => {
      if (!ours(e)) return;
      const text = e.clipboardData?.getData("text/plain");
      if (text && act.pasteText(text)) e.preventDefault();
    };
    document.addEventListener("copy", copy);
    document.addEventListener("cut", cut);
    document.addEventListener("paste", paste);
    return () => {
      document.removeEventListener("copy", copy);
      document.removeEventListener("cut", cut);
      document.removeEventListener("paste", paste);
    };
  }, [root]);
}

export { isEditable };
