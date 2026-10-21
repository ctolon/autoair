import { Box } from "@chakra-ui/react";
import {
  Background,
  BackgroundVariant,
  type Connection,
  Controls,
  type IsValidConnection,
  MiniMap,
  type NodeChange,
  type OnBeforeDelete,
  type OnConnectEnd,
  ReactFlow,
  useReactFlow,
} from "@xyflow/react";
import { type DragEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { RemotePresence } from "../collab/RemotePresence";
import { shareCursor } from "../collab/session";
import { DND_MIME, FRAGMENT_MIME, decodeFragmentItem, decodePaletteItem } from "../lib/dnd";
import type { BuilderEdge, BuilderNode } from "../lib/types";
import { nodeKey } from "../lib/types";
import { useBuilder } from "../store/builder";
import { useShell } from "../shell/shellStore";
import { useColorMode } from "../shell/colorMode";
import { insertPayload, insertSpec } from "./actions";
import { decoratorLabel } from "./insertables";
import { useCatalogPrefs } from "./catalogPrefs";
import { CanvasContextMenu } from "./ContextMenu";
import { DiffPanel, RunLegend } from "./CanvasPanels";
import { EmptyState } from "./EmptyState";
import { InsertableEdge } from "./edges/InsertableEdge";
import { setFlowInstance, setPointer } from "./flowInstance";
import { type Box as Rect, COLLAPSED_GROUP_SIZE, absBox, absPos, descendantsOf, groupAt, nodeSize } from "./geometry";
import { ancestors, checkConnection } from "./graph";
import { collapsedView, displayEdgeEndpoints } from "./grouping";
import { type HelperLine, computeHelperLines } from "./helperLines";
import { HelperLines } from "./HelperLines";
import { InsertPopover } from "./InsertPopover";
import { nodeTypes } from "./nodes";
import { runStateHex } from "./runState";
import { SNAP_GRID, useCanvasUi } from "./uiStore";
import { useCanvasCommands, useClipboardEvents } from "./useCanvasCommands";

const edgeTypes = { default: InsertableEdge };

/** Above this many nodes, only elements inside the viewport are rendered. */
const VIRTUALIZE_ABOVE = 150;

const KIND_COLORS: Record<BuilderNode["type"], string> = {
  operator: "#017cee",
  python_task: "#3b8c3b",
  custom: "#8b5cf6",
  task_group: "rgba(148,163,184,0.35)",
  note: "#facc15",
};

/** Muted minimap in dark mode (the light palette glares on a dark canvas). */
const KIND_COLORS_DARK: Record<BuilderNode["type"], string> = {
  operator: "#2f5f8f",
  python_task: "#3f6b3f",
  custom: "#5b4a8c",
  task_group: "rgba(148,163,184,0.16)",
  note: "#8a7424",
};
const MINIMAP_DARK = {
  bgColor: "#18181b",
  maskColor: "rgba(9, 9, 11, 0.6)",
  maskStrokeColor: "#3f3f46",
  nodeStrokeColor: "#27272a",
} as const;

/**
 * Display copies of store nodes/edges (collapsed groups, aria labels, setup/teardown edges).
 * Copies are cached per source object so unchanged nodes keep their identity (no re-render).
 */
function useDisplayGraph(nodes: BuilderNode[], edges: BuilderEdge[]) {
  const { t } = useTranslation("canvas");
  // Memo caches keyed by source object (state-held so they survive re-renders).
  const [nodeCache] = useState(() => new WeakMap<BuilderNode, { key: string; node: BuilderNode }>());
  const [edgeCache] = useState(() => new WeakMap<BuilderEdge, { key: string; edge: BuilderEdge }>());

  const view = useMemo(() => collapsedView(nodes), [nodes]);
  const displayNodes = useMemo(
    () =>
      nodes.map((n) => {
        const hidden = view.hidden.has(n.id);
        const collapsed = n.type === "task_group" && !!n.data.collapsed;
        const spec = n.data.spec;
        const ariaLabel =
          spec.kind === "operator"
            ? t("aria.operator", { name: nodeKey(n), operator: spec.operator.split(".").pop() })
            : spec.kind === "python_task"
              ? t("aria.python", { name: nodeKey(n), decorator: decoratorLabel(spec.decorator ?? "task") })
              : spec.kind === "custom"
                ? t("aria.custom", { name: nodeKey(n), handler: spec.handler })
                : spec.kind === "task_group"
                  ? t(collapsed ? "aria.groupCollapsed" : "aria.group", { name: nodeKey(n) })
                  : t("aria.note", { text: (spec.text ?? "").slice(0, 60) });
        const key = `${hidden}|${collapsed}|${ariaLabel}`;
        const cached = nodeCache.get(n);
        if (cached?.key === key) return cached.node;
        const node = {
          ...n,
          hidden,
          ariaLabel,
          ...(collapsed ? { style: { ...n.style, ...COLLAPSED_GROUP_SIZE } } : {}),
          ...(n.type === "note" ? { connectable: false } : {}),
        } as BuilderNode;
        nodeCache.set(n, { key, node });
        return node;
      }),
    [nodes, view, t, nodeCache],
  );

  // Edges touching a setup/teardown task are drawn dashed.
  const stKey = useMemo(
    () =>
      nodes
        .filter(
          (n) =>
            n.data.spec.kind !== "task_group" &&
            n.data.spec.kind !== "note" &&
            (n.data.spec.is_setup || n.data.spec.is_teardown),
        )
        .map((n) => n.id)
        .join(","),
    [nodes],
  );
  const displayEdges = useMemo(() => {
    const st = new Set(stKey ? stKey.split(",") : []);
    const ends = displayEdgeEndpoints(edges, view);
    return edges.map((e) => {
      const end = ends.get(e.id) ?? { source: e.source, target: e.target, hidden: false };
      const setupTeardown = st.has(e.source) || st.has(e.target);
      const key = `${end.source}|${end.target}|${end.hidden}|${setupTeardown}`;
      const cached = edgeCache.get(e);
      if (cached?.key === key) return cached.edge;
      const edge: BuilderEdge = {
        ...e,
        source: end.source,
        target: end.target,
        hidden: end.hidden,
        data: { ...e.data, setupTeardown },
      };
      edgeCache.set(e, { key, edge });
      return edge;
    });
  }, [edges, view, stKey, edgeCache]);

  return { displayNodes, displayEdges };
}

export function FlowCanvas() {
  const { t } = useTranslation("canvas");
  const nodes = useBuilder((s) => s.nodes);
  const edges = useBuilder((s) => s.edges);
  const runOverlay = useBuilder((s) => s.runOverlay);
  const onNodesChange = useBuilder((s) => s.onNodesChange);
  const onEdgesChange = useBuilder((s) => s.onEdgesChange);
  const onConnect = useBuilder((s) => s.onConnect);
  const snapToGrid = useCanvasUi((s) => s.snapToGrid);
  const showMinimap = useCanvasUi((s) => s.showMinimap);
  const colorMode = useColorMode((s) => s.mode);
  const rf = useReactFlow<BuilderNode, BuilderEdge>();
  const wrapper = useRef<HTMLDivElement>(null);
  const [lines, setLines] = useState<HelperLine[]>([]);
  const dragTargets = useRef<Rect[] | null>(null);

  const { displayNodes, displayEdges } = useDisplayGraph(nodes, edges);

  useEffect(() => {
    setFlowInstance(rf, wrapper.current);
    return () => setFlowInstance(null, null);
  }, [rf]);

  useCanvasCommands();
  const getRoot = useCallback(() => wrapper.current, []);
  useClipboardEvents(getRoot);

  const isValidConnection: IsValidConnection<BuilderEdge> = useCallback((c: BuilderEdge | Connection) => {
    const { nodes: ns, edges: es } = useBuilder.getState();
    return checkConnection(ns, es, c.source, c.target).ok;
  }, []);

  const onDrop = useCallback(
    (event: DragEvent) => {
      event.preventDefault();
      const point = rf.screenToFlowPosition({ x: event.clientX, y: event.clientY });
      const fragment = decodeFragmentItem(event.dataTransfer.getData(FRAGMENT_MIME));
      if (fragment) {
        useCatalogPrefs.getState().recordRecent(`fragment:${fragment.templateId}`);
        void insertPayload({ kind: "fragment", templateId: fragment.templateId }, point);
        return;
      }
      const item = decodePaletteItem(event.dataTransfer.getData(DND_MIME));
      if (!item) return;
      if (item.key) useCatalogPrefs.getState().recordRecent(item.key);
      insertSpec(item.spec, point);
    },
    [rf],
  );

  // ---- helper lines (single-node drags, when not snapping to the grid) ----
  const onNodeDragStart = useCallback((_: unknown, node: BuilderNode, dragged: BuilderNode[]) => {
    if (dragged.length !== 1) {
      dragTargets.current = null;
      return;
    }
    const { nodes: ns } = useBuilder.getState();
    const byId = new Map(ns.map((n) => [n.id, n]));
    const exclude = new Set([node.id, ...descendantsOf(ns, [node.id]), ...ancestors(ns, node.id)]);
    const hidden = collapsedView(ns).hidden;
    dragTargets.current = ns.filter((n) => !exclude.has(n.id) && !hidden.has(n.id)).map((n) => absBox(ns, n, byId));
  }, []);

  const handleNodesChange = useCallback(
    (changes: NodeChange<BuilderNode>[]) => {
      const first = changes[0];
      if (
        changes.length === 1 &&
        first?.type === "position" &&
        first.dragging &&
        first.position &&
        dragTargets.current &&
        !useCanvasUi.getState().snapToGrid
      ) {
        const { nodes: ns } = useBuilder.getState();
        const node = ns.find((n) => n.id === first.id);
        if (node) {
          const parent = node.parentId ? absPos(ns, node.parentId) : { x: 0, y: 0 };
          const box = { x: first.position.x + parent.x, y: first.position.y + parent.y, ...nodeSize(node) };
          const res = computeHelperLines(box, dragTargets.current, 6 / rf.getZoom());
          setLines(res.lines);
          onNodesChange([{ ...first, position: { x: first.position.x + res.dx, y: first.position.y + res.dy } }]);
          return;
        }
      }
      onNodesChange(changes);
    },
    [onNodesChange, rf],
  );

  const onNodeDragStop = useCallback((_: unknown, node: BuilderNode) => {
    setLines([]);
    dragTargets.current = null;
    const { nodes: ns, setParent } = useBuilder.getState();
    const current = ns.find((n) => n.id === node.id) ?? node;
    const abs = absPos(ns, node.id);
    const { width: w, height: h } = nodeSize(current);
    const group = groupAt(ns, { x: abs.x + w / 2, y: abs.y + Math.min(h / 2, 24) }, node.id);
    if ((group ?? undefined) !== current.parentId) setParent(node.id, group);
  }, []);

  // Route node deletion through the store so group members are re-parented, not orphaned.
  const onBeforeDelete: OnBeforeDelete<BuilderNode, BuilderEdge> = useCallback(
    async ({ nodes: del, edges: delEdges }) => {
      useBuilder.getState().removeElements(del.map((n) => n.id));
      return { nodes: [], edges: delEdges };
    },
    [],
  );

  const onConnectEnd: OnConnectEnd = useCallback(
    (event, state) => {
      if (state.isValid || state.toNode || !state.fromNode || !state.fromHandle) return;
      const target = event.target;
      if (!(target instanceof Element) || !target.classList.contains("react-flow__pane")) return;
      const p = "changedTouches" in event ? event.changedTouches[0] : event;
      if (!p) return;
      useCanvasUi.getState().openInsert({
        screen: { x: p.clientX, y: p.clientY },
        flow: rf.screenToFlowPosition({ x: p.clientX, y: p.clientY }),
        mode: { kind: "connect", nodeId: state.fromNode.id, handleType: state.fromHandle.type },
      });
    },
    [rf],
  );

  const nodeColor = useCallback(
    (n: BuilderNode) =>
      (runOverlay && runStateHex(runOverlay.states[n.id]?.state)) ||
      (colorMode === "dark" ? KIND_COLORS_DARK : KIND_COLORS)[n.type],
    [runOverlay, colorMode],
  );

  const ariaLabelConfig = useMemo(
    () => ({
      "node.a11yDescription.default": t("a11y.nodeDefault"),
      "node.a11yDescription.keyboardDisabled": t("a11y.nodeKeyboardDisabled"),
      "node.a11yDescription.ariaLiveMessage": ({ direction, x, y }: { direction: string; x: number; y: number }) =>
        t("a11y.moved", { direction, x, y }),
      "edge.a11yDescription.default": t("a11y.edgeDefault"),
      "controls.ariaLabel": t("a11y.controls"),
      "controls.zoomIn.ariaLabel": t("a11y.zoomIn"),
      "controls.zoomOut.ariaLabel": t("a11y.zoomOut"),
      "controls.fitView.ariaLabel": t("a11y.fitView"),
      "controls.interactive.ariaLabel": t("a11y.interactive"),
      "minimap.ariaLabel": t("a11y.minimap"),
      "handle.ariaLabel": t("a11y.handle"),
    }),
    [t],
  );

  return (
    <CanvasContextMenu>
      <Box
        ref={wrapper}
        h="100%"
        w="100%"
        position="relative"
        onDragOver={(e) => {
          e.preventDefault();
          e.dataTransfer.dropEffect = "copy";
        }}
        onDrop={onDrop}
        onMouseMove={(e) => {
          setPointer({ x: e.clientX, y: e.clientY });
          shareCursor(rf.screenToFlowPosition({ x: e.clientX, y: e.clientY }));
        }}
        onMouseLeave={() => {
          setPointer(null);
          shareCursor(null);
        }}
        onDoubleClick={(e) => {
          if (!(e.target instanceof Element) || !e.target.classList.contains("react-flow__pane")) return;
          useShell.getState().openPalette("insert", rf.screenToFlowPosition({ x: e.clientX, y: e.clientY }));
        }}
      >
        <ReactFlow<BuilderNode, BuilderEdge>
          nodes={displayNodes}
          edges={displayEdges}
          nodeTypes={nodeTypes}
          edgeTypes={edgeTypes}
          onNodesChange={handleNodesChange}
          onEdgesChange={onEdgesChange}
          onConnect={onConnect}
          onConnectEnd={onConnectEnd}
          isValidConnection={isValidConnection}
          onNodeDragStart={onNodeDragStart}
          onNodeDragStop={onNodeDragStop}
          onBeforeDelete={onBeforeDelete}
          onNodeClick={(_, n) => useBuilder.getState().select(n.id)}
          onPaneClick={() => {
            useBuilder.getState().select(null);
            useCanvasUi.getState().closeInsert();
          }}
          onNodeContextMenu={(_, n) => {
            const s = useBuilder.getState();
            if (!s.nodes.find((x) => x.id === n.id)?.selected) s.setSelection([n.id]);
            useCanvasUi.getState().setContextTarget({ kind: "node", id: n.id });
          }}
          onEdgeContextMenu={(_, e) => useCanvasUi.getState().setContextTarget({ kind: "edge", id: e.id })}
          onPaneContextMenu={() => useCanvasUi.getState().setContextTarget({ kind: "pane" })}
          onSelectionContextMenu={() => {
            const first = useBuilder.getState().nodes.find((n) => n.selected);
            if (first) useCanvasUi.getState().setContextTarget({ kind: "node", id: first.id });
          }}
          onEdgeDoubleClick={(_, e) => useCanvasUi.getState().setEditingEdge(e.id)}
          deleteKeyCode={null}
          zoomOnDoubleClick={false}
          snapToGrid={snapToGrid}
          snapGrid={SNAP_GRID}
          onlyRenderVisibleElements={nodes.length > VIRTUALIZE_ABOVE}
          nodesFocusable
          edgesFocusable
          colorMode={colorMode}
          ariaLabelConfig={ariaLabelConfig}
          fitView
          fitViewOptions={{ maxZoom: 1, padding: 0.3 }}
          maxZoom={1.5}
          proOptions={{ hideAttribution: true }}
        >
          <Background gap={16} variant={snapToGrid ? BackgroundVariant.Cross : BackgroundVariant.Dots} />
          <Controls />
          {showMinimap && nodes.length > 0 ? (
            <MiniMap<BuilderNode>
              pannable
              zoomable
              nodeStrokeWidth={2}
              nodeColor={nodeColor}
              nodeBorderRadius={4}
              {...(colorMode === "dark" ? MINIMAP_DARK : {})}
            />
          ) : null}
          <HelperLines lines={lines} />
          <RunLegend />
          <DiffPanel />
          <RemotePresence />
        </ReactFlow>
        <InsertPopover />
        <EmptyState />
      </Box>
    </CanvasContextMenu>
  );
}
