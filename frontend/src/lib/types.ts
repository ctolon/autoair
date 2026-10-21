import type { Edge, Node } from "@xyflow/react";

import type {
  CustomNode,
  DagSpec,
  Edge as SpecEdge,
  NoteNode,
  OperatorNode,
  PythonTaskNode,
  TaskGroupNode,
} from "../api/gen/types.gen";

/** Spec fields that React Flow owns (geometry, hierarchy, identity). */
type FlowOwned = "id" | "position" | "parent_group";

export type OperatorSpec = Omit<OperatorNode, FlowOwned>;
export type PythonTaskSpec = Omit<PythonTaskNode, FlowOwned>;
export type TaskGroupSpec = Omit<TaskGroupNode, FlowOwned>;
export type CustomSpec = Omit<CustomNode, FlowOwned>;
export type NoteSpec = Omit<NoteNode, FlowOwned>;
export type NodeSpec = OperatorSpec | PythonTaskSpec | TaskGroupSpec | CustomSpec | NoteSpec;
export type TaskSpec = OperatorSpec | PythonTaskSpec | CustomSpec;
export type NodeKind = NodeSpec["kind"];

/** Each React Flow node type maps 1:1 onto a spec `kind` (a discriminated union). */
export type OperatorFlowNode = Node<{ spec: OperatorSpec }, "operator">;
export type PythonTaskFlowNode = Node<{ spec: PythonTaskSpec }, "python_task">;
/** `collapsed` is UI-only state: it is never written into the spec (see `toSpec`). */
export type TaskGroupFlowNode = Node<{ spec: TaskGroupSpec; collapsed?: boolean }, "task_group">;
export type CustomFlowNode = Node<{ spec: CustomSpec }, "custom">;
export type NoteFlowNode = Node<{ spec: NoteSpec }, "note">;
export type BuilderNode = OperatorFlowNode | PythonTaskFlowNode | TaskGroupFlowNode | CustomFlowNode | NoteFlowNode;
export type TaskFlowNode = OperatorFlowNode | PythonTaskFlowNode | CustomFlowNode;

export type BuilderEdge = Edge;

export type DagSettings = Omit<DagSpec, "nodes" | "edges">;

export type SpecFor<K extends NodeKind> = Extract<NodeSpec, { kind: K }>;

export const isTaskNode = (n: BuilderNode): n is TaskFlowNode =>
  n.type === "operator" || n.type === "python_task" || n.type === "custom";

/** Display id of a node (task_id or group_id). */
export const nodeKey = (n: BuilderNode): string => {
  const spec = n.data.spec;
  if (spec.kind === "task_group") return spec.group_id;
  if (spec.kind === "note") return spec.label || "note";
  return spec.task_id;
};

/** Tagged parameter values understood by the backend (see autoair.spec.models). */
export type XComRef = { $type: "xcom"; node: string; key?: string | null };
export const isXComRef = (v: unknown): v is XComRef =>
  typeof v === "object" && v !== null && (v as { $type?: unknown }).$type === "xcom";

/** A node as stored in the spec (React Flow geometry folded back in). */
export type SpecNode = NonNullable<DagSpec["nodes"]>[number];
export type { SpecEdge };

/** A subset of a DAG: what the clipboard, template fragments and `insertFragment` exchange. */
export type FragmentInput = { nodes: SpecNode[]; edges?: SpecEdge[] | null };
/** Clipboard payload written by copy/cut (`{autoair: "fragment", nodes, edges}`). */
export type ClipboardFragment = { autoair: "fragment"; nodes: SpecNode[]; edges: SpecEdge[] };

/** `@task.<decorator>` variants the spec (and code generator) supports. */
export type PythonDecorator = NonNullable<PythonTaskSpec["decorator"]>;
export const PYTHON_DECORATORS = [
  "task",
  "branch",
  "short_circuit",
  "virtualenv",
  "external_python",
  "branch_virtualenv",
  "branch_external_python",
  "sensor",
  "bash",
  "docker",
  "kubernetes",
  "stub",
  "llm",
  "agent",
  "llm_branch",
  "llm_sql",
] as const satisfies readonly PythonDecorator[];
// Compile-time guard: fails if the spec gains a decorator missing from the list above.
type _MissingDecorators = Exclude<PythonDecorator, (typeof PYTHON_DECORATORS)[number]>;
export const _decoratorsExhaustive: [_MissingDecorators] extends [never] ? true : never = true;
export const isPythonDecorator = (v: string): v is PythonDecorator =>
  (PYTHON_DECORATORS as readonly string[]).includes(v);
