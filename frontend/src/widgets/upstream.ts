import { useMemo } from "react";
import { useShallow } from "zustand/react/shallow";

import { upstreamTasks } from "../canvas/graph";
import type { BuilderNode } from "../lib/types";
import { nodeKey } from "../lib/types";
import { detectReturnKeys, effectiveTaskId } from "../lib/xcom";
import { useBuilder } from "../store/builder";

export type UpstreamOutput = {
  node: BuilderNode;
  /** Effective Airflow task id (with group prefixes). */
  taskId: string;
  label: string;
  /** Known XCom keys (multiple_outputs dict keys detected from `return {...}`). */
  keys: string[];
  multipleOutputs: boolean;
};

/** Output keys a task is known to push besides return_value. */
function outputKeys(node: BuilderNode): { keys: string[]; multipleOutputs: boolean } {
  if (node.type !== "python_task") return { keys: [], multipleOutputs: false };
  const multiple = !!node.data.spec.multiple_outputs;
  return { keys: multiple ? detectReturnKeys(node.data.spec.code) : [], multipleOutputs: multiple };
}

/** Upstream tasks of `nodeId` (through task groups) with their outputs. */
export function useUpstreamOutputs(nodeId: string | null): UpstreamOutput[] {
  // Only re-render when the graph shape or relevant specs change.
  const { nodes, edges } = useBuilder(useShallow((s) => ({ nodes: s.nodes, edges: s.edges })));
  return useMemo(() => {
    if (!nodeId) return [];
    return upstreamTasks(nodes, edges, nodeId).map((n) => ({
      node: n,
      taskId: effectiveTaskId(nodes, n.id),
      label: nodeKey(n),
      ...outputKeys(n),
    }));
  }, [nodes, edges, nodeId]);
}
