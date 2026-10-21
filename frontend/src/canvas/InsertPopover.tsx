import { Box, Portal } from "@chakra-ui/react";
import { Command } from "cmdk";
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { useBuilder } from "../store/builder";
import { insertItem, insertSpec } from "./actions";
import { useCatalogPrefs } from "./catalogPrefs";
import { InsertGroups, cmdkCss } from "./InsertList";
import type { InsertItem } from "./insertables";
import { type InsertRequest, useCanvasUi } from "./uiStore";

const WIDTH = 340;
const HEIGHT = 380;

async function pick(req: InsertRequest, item: InsertItem): Promise<void> {
  const { mode, flow } = req;
  if (mode.kind === "point") {
    await insertItem(item, flow);
    return;
  }
  const payload = item.payload();
  if (payload.kind !== "spec") return;
  useCatalogPrefs.getState().recordRecent(item.key);
  const store = useBuilder.getState();
  if (mode.kind === "edge") {
    const id = store.insertOnEdge(mode.edgeId, payload.spec);
    if (id) store.setSelection([id]);
    return;
  }
  // Dropped a connection on the empty pane: create the node there and connect it.
  const at = mode.handleType === "source" ? flow : { x: flow.x - 220, y: flow.y };
  const id = insertSpec(payload.spec, { x: at.x, y: at.y - 32 });
  if (mode.handleType === "source") store.connect(mode.nodeId, id);
  else store.connect(id, mode.nodeId);
}

/** Mini searchable palette opened from an edge "+" button or a connection dropped on the pane. */
export function InsertPopover() {
  const req = useCanvasUi((s) => s.insert);
  if (!req) return null;
  return <InsertPopoverBody req={req} />;
}

function InsertPopoverBody({ req }: { req: InsertRequest }) {
  const { t } = useTranslation("canvas");
  const [search, setSearch] = useState("");
  const ref = useRef<HTMLDivElement>(null);
  const close = useCanvasUi((s) => s.closeInsert);

  useEffect(() => {
    const onDown = (e: PointerEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) close();
    };
    document.addEventListener("pointerdown", onDown, true);
    return () => document.removeEventListener("pointerdown", onDown, true);
  }, [close]);

  // Nodes/fragments make no sense between two tasks; notes cannot be connected.
  const filter = useCallback(
    (i: InsertItem) => req.mode.kind === "point" || (i.key !== "builtin:note" && i.category !== "fragment"),
    [req.mode.kind],
  );

  const left = Math.max(8, Math.min(req.screen.x - WIDTH / 2, window.innerWidth - WIDTH - 8));
  const top = Math.max(8, Math.min(req.screen.y, window.innerHeight - HEIGHT - 8));

  return (
    <Portal>
      <Box
        ref={ref}
        position="fixed"
        left={`${left}px`}
        top={`${top}px`}
        w={`${WIDTH}px`}
        zIndex="popover"
        bg="bg.panel"
        borderWidth="1px"
        borderColor="border.emphasized"
        rounded="lg"
        shadow="lg"
        overflow="hidden"
        css={{ ...cmdkCss, "& [cmdk-list]": { ...cmdkCss["& [cmdk-list]"], maxH: `${HEIGHT - 48}px` } }}
      >
        <Command
          label={t("insertPopover.label")}
          loop
          onKeyDown={(e) => {
            if (e.key === "Escape") {
              e.stopPropagation();
              close();
            }
          }}
        >
          <Command.Input
            autoFocus
            value={search}
            onValueChange={setSearch}
            placeholder={t("insertPopover.placeholder")}
          />
          <Command.List>
            <Command.Empty>{t("palette.empty")}</Command.Empty>
            <InsertGroups
              search={search}
              filter={filter}
              fragments={req.mode.kind === "point"}
              onPick={(item) => {
                close();
                void pick(req, item);
              }}
            />
          </Command.List>
        </Command>
      </Box>
    </Portal>
  );
}
