import { useEffect } from "react";
import { useTranslation } from "react-i18next";
import { LuCommand, LuKeyboard, LuMoon, LuPanelBottom, LuPanelLeft, LuPanelRight, LuPlus } from "react-icons/lu";

import { canvasActive, isRawDag } from "../codemode/store";
import { registerCommands } from "../lib/commands";
import { useBuilder } from "../store/builder";
import { useColorMode } from "./colorMode";
import { useLayout } from "./layout";
import { CommandPalette } from "./CommandPalette";
import { KeyboardShortcuts } from "./KeyboardShortcuts";
import { useShell } from "./shellStore";
import { ShortcutsSheet } from "./ShortcutsSheet";

function useShellCommands() {
  const { t } = useTranslation("canvas");
  useEffect(() => {
    const group = t("groups.general");
    return registerCommands(
      {
        id: "shell.commandPalette",
        title: t("commands.commandPalette"),
        group,
        shortcut: "mod+k",
        icon: <LuCommand />,
        hidden: true,
        run: () => useShell.getState().openPalette(),
      },
      {
        id: "shell.insert",
        title: t("commands.insert"),
        group,
        shortcut: "shift+a",
        icon: <LuPlus />,
        when: canvasActive,
        run: () => useShell.getState().openPalette("insert"),
      },
      {
        id: "shell.shortcuts",
        title: t("commands.shortcuts"),
        group,
        shortcut: "?",
        icon: <LuKeyboard />,
        run: () => useShell.getState().setShortcutsOpen(true),
      },
      {
        id: "shell.togglePalette",
        title: t("commands.togglePalette"),
        group,
        shortcut: "mod+b",
        icon: <LuPanelLeft />,
        when: canvasActive,
        run: () => useLayout.getState().toggle("palette"),
      },
      {
        id: "shell.toggleInspector",
        title: t("commands.toggleInspector"),
        group,
        shortcut: "mod+i",
        icon: <LuPanelRight />,
        // Raw code DAGs keep the inspector (their settings) beside the editor.
        when: () => canvasActive() || isRawDag(useBuilder.getState().dag),
        run: () => useLayout.getState().toggle("inspector"),
      },
      {
        id: "shell.toggleBottom",
        title: t("commands.toggleBottom"),
        group,
        shortcut: "mod+j",
        icon: <LuPanelBottom />,
        run: () => useLayout.getState().toggle("bottom"),
      },
      {
        id: "shell.toggleColorMode",
        title: t("commands.toggleColorMode"),
        group,
        icon: <LuMoon />,
        when: () => useColorMode.getState().canToggle,
        run: () => useColorMode.getState().toggle(),
      },
    );
  }, [t]);
}

/**
 * Global shell pieces: keyboard dispatcher, command palette (Ctrl/⌘+K) and shortcuts
 * sheet ("?"). Mount once inside the builder layout (its DOM parent scopes the shortcuts).
 */
export function ShellOverlays() {
  useShellCommands();
  return (
    <>
      <KeyboardShortcuts />
      <CommandPalette />
      <ShortcutsSheet />
    </>
  );
}
