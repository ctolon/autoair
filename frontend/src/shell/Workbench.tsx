import { Badge, Box, Flex, IconButton, Text } from "@chakra-ui/react";
import {
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
  useEffect,
  useRef,
} from "react";
import { useTranslation } from "react-i18next";
import {
  LuChevronDown,
  LuChevronLeft,
  LuChevronRight,
  LuChevronUp,
  LuPanelBottom,
  LuPanelLeft,
  LuPanelRight,
} from "react-icons/lu";

import { getFlow } from "../canvas/flowInstance";
import { useLifecycle } from "../lifecycle/store";
import { useBuilder } from "../store/builder";
import {
  DEFAULT_SIZES,
  MIN_CANVAS,
  MIN_SIZES,
  type PanelId,
  type SidePanelId,
  isPanelOpen,
  overlayFor,
  useLayout,
} from "./layout";

/** Width of a collapsed side panel's rail / height of the collapsed bottom panel's bar. */
const RAIL = 28;
const STEP = 16;

/** Refit the canvas after the space around it changed (panels shown, hidden or resized). */
function refit() {
  window.setTimeout(() => getFlow()?.fitView({ padding: 0.3, maxZoom: 1, duration: 200 }), 60);
}

/**
 * Drag handle between a panel and the canvas (pointer and keyboard: arrows resize,
 * Home/End min/max, double click resets). Carries the panel's collapse button.
 */
/** Largest size of panel `id` that leaves the canvas its minimum (measured from the DOM). */
function maxSizeOf(id: PanelId, handle: Element | null): number {
  if (id === "bottom") {
    const main = handle?.closest("[data-workbench-main]");
    return Math.max(MIN_SIZES.bottom, (main?.getBoundingClientRect().height ?? 800) - MIN_CANVAS.height);
  }
  const width = handle?.closest("[data-testid=workbench]")?.getBoundingClientRect().width ?? 1600;
  const s = useLayout.getState();
  const other = id === "palette" ? "inspector" : "palette";
  const otherWidth = isPanelOpen(s, other) && !s.overlay[other] ? s.sizes[other] : RAIL;
  return Math.max(MIN_SIZES[id], width - otherWidth - MIN_CANVAS.width);
}

function ResizeHandle({ id, children }: { id: PanelId; children?: ReactNode }) {
  const { t } = useTranslation("canvas");
  const size = useLayout((s) => s.sizes[id]);
  const vertical = id !== "bottom";
  // Dragging the inspector/bottom handle towards the canvas grows the panel.
  const sign = id === "palette" ? 1 : -1;
  const ref = useRef<HTMLDivElement>(null);
  const max = () => maxSizeOf(id, ref.current);
  const setSize = (px: number) => useLayout.getState().setSize(id, Math.min(max(), px));
  const onPointerDown = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (e.button !== 0 || (e.target as HTMLElement).closest("button")) return;
    e.preventDefault();
    const start = vertical ? e.clientX : e.clientY;
    const from = useLayout.getState().sizes[id];
    const el = e.currentTarget;
    el.setPointerCapture(e.pointerId);
    const move = (ev: PointerEvent) => setSize(from + sign * ((vertical ? ev.clientX : ev.clientY) - start));
    const up = () => {
      el.removeEventListener("pointermove", move);
      el.removeEventListener("pointerup", up);
      el.removeEventListener("pointercancel", up);
    };
    el.addEventListener("pointermove", move);
    el.addEventListener("pointerup", up);
    el.addEventListener("pointercancel", up);
  };
  const onKeyDown = (e: ReactKeyboardEvent<HTMLDivElement>) => {
    const grow = vertical ? (id === "palette" ? "ArrowRight" : "ArrowLeft") : "ArrowUp";
    const shrink = vertical ? (id === "palette" ? "ArrowLeft" : "ArrowRight") : "ArrowDown";
    if (e.key === grow) setSize(size + STEP);
    else if (e.key === shrink) setSize(size - STEP);
    else if (e.key === "Home") setSize(MIN_SIZES[id]);
    else if (e.key === "End") setSize(max());
    else return;
    e.preventDefault();
  };
  return (
    <Box
      ref={ref}
      role="separator"
      tabIndex={0}
      aria-orientation={vertical ? "vertical" : "horizontal"}
      aria-label={t(`shell.resize.${id}`)}
      aria-valuenow={size}
      aria-valuemin={MIN_SIZES[id]}
      data-testid={`resize-${id}`}
      position="relative"
      flexShrink={0}
      zIndex={3}
      {...(vertical ? { w: "1px", cursor: "col-resize" } : { h: "1px", cursor: "row-resize" })}
      bg="border"
      onPointerDown={onPointerDown}
      onKeyDown={onKeyDown}
      onDoubleClick={() => useLayout.getState().setSize(id, Math.min(max(), DEFAULT_SIZES[id]))}
      // A wider invisible grab area around the 1px line.
      _before={{
        content: '""',
        position: "absolute",
        ...(vertical
          ? { top: 0, bottom: 0, left: "-3px", right: "-3px" }
          : { left: 0, right: 0, top: "-3px", bottom: "-3px" }),
      }}
      _hover={{ bg: "blue.solid" }}
      _focusVisible={{ bg: "blue.solid", outline: "none" }}
    >
      {children}
    </Box>
  );
}

