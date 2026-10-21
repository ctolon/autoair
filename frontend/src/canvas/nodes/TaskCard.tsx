import { Badge, Box, HStack, Text } from "@chakra-ui/react";
import { Handle, Position } from "@xyflow/react";
import { type ReactNode, memo } from "react";
import { useTranslation } from "react-i18next";
import { LuArrowDownToLine, LuArrowUpFromLine, LuEyeOff, LuFlag, LuLayers } from "react-icons/lu";
import { useShallow } from "zustand/react/shallow";
import { problemText } from "../../lib/problemMessages";

import type { TaskSpec } from "../../lib/types";
import type { DiffOverlay } from "../../store/builder";
import { useBuilder } from "../../store/builder";
import { runStateStyle } from "../runState";
import { TaskToolbar } from "./TaskToolbar";

type Props = {
  id: string;
  selected: boolean;
  title: string;
  subtitle?: string | undefined;
  icon: ReactNode;
  accent: string;
  spec: TaskSpec;
  /** Small icons describing the task's nature (sensor, branch, approval…). */
  markers?: ReactNode;
};

type DiffStatus = "added" | "changed" | null;

function diffStatus(diff: DiffOverlay | null, id: string): DiffStatus {
  if (!diff) return null;
  if (diff.added.includes(id)) return "added";
  if (diff.changed.includes(id)) return "changed";
  return null;
}

const DIFF_SHADOW: Record<Exclude<DiffStatus, null>, string> = {
  added: "0 0 0 3px {colors.green.400}, 0 0 14px {colors.green.400}",
  changed: "0 0 0 3px {colors.yellow.400}",
};

/** Run-state chip (state + try number) shown on a node's top edge. */
export function RunChip({ state, tryNumber, mapped }: { state: string | null; tryNumber?: number; mapped?: number }) {
  const { t } = useTranslation("canvas");
  const style = runStateStyle(state);
  const label = state ? t(`state.${state}`, { defaultValue: state.replace(/_/g, " ") }) : t("state.none");
  return (
    <Badge
      position="absolute"
      top="-11px"
      right="8px"
      size="xs"
      variant="solid"
      colorPalette={style.palette}
      shadow="xs"
      aria-label={t("run.chipLabel", { state: label, n: tryNumber ?? 0 })}
    >
      {label}
      {tryNumber && tryNumber > 1 ? ` · #${tryNumber}` : null}
      {mapped ? ` · [${mapped}]` : null}
    </Badge>
  );
}

function SpecBadges({ spec }: { spec: TaskSpec }) {
  const { t } = useTranslation("canvas");
  return (
    <>
      {spec.disabled ? (
        <Badge colorPalette="gray" size="xs" variant="solid">
          <LuEyeOff /> {t("badge.disabled")}
        </Badge>
      ) : null}
      {spec.is_setup ? (
        <Badge colorPalette="teal" size="xs">
          <LuArrowUpFromLine /> {t("badge.setup")}
        </Badge>
      ) : null}
      {spec.is_teardown ? (
        <Badge colorPalette="teal" size="xs">
          <LuArrowDownToLine /> {t("badge.teardown")}
        </Badge>
      ) : null}
      {spec.mapping ? (
        <Badge colorPalette="purple" size="xs">
          <LuLayers /> {t("badge.mapped")}
        </Badge>
      ) : null}
      {spec.trigger_rule && spec.trigger_rule !== "all_success" ? (
        <Badge size="xs" variant="outline">
          {spec.trigger_rule}
        </Badge>
      ) : null}
      {spec.outlets?.length ? (
        <Badge colorPalette="teal" size="xs" variant="outline">
          {t("badge.asset")}
        </Badge>
      ) : null}
      {spec.kind === "python_task" && spec.is_result ? (
        <Badge colorPalette="green" size="xs">
          <LuFlag /> {t("badge.result")}
        </Badge>
      ) : null}
      {spec.kind === "python_task" && spec.decorator === "stub" ? (
        <Badge colorPalette="orange" size="xs" variant="outline">
          {t("badge.stub", { queue: String(spec.decorator_kwargs?.queue ?? "?") })}
        </Badge>
      ) : null}
      {spec.lineage && spec.lineage !== "inherit" ? (
        <Badge size="xs" variant="outline" colorPalette={spec.lineage === "enabled" ? "blue" : "gray"}>
          {t(`badge.lineage_${spec.lineage}`)}
        </Badge>
      ) : null}
    </>
  );
}

