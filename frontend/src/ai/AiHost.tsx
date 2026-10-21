import { Suspense, lazy, useEffect } from "react";
import { useTranslation } from "react-i18next";
import { LuCircleArrowUp, LuBot, LuShieldCheck } from "react-icons/lu";

import { registerCommands } from "../lib/commands";
import { aiUsable, getAiStatus } from "../lifecycle/lib/ai";
import { getMeta } from "../lifecycle/runtime";
import { ErrorBoundary } from "../ui/ErrorBoundary";
import { useAiChat } from "./store";

const ChatPanel = lazy(() => import("./ChatPanel"));
const TroubleshootDialog = lazy(() => import("./TroubleshootDialog"));
const UpgradeDialog = lazy(() => import("./UpgradeDialog"));

/** Keyboard shortcut of the chat panel (also shown in the AI menu). */
export const CHAT_SHORTCUT = "mod+shift+a";

function aiEnabled(): boolean {
  const meta = getMeta();
  const permissions = meta?.iam?.permissions;
  const allowed = !permissions || permissions.includes("ai:use");
  return allowed && aiUsable(getAiStatus(), !!meta?.ai_enabled);
}

function useAiCommands(): void {
  const { t, i18n } = useTranslation("ai");
  useEffect(() => {
    const group = t("commands.group");
    return registerCommands(
      {
        id: "ai.chat",
        title: t("commands.chat"),
        group,
        shortcut: CHAT_SHORTCUT,
        icon: <LuBot />,
        keywords: ["ask", "assistant", "chat", "ai"],
        when: aiEnabled,
        run: () => useAiChat.getState().toggle(),
      },
      {
        id: "ai.chatAdmin",
        title: t("commands.chatAdmin"),
        group,
        icon: <LuShieldCheck />,
        keywords: ["admin", "assistant", "configuration", "audit"],
        when: () => aiEnabled() && getAiStatus()?.role === "admin",
        run: () => useAiChat.getState().openPanel({ mode: "admin" }),
      },
      {
        id: "ai.upgrade",
        title: t("commands.upgrade"),
        group,
        icon: <LuCircleArrowUp />,
        keywords: ["airflow 3", "migration", "deprecated"],
        run: () => useAiChat.getState().setUpgradeOpen(true),
      },
    );
  }, [t, i18n.language]);
}

/**
 * Mounts the AI chat panel, the troubleshooting and upgrade dialogs (lazily, once used)
 * and registers their commands (Ctrl/⌘+Shift+A opens the chat).
 */
export function AiHost() {
  useAiCommands();
  const open = useAiChat((s) => s.open);
  const used = useAiChat((s) => s.open || s.messages.length > 0 || !!s.pending);
  const troubleshoot = useAiChat((s) => s.troubleshoot);
  const upgrade = useAiChat((s) => s.upgradeOpen);
  return (
    <ErrorBoundary
      area="dialog"
      onReset={() => {
        useAiChat.getState().close();
        useAiChat.getState().closeTroubleshoot();
        useAiChat.getState().setUpgradeOpen(false);
      }}
    >
      <Suspense fallback={null}>
        {open || used ? <ChatPanel /> : null}
        {troubleshoot ? (
          <TroubleshootDialog key={`${troubleshoot.runId}|${troubleshoot.taskId ?? ""}`} target={troubleshoot} />
        ) : null}
        {upgrade ? <UpgradeDialog /> : null}
      </Suspense>
    </ErrorBoundary>
  );
}