const EDGE_ICON = {
  palette: [LuChevronLeft, LuChevronRight],
  inspector: [LuChevronRight, LuChevronLeft],
  bottom: [LuChevronDown, LuChevronUp],
} as const;

/** Small round button on a panel's edge that hides it. */
function EdgeButton({ id }: { id: PanelId }) {
  const { t } = useTranslation("canvas");
  const [Hide] = EDGE_ICON[id];
  const label = t(`shell.hide.${id}`);
  const pos =
    id === "bottom"
      ? { left: "50%", top: 0, transform: "translate(-50%, -50%)" }
      : { top: "50%", left: 0, transform: "translate(-50%, -50%)" };
  return (
    <IconButton
      aria-label={label}
      title={label}
      data-testid={`collapse-${id}`}
      size="2xs"
      minW="18px"
      h="18px"
      rounded="full"
      variant="outline"
      bg="bg"
      position="absolute"
      zIndex={4}
      opacity={0.85}
      _hover={{ opacity: 1, bg: "bg.muted" }}
      {...pos}
      onClick={() => useLayout.getState().set(id, true)}
    >
      <Hide />
    </IconButton>
  );
}

const RAIL_ICON = { palette: LuPanelLeft, inspector: LuPanelRight, bottom: LuPanelBottom } as const;

/** What a collapsed panel leaves: a slim rail (side) or bar (bottom) that expands it again. */
function Rail({ id, label, badge }: { id: PanelId; label: string; badge?: ReactNode }) {
  const { t } = useTranslation("canvas");
  const Icon = RAIL_ICON[id];
  const show = t(`shell.show.${id}`);
  const vertical = id !== "bottom";
  return (
    <Flex
      asChild
      flexShrink={0}
      align="center"
      gap={2}
      color="fg.muted"
      bg="bg.subtle"
      cursor="pointer"
      _hover={{ bg: "bg.muted", color: "fg" }}
      _focusVisible={{ outline: "2px solid", outlineColor: "blue.focusRing", outlineOffset: "-2px" }}
      {...(vertical
        ? {
            direction: "column",
            w: `${RAIL}px`,
            py: 2,
            ...(id === "palette" ? { borderRightWidth: "1px" } : { borderLeftWidth: "1px" }),
          }
        : { direction: "row", h: `${RAIL}px`, px: 3, borderTopWidth: "1px", w: "100%" })}
    >
      <button
        type="button"
        aria-label={show}
        title={show}
        data-testid={`expand-${id}`}
        onClick={() => useLayout.getState().set(id, false)}
      >
        <Icon />
        <Text
          fontSize="xs"
          fontWeight="medium"
          whiteSpace="nowrap"
          css={
            vertical
              ? { writingMode: "vertical-rl", transform: id === "palette" ? "rotate(180deg)" : undefined }
              : undefined
          }
        >
          {label}
        </Text>
        {badge}
      </button>
    </Flex>
  );
}

function useOpen(id: PanelId): boolean {
  return useLayout((s) => isPanelOpen(s, id));
}

/** Follow the builder's width: narrow builders show the side panels as overlay drawers. */
function useResponsive(root: React.RefObject<HTMLDivElement | null>) {
  useEffect(() => {
    const el = root.current;
    if (!el) return;
    const apply = () => {
      const w = el.getBoundingClientRect().width;
      if (w > 0) useLayout.getState().setOverlay(overlayFor(w));
    };
    apply();
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(apply);
    ro.observe(el);
    return () => ro.disconnect();
  }, [root]);
}

