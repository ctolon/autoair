import type { DagSpec, Edge as SpecEdge } from "../api/gen/types.gen";
import type { BuilderEdge, BuilderNode, DagSettings, NodeSpec, SpecNode } from "../lib/types";

export const GROUP_DEFAULT_SIZE = { width: 360, height: 220 } as const;
export const NOTE_DEFAULT_SIZE = { width: 240, height: 140 } as const;

const edgeId = (source: string, target: string) => `e_${source}__${target}`;

/** Spec nodes/edges → React Flow nodes/edges. Parents are emitted before children (React Flow requirement). */
export function graphFromSpec(
  specNodes: readonly SpecNode[],
  specEdges: readonly SpecEdge[] = [],
): { nodes: BuilderNode[]; edges: BuilderEdge[] } {
  const byId = new Map(specNodes.map((n) => [n.id, n]));
  const depth = (id: string, seen = new Set<string>()): number => {
    const parent = byId.get(id)?.parent_group;
    if (!parent || seen.has(parent)) return 0;
    seen.add(parent);
    return 1 + depth(parent, seen);
  };
  const ordered = [...specNodes].sort((a, b) => {
    const ga = a.kind === "task_group" ? 0 : 1;
    const gb = b.kind === "task_group" ? 0 : 1;
    return ga - gb || depth(a.id) - depth(b.id);
  });

  const nodes = ordered.map((n): BuilderNode => {
    const { id, position, parent_group, ...rest } = n;
    const base = {
      id,
      position: { x: position?.x ?? 0, y: position?.y ?? 0 },
      ...(parent_group ? { parentId: parent_group } : {}),
    };
    switch (rest.kind) {
      case "task_group":
        return {
          ...base,
          type: "task_group",
          data: { spec: rest },
          style: {
            width: position?.width ?? GROUP_DEFAULT_SIZE.width,
            height: position?.height ?? GROUP_DEFAULT_SIZE.height,
          },
        };
      case "operator":
        return { ...base, type: "operator", data: { spec: rest } };
      case "python_task":
        return { ...base, type: "python_task", data: { spec: rest } };
      case "custom":
        return { ...base, type: "custom", data: { spec: rest } };
      case "blueprint":
        return { ...base, type: "blueprint", data: { spec: rest } };
      case "note":
        return {
          ...base,
          type: "note",
          data: { spec: rest },
          style: {
            width: position?.width ?? NOTE_DEFAULT_SIZE.width,
            height: position?.height ?? NOTE_DEFAULT_SIZE.height,
          },
        };
    }
  });

  const edges = specEdges.map((e): BuilderEdge => ({
    id: e.id ?? edgeId(e.source, e.target),
    source: e.source,
    target: e.target,
    ...(e.label ? { label: e.label } : {}),
  }));
  return { nodes, edges };
}

/** Spec → React Flow graph. */
export function fromSpec(spec: DagSpec): { dag: DagSettings; nodes: BuilderNode[]; edges: BuilderEdge[] } {
  const { nodes: specNodes = [], edges: specEdges = [], ...dag } = spec;
  return { dag, ...graphFromSpec(specNodes, specEdges) };
}

/** React Flow nodes → spec nodes. Only `data.spec` is serialised: UI state (e.g. `collapsed`) never leaks. */
export function specNodesOf(nodes: readonly BuilderNode[]): SpecNode[] {
  return nodes.map((n) => {
    const def = n.type === "note" ? NOTE_DEFAULT_SIZE : GROUP_DEFAULT_SIZE;
    const size =
      n.type === "task_group" || n.type === "note"
        ? {
            width: Number(n.style?.width ?? n.measured?.width ?? def.width),
            height: Number(n.style?.height ?? n.measured?.height ?? def.height),
          }
        : {};
    const spec: NodeSpec = n.data.spec;
    return {
      ...spec,
      id: n.id,
      parent_group: n.parentId ?? null,
      position: { x: Math.round(n.position.x), y: Math.round(n.position.y), ...size },
    } as SpecNode;
  });
}

export function specEdgesOf(edges: readonly BuilderEdge[]): SpecEdge[] {
  return edges.map((e) => ({
    id: e.id,
    source: e.source,
    target: e.target,
    label: typeof e.label === "string" && e.label ? e.label : null,
  }));
}

/** React Flow graph → spec (the payload sent to the backend). */
export function toSpec(dag: DagSettings, nodes: BuilderNode[], edges: BuilderEdge[]): DagSpec {
  return { ...dag, nodes: specNodesOf(nodes), edges: specEdgesOf(edges) };
}

export { edgeId };
