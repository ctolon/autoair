import { HStack, Menu, Text } from "@chakra-ui/react";
import { useTranslation } from "react-i18next";
import { LuCircleArrowUp, LuBotMessageSquare, LuShieldCheck } from "react-icons/lu";

import { formatShortcut } from "../lib/commands";
import { useAccess } from "../lib/access";
import { useAiStatus } from "../lifecycle/lib/ai";
import { Tip } from "../shell/Tip";
import { CHAT_SHORTCUT } from "./AiHost";
import { useAiChat } from "./store";

function Item({ tip, ...props }: { tip?: string | undefined } & Menu.ItemProps) {
  const item = <Menu.Item {...props} />;
  return tip ? (
    <Tip content={tip} placement="right">
      {item}
    </Tip>
  ) : (
    item
  );
}

/** "Ask AI" (developer / admin assistant) and the upgrade assistant in the AI menu. */
export function AiAssistantMenuItems({ enabled, off }: { enabled: boolean; off?: string | undefined }) {
  const { t } = useTranslation("ai");
  const access = useAccess();
  const status = useAiStatus();
  // The admin assistant: ai:admin or admin:panel (servers without IAM: the AI admin role).
  const admin =
    status?.role === "admin" || (access.roles.length > 0 && (access.can("ai:admin") || access.can("admin:panel")));
  return (
    <>
      <Item
        value="ai-chat"
        disabled={!enabled}
        tip={off}
        onClick={() => useAiChat.getState().openPanel({ mode: "developer" })}
      >
        <HStack justify="space-between" w="full" gap={3}>
          <HStack gap={2}>
            <LuBotMessageSquare /> {t("menu.chat")}
          </HStack>
          <Text fontSize="2xs" color="fg.muted">
            {formatShortcut(CHAT_SHORTCUT)}
          </Text>
        </HStack>
      </Item>
      {admin ? (
        <Item
          value="ai-chat-admin"
          disabled={!enabled}
          tip={off}
          onClick={() => useAiChat.getState().openPanel({ mode: "admin" })}
        >
          <LuShieldCheck /> {t("menu.adminChat")}
        </Item>
      ) : null}
      <Item value="ai-upgrade" onClick={() => useAiChat.getState().setUpgradeOpen(true)}>
        <LuCircleArrowUp /> {t("menu.upgrade")}
      </Item>
    </>
  );
}