function SidePanel({ id, children }: { id: SidePanelId; children: ReactNode }) {
  const open = useOpen(id);
  const overlay = useLayout((s) => s.overlay[id]);
  const size = useLayout((s) => s.sizes[id]);
  const left = id === "palette";
  const panel = (
    <Box
      data-testid={`panel-${id}`}
      data-overlay={overlay || undefined}
      display={open ? "flex" : "none"}
      flexDirection="column"
      flexShrink={0}
      overflow="hidden"
      bg="bg"
      {...(overlay
        ? {
            position: "absolute",
            top: 0,
            bottom: 0,
            ...(left ? { left: `${RAIL}px` } : { right: `${RAIL}px` }),
            w: `min(${size}px, calc(100% - ${RAIL * 2}px))`,
            zIndex: 20,
            boxShadow: "lg",
            ...(left ? { borderRightWidth: "1px" } : { borderLeftWidth: "1px" }),
          }
        : { w: `${size}px`, minW: 0 })}
    >
      <Box flex="1" minH={0} overflowY={id === "inspector" ? "auto" : "hidden"} position="relative">
        {children}
      </Box>
    </Box>
  );
  // Docked: [panel][handle] | [handle][panel]; collapsed or overlay: a rail (+ the drawer).
  const handle =
    open && !overlay ? (
      <ResizeHandle id={id}>
        <EdgeButton id={id} />
      </ResizeHandle>
    ) : null;
  const rail = !open || overlay ? <PanelRail id={id} /> : null;
  // Overlay drawers close with their own edge button (on the drawer's inner edge).
  const drawerClose =
    open && overlay ? (
      <Box
        position="absolute"
        top={0}
        bottom={0}
        zIndex={21}
        {...(left
          ? { left: `calc(${RAIL}px + min(${size}px, calc(100% - ${RAIL * 2}px)))` }
          : { right: `calc(${RAIL}px + min(${size}px, calc(100% - ${RAIL * 2}px)))` })}
        w={0}
      >
        <Box position="relative" h="100%">
          <EdgeButton id={id} />
        </Box>
      </Box>
    ) : null;
  return left ? (
    <>
      {rail}
      {panel}
      {handle}
      {drawerClose}
    </>
  ) : (
    <>
      {drawerClose}
      {handle}
      {panel}
      {rail}
    </>
  );
}

function PanelRail({ id }: { id: PanelId }) {
  const { t } = useTranslation(["canvas", "lifecycle"]);
  const tab = useLifecycle((s) => s.bottomTab);
  const problems = useBuilder((s) => s.problems.length);
  const errors = useBuilder((s) => s.problems.some((p) => p.severity === "error"));
  if (id === "bottom")
    return (
      <Rail
        id="bottom"
        label={t(`tabs.${tab}`, { ns: "lifecycle" })}
        badge={
          problems ? (
            <Badge size="xs" colorPalette={errors ? "red" : "orange"}>
              {tab === "problems" ? problems : `${t("tabs.problems", { ns: "lifecycle" })} ${problems}`}
            </Badge>
          ) : null
        }
      />
    );
  return <Rail id={id} label={t(`shell.rail.${id}`)} />;
}

/**
 * The builder's work area: palette | canvas over the bottom panel | inspector. Hidden panels
 * take no space at all (0 width, no border, no resize handle) and leave a slim rail that
 * expands them; the canvas refits whenever the space around it changes. Below
 * `OVERLAY_BELOW` the side panels open as drawers over the canvas instead.
 */
export function Workbench({
  palette,
  canvas,
  bottom,
  inspector,
  sidePanels = true,
  inspectorPanel = sidePanels,
}: {
  /** False: no palette and (unless `inspectorPanel`) no inspector (code mode). */
  sidePanels?: boolean;
  /** The inspector without the palette (raw code DAGs: their settings). */
  inspectorPanel?: boolean;
  palette: ReactNode;
  canvas: ReactNode;
  bottom: ReactNode;
  inspector: ReactNode;
}) {
  const root = useRef<HTMLDivElement>(null);
  useResponsive(root);
  const paletteDocked = useLayout((s) => !s.overlay.palette && !s.collapsed.palette);
  const inspectorDocked = useLayout((s) => !s.overlay.inspector && !s.collapsed.inspector);
  const bottomOpen = useOpen("bottom");
  const bottomSize = useLayout((s) => s.sizes.bottom);

  // Refit when the canvas' share of the space changes (not on every resize-drag frame).
  const first = useRef(true);
  useEffect(() => {
    if (first.current) {
      first.current = false;
      return;
    }
    refit();
  }, [paletteDocked, inspectorDocked, bottomOpen, sidePanels, inspectorPanel]);

  return (
    <Flex ref={root} flex="1" minH={0} minW={0} position="relative" overflow="hidden" data-testid="workbench">
      {sidePanels ? <SidePanel id="palette">{palette}</SidePanel> : null}
      <Flex direction="column" flex="1" minW={0} minH={0} data-workbench-main="">
        <Box flex="1" minH={0} position="relative">
          {canvas}
        </Box>
        {bottomOpen ? (
          <ResizeHandle id="bottom">
            <EdgeButton id="bottom" />
          </ResizeHandle>
        ) : (
          <PanelRail id="bottom" />
        )}
        <Box
          data-testid="panel-bottom"
          display={bottomOpen ? "block" : "none"}
          h={`${bottomSize}px`}
          maxH={`calc(100% - ${MIN_CANVAS.height}px)`}
          flexShrink={0}
          overflow="hidden"
        >
          {bottom}
        </Box>
      </Flex>
      {inspectorPanel ? <SidePanel id="inspector">{inspector}</SidePanel> : null}
    </Flex>
  );
}
