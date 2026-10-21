import { Badge, Box, HStack, IconButton, Text } from "@chakra-ui/react";
import { Handle, NodeResizer, type NodeProps, type NodeTypes, Position } from "@xyflow/react";
import { type ReactNode, memo } from "react";
import { useTranslation } from "react-i18next";
import {
  LuBlocks,
  LuBox,
  LuChevronDown,
  LuChevronRight,
  LuCode,
  LuGitBranch,
  LuHourglass,
  LuPuzzle,
  LuRadar,
  LuRepeat,
  LuTerminal,
  LuUserCheck,
} from "react-icons/lu";
import { useShallow } from "zustand/react/shallow";

import { nodeKindExtension } from "../../extensions/registry";
import type { CustomFlowNode, OperatorFlowNode, PythonTaskFlowNode, TaskGroupFlowNode } from "../../lib/types";
import { useBuilder } from "../../store/builder";
import { descendantsOf } from "../geometry";
import { isHitlClass, useOperatorSummary } from "../insertables";
import { aggregateState } from "../runState";
import { NoteNodeView } from "./NoteNode";
import { IconImage, decoratorIcon } from "../icons";
import { RunChip, TaskCard } from "./TaskCard";

function Marker({ label, children }: { label: string; children: ReactNode }) {
  return (
    <Box as="span" display="inline-flex" title={label} aria-label={label} role="img">
      {children}
    </Box>
  );
}

const OperatorNodeView = memo(function OperatorNodeView({ id, data, selected }: NodeProps<OperatorFlowNode>) {
  const { t } = useTranslation("canvas");
  const spec = data.spec;
  const cls = spec.operator.split(".").pop() ?? spec.operator;
  const summary = useOperatorSummary(spec.operator);
  const isSensor = summary?.category === "sensor" || /Sensor$/.test(cls);
  const isHitl = isHitlClass(cls);
  const deferrable = spec.params?.deferrable === true;
  return (
    <TaskCard
      id={id}
      selected={selected}
      spec={spec}
      title={spec.label || spec.task_id}
      subtitle={spec.label ? `${spec.task_id} · ${cls}` : cls}
      icon={<IconImage id={spec.icon ?? summary?.icon} size={18} fallback={<LuBox size={16} />} />}
      accent={
        summary?.ui_color && /^#[0-9a-f]{3,8}$/i.test(summary.ui_color)
          ? summary.ui_color
          : isSensor
            ? "#e5a00d"
            : "#017cee"
      }
      markers={
        isSensor || isHitl || deferrable ? (
          <>
            {isSensor ? (
              <Marker label={t("marker.sensor")}>
                <LuRadar size={13} />
              </Marker>
            ) : null}
            {deferrable ? (
              <Marker label={t("marker.deferrable")}>
                <LuHourglass size={13} />
              </Marker>
            ) : null}
            {isHitl ? (
              <Marker label={t("marker.hitl")}>
                <LuUserCheck size={13} />
              </Marker>
            ) : null}
          </>
        ) : undefined
      }
    />
  );
});

const BRANCHING = new Set(["branch", "short_circuit", "branch_virtualenv", "branch_external_python", "llm_branch"]);

const PythonTaskNodeView = memo(function PythonTaskNodeView({ id, data, selected }: NodeProps<PythonTaskFlowNode>) {
  const { t } = useTranslation("canvas");
  const spec = data.spec;
  const decorator = spec.decorator ?? "task";
  const deco = decorator === "task" ? "@task" : `@task.${decorator}`;
  return (
    <TaskCard
      id={id}
      selected={selected}
      spec={spec}
      title={spec.label || spec.task_id}
      subtitle={`${deco} ${spec.function_name}()`}
      icon={
        <IconImage
          id={spec.icon ?? decoratorIcon(decorator)}
          size={18}
          fallback={decorator === "bash" ? <LuTerminal size={16} /> : <LuCode size={16} />}
        />
      }
      accent="#3b8c3b"
      markers={
        BRANCHING.has(decorator) ? (
          <Marker label={t(decorator === "short_circuit" ? "marker.shortCircuit" : "marker.branch")}>
            <LuGitBranch size={13} />
          </Marker>
        ) : decorator === "sensor" ? (
          <Marker label={t("marker.sensor")}>
            <LuRadar size={13} />
          </Marker>
        ) : undefined
      }
    />
  );
});

