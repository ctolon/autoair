import { Box, Button, Card, Stack, Text } from "@chakra-ui/react";
import { useTranslation } from "react-i18next";
import { LuCode, LuFileCode, LuLayoutTemplate, LuSearch, LuSparkles } from "react-icons/lu";

import { editorAvailability, useCodeMode } from "../codemode/store";
import { newRawDag } from "../lifecycle/actions";
import { useMeta } from "../lifecycle/hooks";
import { aiUsable, useAiStatus } from "../lifecycle/lib/ai";
import { useLifecycle } from "../lifecycle/store";
import { useAccess } from "../lib/access";
import { formatShortcut } from "../lib/commands";
import { useShell } from "../shell/shellStore";
import { useBuilder } from "../store/builder";

/** Shown on an empty canvas: the ways to start a DAG. */
export function EmptyState() {
  const { t } = useTranslation("canvas");
  const empty = useBuilder((s) => s.nodes.length === 0);
  const access = useAccess();
  const meta = useMeta();
  const status = useAiStatus();
  if (!empty) return null;
  const ai = access.feature("ai") && access.can("ai:use") && aiUsable(status, !!meta?.ai_enabled);
  const templates = access.feature("templates");
  const availability = editorAvailability(access);
  const code = availability.both;
  return (
    <Box
      position="absolute"
      inset={0}
      display="flex"
      alignItems="center"
      justifyContent="center"
      pointerEvents="none"
      zIndex={4}
      p={4}
    >
      <Card.Root
        pointerEvents="auto"
        maxW="380px"
        w="100%"
        size="sm"
        variant="outline"
        bg="bg.panel"
        shadow="sm"
        data-testid="empty-canvas"
      >
        <Card.Body gap={3}>
          <Stack gap={1}>
            <Text fontWeight="semibold">{t("emptyState.title")}</Text>
            <Text fontSize="sm" color="fg.muted">
              {t("emptyState.help")}
            </Text>
          </Stack>
          <Stack gap={1.5}>
            <Button
              size="sm"
              variant="outline"
              justifyContent="start"
              onClick={() => useShell.getState().openPalette("insert")}
            >
              <LuSearch />{" "}
              <Box flex="1" textAlign="start">
                {t("emptyState.search")}
              </Box>
              <Text as="span" fontSize="2xs" color="fg.muted">
                {formatShortcut("shift+a")}
              </Text>
            </Button>
            {templates ? (
              <Button
                size="sm"
                variant="outline"
                justifyContent="start"
                onClick={() => useLifecycle.getState().openDialog({ kind: "templates" })}
              >
                <LuLayoutTemplate /> {t("emptyState.template")}
              </Button>
            ) : null}
            {ai ? (
              <Button
                size="sm"
                variant="outline"
                justifyContent="start"
                onClick={() => useLifecycle.getState().openDialog({ kind: "aiGenerate" })}
              >
                <LuSparkles /> {t("emptyState.ai")}
              </Button>
            ) : null}
            {code ? (
              <Button
                size="sm"
                variant="outline"
                justifyContent="start"
                onClick={() => useCodeMode.getState().setMode("code")}
              >
                <LuCode /> {t("emptyState.code")}
              </Button>
            ) : null}
            {availability.canRaw ? (
              <Button
                size="sm"
                variant="outline"
                justifyContent="start"
                onClick={newRawDag}
                data-testid="empty-new-raw"
              >
                <LuFileCode /> {t("emptyState.raw")}
              </Button>
            ) : null}
          </Stack>
        </Card.Body>
      </Card.Root>
    </Box>
  );
}
