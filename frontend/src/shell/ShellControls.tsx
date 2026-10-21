import { Badge, Box, HStack, IconButton, Menu, Portal } from "@chakra-ui/react";
import { useTranslation } from "react-i18next";
import {
  LuCircleUser,
  LuCommand,
  LuEllipsis,
  LuKeyboard,
  LuLanguages,
  LuLogOut,
  LuMoon,
  LuPanelBottom,
  LuPanelLeft,
  LuPanelRight,
  LuSettings2,
  LuSun,
} from "react-icons/lu";

import { AdminMenuItem, AdminPanelHost } from "../admin/AdminEntry";
import { SUPPORTED_LANGUAGES, setLanguage } from "../i18n";
import { useMeta } from "../lifecycle/hooks";
import { useLifecycle } from "../lifecycle/store";
import { formatShortcut } from "../lib/commands";
import { useColorMode } from "./colorMode";
import { isPanelOpen, useLayout } from "./layout";
import { signOut } from "./LoginGate";
import { useShell } from "./shellStore";
import { Tip } from "./Tip";

/** Signed-in user, AutoAir roles and (standalone with login) sign out. */
function UserMenu() {
  const { t } = useTranslation("canvas");
  const meta = useMeta();
  const iam = meta?.iam;
  if (!iam?.user) return null;
  const canSignOut = iam.login_enabled || iam.auth === "bearer";
  return (
    <>
      <Menu.Root>
        <Menu.Trigger asChild>
          <IconButton size="xs" variant="ghost" aria-label={t("shell.user", { user: iam.user })} title={iam.user}>
            <LuCircleUser />
          </IconButton>
        </Menu.Trigger>
        <Portal>
          <Menu.Positioner>
            <Menu.Content minW="220px">
              <Menu.ItemGroup>
                <Menu.ItemGroupLabel>{iam.user}</Menu.ItemGroupLabel>
                <Box px={2} pb={2} display="flex" flexWrap="wrap" gap={1} data-testid="user-roles">
                  {iam.roles.map((r) => {
                    const perms = iam.role_permissions?.[r] ?? [];
                    return (
                      <Tip
                        key={r}
                        // Beside the menu, not over the items below it (Admin panel, sign out).
                        placement="left-start"
                        content={
                          perms.length ? `${t("shell.rolePermissions", { role: r })}\n${perms.join("\n")}` : null
                        }
                      >
                        <Badge size="xs" variant="surface" tabIndex={0} cursor="help">
                          {r}
                        </Badge>
                      </Tip>
                    );
                  })}
                </Box>
              </Menu.ItemGroup>
              {iam.permissions.includes("admin:config") && meta?.expose_config && meta.expose_config !== "false" ? (
                <>
                  <Menu.Separator />
                  <Menu.Item value="config" onClick={() => useLifecycle.getState().openDialog({ kind: "config" })}>
                    <LuSettings2 /> {t("shell.configuration")}
                  </Menu.Item>
                </>
              ) : null}
              <AdminMenuItem />
              {canSignOut ? (
                <>
                  <Menu.Separator />
                  <Menu.Item
                    value="sign-out"
                    onClick={() => {
                      void signOut().then(() => window.location.reload());
                    }}
                  >
                    <LuLogOut /> {t("shell.signOut")}
                  </Menu.Item>
                </>
              ) : null}
            </Menu.Content>
          </Menu.Positioner>
        </Portal>
      </Menu.Root>
      <AdminPanelHost />
    </>
  );
}

/**
 * Low-priority shell actions behind a "⋯" button (keeps the header on one row): command
 * palette, shortcuts, language, color mode and the panel toggles.
 */
function MoreMenu() {
  const { t, i18n } = useTranslation("canvas");
  const current = (i18n.resolvedLanguage ?? i18n.language ?? "en").split("-")[0] ?? "en";
  const mode = useColorMode((s) => s.mode);
  const canToggle = useColorMode((s) => s.canToggle);
  const open = {
    palette: useLayout((s) => isPanelOpen(s, "palette")),
    inspector: useLayout((s) => isPanelOpen(s, "inspector")),
    bottom: useLayout((s) => isPanelOpen(s, "bottom")),
  };
  return (
    <Menu.Root positioning={{ placement: "bottom-end" }}>
      <Menu.Trigger asChild>
        <IconButton
          size="xs"
          variant="ghost"
          aria-label={t("shell.more")}
          title={t("shell.more")}
          data-testid="more-menu"
        >
          <LuEllipsis />
        </IconButton>
      </Menu.Trigger>
      <Portal>
        <Menu.Positioner>
          <Menu.Content minW="240px">
            <Menu.Item value="command-palette" onClick={() => useShell.getState().openPalette()}>
              <LuCommand /> <Box flex="1">{t("shell.commandPaletteItem")}</Box>
              <Menu.ItemCommand>{formatShortcut("mod+k")}</Menu.ItemCommand>
            </Menu.Item>
            <Menu.Item value="shortcuts" onClick={() => useShell.getState().setShortcutsOpen(true)}>
              <LuKeyboard /> <Box flex="1">{t("shell.shortcutsItem")}</Box>
              <Menu.ItemCommand>?</Menu.ItemCommand>
            </Menu.Item>
            <Menu.Separator />
            {(["palette", "inspector", "bottom"] as const).map((id) => (
              <Menu.Item key={id} value={`toggle-${id}`} onClick={() => useLayout.getState().toggle(id)}>
                {PANEL_ICON[id]} <Box flex="1">{t(open[id] ? `shell.hideItem.${id}` : `shell.showItem.${id}`)}</Box>
                <Menu.ItemCommand>{formatShortcut(PANEL_KEY[id])}</Menu.ItemCommand>
              </Menu.Item>
            ))}
            <Menu.Separator />
            {canToggle ? (
              <Menu.Item value="color-mode" onClick={() => useColorMode.getState().toggle()}>
                {mode === "dark" ? <LuSun /> : <LuMoon />}{" "}
                {mode === "dark" ? t("shell.lightMode") : t("shell.darkMode")}
              </Menu.Item>
            ) : null}
            <Menu.ItemGroup>
              <Menu.ItemGroupLabel fontSize="xs">
                <LuLanguages style={{ display: "inline", verticalAlign: "-2px" }} /> {t("shell.language")}
              </Menu.ItemGroupLabel>
              <Menu.RadioItemGroup value={current} onValueChange={(e) => setLanguage(e.value)}>
                {SUPPORTED_LANGUAGES.map((lng) => (
                  <Menu.RadioItem key={lng} value={lng}>
                    {t(`languages.${lng}`, { ns: "common", defaultValue: lng })}
                    <Menu.ItemIndicator />
                  </Menu.RadioItem>
                ))}
              </Menu.RadioItemGroup>
            </Menu.ItemGroup>
          </Menu.Content>
        </Menu.Positioner>
      </Portal>
    </Menu.Root>
  );
}

const PANEL_ICON = { palette: <LuPanelLeft />, inspector: <LuPanelRight />, bottom: <LuPanelBottom /> } as const;
const PANEL_KEY = { palette: "mod+b", inspector: "mod+i", bottom: "mod+j" } as const;

/** Right end of the header: the signed-in user and the "⋯" menu. */
export function ShellControls() {
  return (
    <HStack gap={0.5} flexShrink={0}>
      <UserMenu />
      <MoreMenu />
    </HStack>
  );
}