/** Shared visual shell for every task-like node. */
export const TaskCard = memo(function TaskCard({ id, selected, title, subtitle, icon, accent, spec, markers }: Props) {
  const { t } = useTranslation("canvas");
  const problems = useBuilder(useShallow((s) => s.problems.filter((p) => p.node_id === id)));
  const run = useBuilder((s) => s.runOverlay?.states[id]);
  const hasRun = useBuilder((s) => s.runOverlay !== null);
  const diff = useBuilder((s) => diffStatus(s.diffOverlay, id));
  const errors = problems.filter((p) => p.severity === "error").length;
  const warnings = problems.length - errors;
  const runStyle = hasRun ? runStateStyle(run?.state) : null;
  const border = errors
    ? "red.500"
    : warnings
      ? "orange.400"
      : selected
        ? "blue.500"
        : runStyle
          ? runStyle.color
          : "border.emphasized";

  return (
    <Box
      position="relative"
      bg={runStyle?.bg ?? "bg.panel"}
      // Emphasis (selection, problems, run state) is an outline: it never changes the node's
      // size, so laid-out neighbours don't end up overlapping when a problem appears.
      borderWidth="1px"
      borderColor={border}
      outline={selected || errors || runStyle ? "1px solid" : undefined}
      outlineColor={border}
      borderLeftWidth="5px"
      borderLeftColor={spec.disabled ? "gray.400" : accent}
      borderStyle={spec.is_setup || spec.is_teardown ? "dashed" : "solid"}
      rounded="md"
      shadow={diff ? DIFF_SHADOW[diff] : selected ? "md" : "xs"}
      opacity={spec.disabled ? 0.55 : 1}
      px={3}
      py={2}
      minW="200px"
      maxW="260px"
      title={problems.map((p) => problemText(p)).join("\n") || undefined}
    >
      {runStyle?.pulse ? (
        <Box
          position="absolute"
          inset="-5px"
          rounded="lg"
          borderWidth="2px"
          borderColor={runStyle.color}
          animation="pulse"
          pointerEvents="none"
          aria-hidden
        />
      ) : null}
      {hasRun ? <RunChip state={run?.state ?? null} tryNumber={run?.tryNumber} mapped={run?.mapped} /> : null}
      {selected ? <TaskToolbar id={id} spec={spec} /> : null}
      <Handle type="target" position={Position.Left} />
      <HStack gap={2}>
        <Box color={spec.disabled ? "fg.muted" : accent} flexShrink={0} aria-hidden>
          {icon}
        </Box>
        <Text
          fontWeight="semibold"
          fontSize="sm"
          truncate
          flex="1"
          textDecoration={spec.disabled ? "line-through" : undefined}
        >
          {title}
        </Text>
        {markers ? (
          <HStack gap={1} color="fg.muted" flexShrink={0}>
            {markers}
          </HStack>
        ) : null}
      </HStack>
      {subtitle ? (
        <Text fontSize="xs" color="fg.muted" truncate>
          {subtitle}
        </Text>
      ) : null}
      <HStack gap={1} mt={1} wrap="wrap" _empty={{ display: "none" }}>
        <SpecBadges spec={spec} />
      </HStack>
      {errors || warnings || diff ? (
        // Corner badges float over the border instead of adding a row.
        <HStack gap={1} position="absolute" top="-9px" right="8px" pointerEvents="none" zIndex={1}>
          {errors ? (
            <Badge colorPalette="red" size="xs" variant="solid">
              {t("badge.errors", { count: errors })}
            </Badge>
          ) : null}
          {warnings ? (
            <Badge colorPalette="orange" size="xs" variant="solid">
              {t("badge.warnings", { count: warnings })}
            </Badge>
          ) : null}
          {diff ? (
            <Badge colorPalette={diff === "added" ? "green" : "yellow"} size="xs" variant="solid">
              {t(`diff.${diff}`)}
            </Badge>
          ) : null}
        </HStack>
      ) : null}
      <Handle type="source" position={Position.Right} />
    </Box>
  );
});
