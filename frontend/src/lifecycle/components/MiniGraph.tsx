import { Box, Stack, Text } from "@chakra-ui/react";
import { Background, ReactFlow, ReactFlowProvider } from "@xyflow/react";
import { useMemo } from "react";

import type { DagSpec } from "../../api/gen/types.gen";
import { nodeTypes } from "../../canvas/nodes";
import { nodeKey } from "../../lib/types";
import { useColorMode } from "../../shell/colorMode";
import { fromSpec } from "../../store/convert";

/**
 * React Flow's own colours mapped to Chakra semantic tokens (light and dark). React Flow also
 * gets the app's colour mode: by default it adds a `light` class, which switches every Chakra
 * token inside it to its light value (white cards with light text in dark mode).
 */
const FLOW_TOKENS = {
  "--xy-background-color": "var(--chakra-colors-bg-subtle)",
  "--xy-background-pattern-color": "var(--chakra-colors-border-emphasized)",
  "--xy-edge-stroke": "var(--chakra-colors-fg-subtle)",
  "--xy-handle-background-color": "var(--chakra-colors-fg-muted)",
  "--xy-handle-border-color": "var(--chakra-colors-bg-panel)",
} as const;

/** Read-only, non-interactive React Flow render of a spec (template previews). */
export function MiniGraph({ spec, label, height = "260px" }: { spec: DagSpec; label: string; height?: string }) {
  const colorMode = useColorMode((s) => s.mode);
  const graph = useMemo(() => {
    try {
      return fromSpec(spec);
    } catch {
      return null;
    }
  }, [spec]);
  if (!graph) return null;
  if (!graph.nodes.length) {
    return (
      <Text fontSize="sm" color="fg.muted">
        —
      </Text>
    );
  }
  return (
    <Box
      h={height}
      borderWidth="1px"
      borderColor="border"
      rounded="md"
      role="img"
      aria-label={label}
      overflow="hidden"
      bg="bg.subtle"
      color="fg"
      css={FLOW_TOKENS}
      data-color-mode={colorMode}
    >
      <ReactFlowProvider>
        <ReactFlow
          nodes={graph.nodes.map((n) => ({ ...n, selected: false, draggable: false }))}
          edges={graph.edges}
          nodeTypes={nodeTypes}
          colorMode={colorMode}
          fitView
          fitViewOptions={{ padding: 0.15, maxZoom: 1 }}
          nodesDraggable={false}
          nodesConnectable={false}
          elementsSelectable={false}
          zoomOnScroll={false}
          panOnDrag
          proOptions={{ hideAttribution: true }}
          minZoom={0.1}
        >
          <Background gap={16} size={1} />
        </ReactFlow>
      </ReactFlowProvider>
      <Stack srOnly as="ul">
        {graph.nodes.map((n) => (
          <li key={n.id}>{nodeKey(n)}</li>
        ))}
      </Stack>
    </Box>
  );
}