const CustomNodeView = memo(function CustomNodeView({ id, data, selected }: NodeProps<CustomFlowNode>) {
  const ext = nodeKindExtension(data.spec.handler);
  const Icon = ext?.icon ?? LuPuzzle;
  return (
    <TaskCard
      id={id}
      selected={selected}
      spec={data.spec}
      title={data.spec.label || data.spec.task_id}
      subtitle={ext?.describe?.(data.spec) ?? data.spec.handler}
      icon={<IconImage id={data.spec.icon} size={18} fallback={<Icon size={16} />} />}
      accent={ext?.color ?? "#8b5cf6"}
    />
  );
});

const NO_SUMMARY: [number, string | null] = [0, null];

const TaskGroupNodeView = memo(function TaskGroupNodeView({ id, data, selected }: NodeProps<TaskGroupFlowNode>) {
  const { t } = useTranslation("canvas");
  const collapsed = !!data.collapsed;
  const errors = useBuilder((s) => s.problems.some((p) => p.node_id === id && p.severity === "error"));
  const hasRun = useBuilder((s) => s.runOverlay !== null);
  // Member count + aggregated run state are only needed (and computed) while collapsed.
  const [members, state] = useBuilder(
    useShallow((s): [number, string | null] => {
      if (!collapsed) return NO_SUMMARY;
      const inside = descendantsOf(s.nodes, [id]);
      const tasks = s.nodes.filter((n) => inside.has(n.id) && n.type !== "task_group" && n.type !== "note");
      const states = s.runOverlay ? tasks.map((n) => s.runOverlay?.states[n.id]?.state) : [];
      return [tasks.length, aggregateState(states)];
    }),
  );
  const name = data.spec.label || data.spec.group_id;
  const toggleLabel = collapsed ? t("group.expand", { name }) : t("group.collapse", { name });

  return (
    <Box
      position="relative"
      w="100%"
      h="100%"
      rounded="lg"
      borderWidth="2px"
      borderStyle={collapsed ? "solid" : "dashed"}
      borderColor={errors ? "red.500" : selected ? "blue.500" : "border.emphasized"}
      bg={collapsed ? "bg.panel" : "color-mix(in srgb, var(--chakra-colors-bg-muted) 55%, transparent)"}
      shadow={collapsed ? "sm" : undefined}
    >
      <NodeResizer isVisible={selected && !collapsed} minWidth={240} minHeight={140} />
      {collapsed && hasRun ? <RunChip state={state} /> : null}
      <Handle type="target" position={Position.Left} />
      <HStack px={2} py={1} gap={1.5} borderBottomWidth={collapsed ? 0 : "1px"} borderColor="border.muted">
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
        <LuBlocks size={14} aria-hidden />
        <Text fontSize="sm" fontWeight="semibold" truncate flex="1">
          {name}
        </Text>
        {data.spec.loop ? (
          <Badge size="xs" colorPalette="purple" title={t("group.loopTitle")}>
            <LuRepeat /> ×{data.spec.loop.max_iterations}
          </Badge>
        ) : null}
        {collapsed ? (
          <Badge size="xs" variant="subtle" colorPalette="blue">
            {t("group.members", { count: members })}
          </Badge>
        ) : (
          <Badge size="xs" variant="subtle">
            {t("group.badge")}
          </Badge>
        )}
      </HStack>
      <Handle type="source" position={Position.Right} />
    </Box>
  );
});

export const nodeTypes = {
  operator: OperatorNodeView,
  python_task: PythonTaskNodeView,
  task_group: TaskGroupNodeView,
  custom: CustomNodeView,
  note: NoteNodeView,
} satisfies NodeTypes;
