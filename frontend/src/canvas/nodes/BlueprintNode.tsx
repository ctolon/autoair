import { Badge, Box, HStack, IconButton, Stack, Text } from "@chakra-ui/react";
import { Handle, type NodeProps, Position } from "@xyflow/react";
import { memo } from "react";
import { useTranslation } from "react-i18next";
import { LuChevronDown, LuChevronRight, LuPackage } from "react-icons/lu";

import { problemText } from "../../lib/problemMessages";
import type { BlueprintFlowNode } from "../../lib/types";
import { useBuilder } from "../../store/builder";
import { previewTasks, useBlueprintDetail, useCachedPreview } from "../blueprints";
import { IconImage } from "../icons";
import { RunChip } from "./TaskCard";

/** Preview rows drawn inside the node; the rest is summarised as "+N more". */
const MAX_PREVIEW_ROWS = 8;

const countProblems = (problems: { node_id?: string | null; severity: string }[], id: string) => {
  let errors = 0;
  let warnings = 0;
  for (const p of problems) {
    if (p.node_id !== id) continue;
    if (p.severity === "error") errors++;
    else if (p.severity === "warning") warnings++;
  }
  return errors * 10_000 + warnings; // one primitive keeps the selector stable
};

/**
 * An Astronomer Blueprint step: one node standing for the task or task group the blueprint
 * renders. Expanded, it lists the tasks of the latest preview (fetched by the inspector for the
 * current config, or the catalog's default preview while the config is empty).
 */
export const BlueprintNodeView = memo(function BlueprintNodeView({ id, data, selected }: NodeProps<BlueprintFlowNode>) {
  const { t } = useTranslation("canvas");
  const spec = data.spec;
  const collapsed = !!data.collapsed;
  const counts = useBuilder((s) => countProblems(s.problems, id));
  const firstProblem = useBuilder((s) => s.problems.find((p) => p.node_id === id && p.severity !== "info"));
  const run = useBuilder((s) => s.runOverlay?.states[id]);
  const hasRun = useBuilder((s) => s.runOverlay !== null);
  const version = typeof spec.version === "number" ? spec.version : 1;
  const name = typeof spec.blueprint === "string" ? spec.blueprint : "";
  const detail = useBlueprintDetail(name, version);
  const cached = useCachedPreview(spec);
  const preview = previewTasks(spec, cached.data, detail.data);
  const errors = Math.floor(counts / 10_000);
  const warnings = counts % 10_000;
  const str = (v: unknown) => (typeof v === "string" ? v : "");
  const stepId = str(spec.step_id);
  const title = str(spec.label) || stepId || name;
  const toggleLabel = collapsed ? t("blueprint.expand", { name: title }) : t("blueprint.collapse", { name: title });
  const tasks = preview?.tasks ?? [];
  const border = errors ? "red.500" : warnings ? "orange.400" : selected ? "blue.500" : "border.emphasized";

  return (
    <Box
      position="relative"
      bg="bg.panel"
      borderWidth="1px"
      borderColor={border}
      outline={selected || errors ? "1px solid" : undefined}
      outlineColor={border}
      borderLeftWidth="5px"
      borderLeftColor={spec.disabled ? "border.emphasized" : "teal.solid"}
      rounded="lg"
      shadow={selected ? "md" : "xs"}
      opacity={spec.disabled ? 0.55 : 1}
      minW="220px"
      maxW="280px"
      title={firstProblem ? problemText(firstProblem) : undefined}
      data-testid="blueprint-node"
    >
      {hasRun ? <RunChip state={run?.state ?? null} tryNumber={run?.tryNumber} /> : null}
      <Handle type="target" position={Position.Left} />
      <HStack px={2} pt={1.5} pb={collapsed ? 1.5 : 1} gap={1.5}>
        <IconButton
          className="nodrag"
          size="2xs"
          variant="ghost"
          aria-expanded={!collapsed}
          aria-label={toggleLabel}
          title={toggleLabel}
          onClick={(e) => {
            e.stopPropagation();
            useBuilder.getState().setCollapsed(id, !collapsed);
          }}
        >
          {collapsed ? <LuChevronRight /> : <LuChevronDown />}
        </IconButton>
        <Box color="teal.fg" flexShrink={0} aria-hidden>
          <IconImage id={spec.icon} size={16} fallback={<LuPackage size={15} />} />
        </Box>
        <Stack gap={0} minW={0} flex="1">
          <Text
            fontSize="sm"
            fontWeight="semibold"
            truncate
            textDecoration={spec.disabled ? "line-through" : undefined}
          >
            {title}
          </Text>
          <Text fontSize="xs" color="fg.muted" fontFamily="mono" truncate>
            {t("blueprint.subtitle", { name, version })}
          </Text>
        </Stack>
        <Badge size="xs" colorPalette="teal" variant="subtle" flexShrink={0}>
          {collapsed && tasks.length ? t("blueprint.tasks", { count: tasks.length }) : t("blueprint.badge")}
        </Badge>
      </HStack>
      {collapsed ? null : (
        <Box borderTopWidth="1px" borderColor="border.muted" px={2.5} py={1.5}>
          {preview?.error ? (
            <Text fontSize="xs" color="fg.warning" lineClamp={2}>
              {t("blueprint.previewError", { error: preview.error })}
            </Text>
          ) : tasks.length ? (
            <Stack gap={0} aria-label={t("blueprint.previewLabel")}>
              {tasks.slice(0, MAX_PREVIEW_ROWS).map((task, i) => (
                <Text key={`${i}:${task}`} fontSize="xs" fontFamily="mono" color="fg.muted" truncate>
                  {preview?.kind === "task" ? task : `${stepId}.${task}`}
                </Text>
              ))}
              {tasks.length > MAX_PREVIEW_ROWS ? (
                <Text fontSize="xs" color="fg.subtle">
                  {t("blueprint.more", { count: tasks.length - MAX_PREVIEW_ROWS })}
                </Text>
              ) : null}
            </Stack>
          ) : (
            <Text fontSize="xs" color="fg.subtle">
              {t("blueprint.noPreview")}
            </Text>
          )}
        </Box>
      )}
      {errors || warnings ? (
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
        </HStack>
      ) : null}
      <Handle type="source" position={Position.Right} />
    </Box>
  );
});
