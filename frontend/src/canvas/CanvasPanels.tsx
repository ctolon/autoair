import { Box, CloseButton, HStack, IconButton, Stack, Text } from "@chakra-ui/react";
import { Panel } from "@xyflow/react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { LuChevronDown, LuChevronUp } from "react-icons/lu";

import { Tip } from "../shell/Tip";
import { uiDefaults } from "../shell/uiDefaults";

import { useBuilder } from "../store/builder";
import { LEGEND_STATES, runStateStyle, shortRunLabel } from "./runState";

const LEGEND_KEY = "autoair.runLegend.open";

function readLegendOpen(): boolean {
  try {
    return localStorage.getItem(LEGEND_KEY) === "1";
  } catch {
    return false;
  }
}

/** The painted DAG run (type + local time) and a collapsible legend of run-state colors. */
export function RunLegend() {
  const { t, i18n } = useTranslation("canvas");
  const run = useBuilder((s) => s.runOverlay);
  const [open, setOpen] = useState(readLegendOpen);
  if (!run) return null;
  const ui = uiDefaults();
  const label = shortRunLabel(run.runId, ui.locale ?? i18n.language, ui.timeZone);
  const title = label.type
    ? t("run.shortTitle", {
        type: t(`run.types.${label.type}`, { defaultValue: label.type }),
        when: label.when ?? "",
      })
    : t("run.title", { runId: run.runId });
  const toggle = () => {
    setOpen(!open);
    try {
      localStorage.setItem(LEGEND_KEY, open ? "0" : "1");
    } catch {
      /* storage unavailable */
    }
  };
  return (
    <Panel position="top-left">
      <Box
        bg="bg.panel"
        borderWidth="1px"
        borderColor="border.emphasized"
        rounded="md"
        shadow="sm"
        px={3}
        py={1.5}
        maxW="280px"
        role="region"
        aria-label={t("run.legend")}
        data-testid="run-legend"
      >
        <HStack justify="space-between" gap={1}>
          <Tip content={run.runId}>
            <Text fontSize="xs" fontWeight="semibold" truncate>
              {title}
            </Text>
          </Tip>
          <HStack gap={0}>
            <IconButton
              size="2xs"
              variant="ghost"
              aria-expanded={open}
              aria-label={t(open ? "run.hideLegend" : "run.showLegend")}
              title={t(open ? "run.hideLegend" : "run.showLegend")}
              onClick={toggle}
            >
              {open ? <LuChevronUp /> : <LuChevronDown />}
            </IconButton>
            <CloseButton
              size="2xs"
              aria-label={t("run.clear")}
              onClick={() => useBuilder.getState().setRunOverlay(null)}
            />
          </HStack>
        </HStack>
        {open ? (
          <Box display="grid" gridTemplateColumns="1fr 1fr" columnGap={3} rowGap={0.5} mt={1} mb={0.5}>
            {LEGEND_STATES.map((s) => (
              <HStack key={s} gap={1.5}>
                <Box w="10px" h="10px" rounded="sm" bg={runStateStyle(s).color} flexShrink={0} aria-hidden />
                <Text fontSize="2xs">{t(`state.${s}`)}</Text>
              </HStack>
            ))}
          </Box>
        ) : null}
      </Box>
    </Panel>
  );
}

/** Diff overlay summary: legend plus the nodes removed in the compared version (not on the canvas). */
export function DiffPanel() {
  const { t } = useTranslation("canvas");
  const diff = useBuilder((s) => s.diffOverlay);
  if (!diff) return null;
  return (
    <Panel position="top-right">
      <Box
        bg="bg.panel"
        borderWidth="1px"
        borderColor="border.emphasized"
        rounded="md"
        shadow="sm"
        px={3}
        py={2}
        w="240px"
        role="region"
        aria-label={t("diff.title", { label: diff.label })}
      >
        <HStack justify="space-between" mb={1}>
          <Text fontSize="xs" fontWeight="semibold" truncate>
            {t("diff.title", { label: diff.label })}
          </Text>
          <CloseButton
            size="2xs"
            aria-label={t("diff.clear")}
            onClick={() => useBuilder.getState().setDiffOverlay(null)}
          />
        </HStack>
        <Stack gap={0.5} fontSize="2xs" mb={diff.removed.length ? 2 : 0}>
          <HStack gap={1.5}>
            <Box w="10px" h="10px" rounded="sm" bg="green.400" aria-hidden />
            {t("diff.addedCount", { count: diff.added.length })}
          </HStack>
          <HStack gap={1.5}>
            <Box w="10px" h="10px" rounded="sm" bg="yellow.400" aria-hidden />
            {t("diff.changedCount", { count: diff.changed.length })}
          </HStack>
          <HStack gap={1.5}>
            <Box
              w="10px"
              h="10px"
              rounded="sm"
              borderWidth="2px"
              borderStyle="dashed"
              borderColor="red.400"
              aria-hidden
            />
            {t("diff.removedCount", { count: diff.removed.length })}
          </HStack>
        </Stack>
        {diff.removed.length ? (
          <>
            <Text fontSize="2xs" fontWeight="semibold" color="fg.muted" textTransform="uppercase" mb={1}>
              {t("diff.removedTitle")}
            </Text>
            <Stack as="ul" gap={1} maxH="180px" overflowY="auto" className="nowheel" listStyleType="none">
              {diff.removed.map((id) => (
                <Box
                  as="li"
                  key={id}
                  fontSize="xs"
                  fontFamily="mono"
                  px={2}
                  py={0.5}
                  rounded="sm"
                  borderWidth="1px"
                  borderStyle="dashed"
                  borderColor="red.400"
                  color="fg.muted"
                  textDecoration="line-through"
                  truncate
                >
                  {id}
                </Box>
              ))}
            </Stack>
          </>
        ) : null}
      </Box>
    </Panel>
  );
}
