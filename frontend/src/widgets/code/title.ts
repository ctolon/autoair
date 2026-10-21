import { useContext } from "react";

import { nodeKey } from "../../lib/types";
import { useBuilder } from "../../store/builder";
import { EditingNodeContext } from "../context";

/** "<field> — <task id>" unless the title already names the task. */
export function withTaskId(base: string, taskId: string | null): string {
  return taskId && !base.includes(taskId) ? `${base} — ${taskId}` : base;
}

/** Title of a full-screen editor: names the task whose inspector it was opened from. */
export function useEditorTitle(base: string): string {
  const nodeId = useContext(EditingNodeContext);
  const taskId = useBuilder((s) => {
    if (!nodeId) return null;
    const node = s.nodes.find((n) => n.id === nodeId);
    return node ? nodeKey(node) : null;
  });
  return withTaskId(base, taskId);
}
