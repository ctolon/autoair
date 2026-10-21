import { Box, HStack, IconButton, Input } from "@chakra-ui/react";
import { BaseEdge, EdgeLabelRenderer, type EdgeProps, getSmoothStepPath } from "@xyflow/react";
import { memo, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { LuPlus } from "react-icons/lu";

import type { BuilderEdge } from "../../lib/types";
import { useBuilder } from "../../store/builder";
import { useLowDetail } from "../lowDetail";
import { useCanvasUi } from "../uiStore";

/** Display-only data the canvas attaches to edges. */
type EdgeViewData = { setupTeardown?: boolean };

function LabelEditor({ id, initial }: { id: string; initial: string }) {
  const { t } = useTranslation("canvas");
  const [value, setValue] = useState(initial);
  const ref = useRef<HTMLInputElement>(null);
  const done = useRef(false);
  useEffect(() => {
    ref.current?.focus();
    ref.current?.select();
  }, []);
  const finish = (commit: boolean) => {
    if (done.current) return;
    done.current = true;
    if (commit && value !== initial) useBuilder.getState().setEdgeLabel(id, value.trim());
    useCanvasUi.getState().setEditingEdge(null);
  };
  return (
    <Input
      ref={ref}
      size="2xs"
      w="140px"
      bg="bg.panel"
      className="nodrag nopan"
      aria-label={t("edge.labelInput")}
      value={value}
      onChange={(e) => setValue(e.target.value)}
      onBlur={() => finish(true)}
      onKeyDown={(e) => {
        e.stopPropagation();
        if (e.key === "Enter") finish(true);
        else if (e.key === "Escape") finish(false);
      }}
    />
  );
}

/**
 * Default edge: smooth-step path, dashed for setup/teardown relations, an editable label
 * (double-click) and a "+" button at the midpoint that inserts a node between both ends.
 */
export const InsertableEdge = memo(function InsertableEdge(props: EdgeProps<BuilderEdge>) {
  const { id, sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition, selected, label, markerEnd } = props;
  const { t } = useTranslation("canvas");
  const [hover, setHover] = useState(false);
  const editing = useCanvasUi((s) => s.editingEdgeId === id);
  const [path, labelX, labelY] = getSmoothStepPath({
    sourceX,
    sourceY,
    targetX,
    targetY,
    sourcePosition,
    targetPosition,
    borderRadius: 8,
  });
  const data = props.data as EdgeViewData | undefined;
  const text = typeof label === "string" ? label : "";
  const showPlus = (hover || selected) && !editing;
  const lowDetail = useLowDetail();
  const style = {
    ...props.style,
    ...(data?.setupTeardown ? { strokeDasharray: "6 4" } : {}),
    ...(selected ? { stroke: "var(--chakra-colors-blue-500)", strokeWidth: 2 } : {}),
  };

  // Zoomed far out on a large graph: the line only (no label, no insert button, no portal).
  if (lowDetail && !editing) return <BaseEdge id={id} path={path} markerEnd={markerEnd} style={style} />;

  return (
    <g onMouseEnter={() => setHover(true)} onMouseLeave={() => setHover(false)}>
      <BaseEdge id={id} path={path} markerEnd={markerEnd} interactionWidth={24} style={style} />
      <EdgeLabelRenderer>
        <Box
          position="absolute"
          transform={`translate(-50%, -50%) translate(${labelX}px, ${labelY}px)`}
          pointerEvents="all"
          className="nodrag nopan"
          onMouseEnter={() => setHover(true)}
          onMouseLeave={() => setHover(false)}
        >
          <HStack gap={1}>
            {editing ? (
              <LabelEditor id={id} initial={text} />
            ) : text ? (
              <Box
                as="span"
                fontSize="xs"
                px={1.5}
                py={0.5}
                rounded="sm"
                bg="bg.panel"
                borderWidth="1px"
                borderColor={selected ? "blue.500" : "border.muted"}
                cursor="text"
                title={t("edge.editHint")}
                onDoubleClick={(e) => {
                  e.stopPropagation();
                  useCanvasUi.getState().setEditingEdge(id);
                }}
              >
                {text}
              </Box>
            ) : null}
            <IconButton
              size="2xs"
              variant="solid"
              colorPalette="blue"
              rounded="full"
              minW="18px"
              h="18px"
              opacity={showPlus ? 1 : 0}
              _focusVisible={{ opacity: 1 }}
              transition="opacity 0.12s"
              aria-label={t("edge.insert")}
              title={t("edge.insert")}
              onClick={(e) => {
                e.stopPropagation();
                const r = e.currentTarget.getBoundingClientRect();
                useCanvasUi.getState().openInsert({
                  screen: { x: r.left + r.width / 2, y: r.bottom + 4 },
                  flow: { x: labelX, y: labelY },
                  mode: { kind: "edge", edgeId: id },
                });
              }}
            >
              <LuPlus />
            </IconButton>
          </HStack>
        </Box>
      </EdgeLabelRenderer>
    </g>
  );
});
