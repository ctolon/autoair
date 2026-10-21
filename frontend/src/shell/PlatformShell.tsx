import { Box, HStack, Text } from "@chakra-ui/react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { setToken } from "../api/setup";
import { i18n, SUPPORTED_LANGUAGES } from "../i18n";
import { useMeta } from "../lifecycle/hooks";
import { toaster } from "../ui/toaster";
import { LoginGate } from "./LoginGate";
import { fetchAuthConfig, startSessionWatcher } from "./session";
import { setUiDefaults } from "./uiDefaults";

const HEX = /^#[0-9a-fA-F]{3,8}$/;

/** A Chakra palette name or hex colour → background/foreground of the banner. */
function bannerColors(color: string): { bg: string; color: string } {
  if (HEX.test(color)) return { bg: color, color: "white" };
  return { bg: `${color}.solid`, color: `${color}.contrast` };
}

function storedLanguage(): string | null {
  try {
    return localStorage.getItem("autoair.lang") ?? localStorage.getItem("i18nextLng");
  } catch {
    return null;
  }
}

/**
 * Server-driven shell pieces, rendered once above the builder:
 *  - the environment/maintenance banner (`ui.banner_text`, e.g. "PRODUCTION");
 *  - branding: page title (standalone), accent strip; default language and time zone;
 *  - the session watcher: refreshes Airflow cookie sessions before they expire, and signs
 *    AutoAir out when the Airflow session ends (sign-out in Airflow, revoked token).
 */
export function PlatformShell() {
  const { t } = useTranslation();
  const meta = useMeta();
  const [ended, setEnded] = useState(false);
  const ui = meta?.ui;

  useEffect(() => {
    if (!ui) return;
    setUiDefaults({ timeZone: ui.default_timezone ?? undefined, locale: ui.date_locale ?? undefined });
    if (import.meta.env.MODE !== "plugin" && ui.title) document.title = ui.title;
    const lang = ui.default_language;
    if (lang && !storedLanguage() && SUPPORTED_LANGUAGES.includes(lang) && i18n.language !== lang) {
      void i18n.changeLanguage(lang);
    }
  }, [ui]);

  const mode = meta?.mode;
  const auth = meta?.auth;
  useEffect(() => {
    if (!mode || auth === "none") return;
    let stop: (() => void) | undefined;
    let alive = true;
    void fetchAuthConfig().then((config) => {
      if (!alive || !config) return;
      stop = startSessionWatcher(config, {
        onEnded: () => {
          setToken(null);
          setEnded(true);
        },
        onExpiring: (minutes) =>
          toaster.create({ type: "warning", title: t("session.expiring", { minutes }), duration: 30_000 }),
      });
    });
    return () => {
      alive = false;
      stop?.();
    };
  }, [mode, auth, t]);

  const banner = ui?.banner_text?.trim();
  const accent = ui?.accent_color;
  return (
    <>
      {accent ? (
        <Box h="3px" flexShrink={0} bg={HEX.test(accent) ? accent : `${accent}.solid`} data-testid="accent-strip" />
      ) : null}
      {banner ? (
        <HStack
          role="status"
          aria-live="polite"
          justify="center"
          px={3}
          py={0.5}
          flexShrink={0}
          {...bannerColors(ui?.banner_color ?? "orange")}
          data-testid="environment-banner"
        >
          <Text fontSize="xs" fontWeight="bold" letterSpacing="wide" lineClamp={1}>
            {banner}
          </Text>
        </HStack>
      ) : null}
      {ended ? (
        <Box position="fixed" inset={0} zIndex="modal" bg="bg/80" overflowY="auto" data-testid="session-ended">
          <LoginGate message={t("session.ended")} />
        </Box>
      ) : null}
    </>
  );
}
