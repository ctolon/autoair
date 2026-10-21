import { Box, CloseButton, Dialog, HStack, IconButton, Portal, Tabs, Text } from "@chakra-ui/react";
import { useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import {
  LuBot,
  LuLibrary,
  LuBookMarked,
  LuDatabase,
  LuPackageSearch,
  LuFileStack,
  LuGauge,
  LuHardDrive,
  LuPlug,
  LuRefreshCw,
  LuScrollText,
  LuSettings2,
  LuToggleRight,
  LuShieldCheck,
  LuUsers,
} from "react-icons/lu";

import { useMeta } from "../lifecycle/hooks";
import { ADMIN_TABS, type AdminTab, useAdmin } from "./store";
import { AiRulesTab } from "./tabs/AiRulesTab";
import { AiUsageTab } from "./tabs/AiUsageTab";
import { AuditTab } from "./tabs/AuditTab";
import { CachesTab } from "./tabs/CachesTab";
import { CatalogTab } from "./tabs/CatalogTab";
import { ConfigTab } from "./tabs/ConfigTab";
import { DatabaseTab } from "./tabs/DatabaseTab";
import { FeaturesTab } from "./tabs/FeaturesTab";
import { KbTab } from "./tabs/KbTab";
import { McpServersTab } from "./tabs/McpServersTab";
import { OverviewTab } from "./tabs/OverviewTab";
import { ProposalsTab } from "./tabs/ProposalsTab";
import { TemplatesTab } from "./tabs/TemplatesTab";
import { UsersTab } from "./tabs/UsersTab";

const ICONS: Record<AdminTab, typeof LuGauge> = {
  overview: LuGauge,
  features: LuToggleRight,
  audit: LuScrollText,
  users: LuUsers,
  ai: LuBot,
  aiRules: LuBookMarked,
  mcp: LuPlug,
  templates: LuFileStack,
  proposals: LuShieldCheck,
  database: LuDatabase,
  catalog: LuPackageSearch,
  kb: LuLibrary,
  caches: LuHardDrive,
  config: LuSettings2,
};

/** Full-page admin panel (admins: `admin:panel`). Opened from the user menu. */
export default function AdminPanel() {
  const { t, i18n } = useTranslation("admin");
  const open = useAdmin((s) => s.open);
  const tab = useAdmin((s) => s.tab);
  const meta = useMeta();
  const client = useQueryClient();
  const perms = meta?.iam?.permissions ?? [];
  const canConfig = perms.includes("admin:config");
  const canTemplates = perms.includes("template:write");
  const canAiAdmin = perms.includes("ai:admin");
  const canCatalog = perms.includes("catalog:refresh");
  const canKb = perms.includes("ai:admin") || canConfig;
  const exposed = !!meta?.expose_config && meta.expose_config !== "false";

  const body = (name: AdminTab) => {
    switch (name) {
      case "overview":
        return <OverviewTab />;
      case "features":
        return <FeaturesTab canWrite={canConfig} />;
      case "audit":
        return <AuditTab />;
      case "users":
        return <UsersTab />;
      case "ai":
        return <AiUsageTab />;
      case "aiRules":
        return <AiRulesTab canWrite={canAiAdmin} />;
      case "mcp":
        return <McpServersTab canWrite={canConfig} />;
      case "templates":
        return <TemplatesTab canWrite={canTemplates} />;
      case "proposals":
        return <ProposalsTab />;
      case "database":
        return <DatabaseTab canWrite={canConfig} />;
      case "kb":
        return <KbTab canWrite={canKb} />;
      case "catalog":
        return <CatalogTab canRefresh={canCatalog} />;
      case "caches":
        return <CachesTab canWrite={canConfig} />;
      case "config":
        return <ConfigTab exposed={exposed} />;
    }
  };

  return (
    <Dialog.Root
      open={open}
      onOpenChange={(e) => !e.open && useAdmin.getState().close()}
      size="full"
      scrollBehavior="inside"
      lazyMount
      unmountOnExit
    >
      <Portal>
        <Dialog.Backdrop />
        <Dialog.Positioner>
          <Dialog.Content
            data-testid="admin-panel"
            lang={i18n.resolvedLanguage ?? i18n.language}
            bg="bg"
            css={{ colorScheme: "light", _dark: { colorScheme: "dark" } }}
          >
            <Dialog.Header borderBottomWidth="1px" py={3}>
              <HStack justify="space-between" w="full" gap={3} pe={10}>
                <Box minW={0}>
                  <Dialog.Title>{t("title")}</Dialog.Title>
                  <Dialog.Description fontSize="sm" color="fg.muted">
                    {t("subtitle")}
                  </Dialog.Description>
                </Box>
                <IconButton
                  size="sm"
                  variant="ghost"
                  aria-label={t("refresh")}
                  title={t("refresh")}
                  onClick={() => void client.invalidateQueries()}
                >
                  <LuRefreshCw />
                </IconButton>
              </HStack>
            </Dialog.Header>
            <Dialog.Body p={0} display="flex" flexDirection="column" minH={0}>
              <Tabs.Root
                value={tab}
                onValueChange={(e) => useAdmin.getState().setTab(e.value as AdminTab)}
                lazyMount
                unmountOnExit
                variant="line"
                display="flex"
                flexDirection="column"
                flex="1"
                minH={0}
              >
                <Tabs.List
                  aria-label={t("tabs.label")}
                  px={4}
                  overflowX="auto"
                  flexShrink={0}
                  css={{ scrollbarWidth: "thin" }}
                >
                  {ADMIN_TABS.map((name) => {
                    const Icon = ICONS[name];
                    return (
                      <Tabs.Trigger key={name} value={name} data-testid={`admin-tab-${name}`} whiteSpace="nowrap">
                        <Icon aria-hidden />
                        <Text as="span">{t(`tabs.${name}`)}</Text>
                      </Tabs.Trigger>
                    );
                  })}
                </Tabs.List>
                {ADMIN_TABS.map((name) => (
                  <Tabs.Content key={name} value={name} px={{ base: 3, md: 6 }} py={4} flex="1" overflowY="auto">
                    {body(name)}
                  </Tabs.Content>
                ))}
              </Tabs.Root>
            </Dialog.Body>
            <Dialog.CloseTrigger asChild>
              <CloseButton size="sm" aria-label={t("close")} />
            </Dialog.CloseTrigger>
          </Dialog.Content>
        </Dialog.Positioner>
      </Portal>
    </Dialog.Root>
  );
}
