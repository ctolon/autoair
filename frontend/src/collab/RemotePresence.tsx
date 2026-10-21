import { Box } from "@chakra-ui/react";
import { ViewportPortal, useStore } from "@xyflow/react";
import { memo } from "react";
import { LuMousePointer2 } from "react-icons/lu";

import { useCollab } from "./session";

/** Collaborators' cursors and selections, drawn in flow coordinates on the canvas. */
export const RemotePresence = memo(function RemotePresence() {
  const peers = useCollab((s) => s.peers);
  const nodeLookup = useStore((s) => s.nodeLookup);
  if (!peers.length) return null;

  return (
    <ViewportPortal>
      {peers.flatMap((p) =>
        p.selection.map((id) => {
          const n = nodeLookup.get(id);
          if (!n) return null;
          const { x, y } = n.internals.positionAbsolute;
          return (
            <Box
              key={`${p.clientId}:${id}`}
              position="absolute"
              pointerEvents="none"
              transform={`translate(${x - 4}px, ${y - 4}px)`}
              w={`${(n.measured.width ?? 0) + 8}px`}
              h={`${(n.measured.height ?? 0) + 8}px`}
              borderWidth="2px"
              borderStyle="dashed"
              borderRadius="md"
              style={{ borderColor: p.color }}
              data-testid="collab-remote-selection"
            />
          );
        }),
      )}
      {peers.map((p) =>
        p.cursor ? (
          <Box
            key={p.clientId}
            position="absolute"
            pointerEvents="none"
            transform={`translate(${p.cursor.x}px, ${p.cursor.y}px)`}
            zIndex={1000}
            data-testid="collab-cursor"
          >
            <LuMousePointer2 color={p.color} fill={p.color} size={16} />
            <Box
              mt="-1"
              ml="3"
              px="1.5"
              borderRadius="sm"
              fontSize="2xs"
              color="white"
              whiteSpace="nowrap"
              style={{ background: p.color }}
            >
              {p.name}
            </Box>
          </Box>
        ) : null,
      )}
    </ViewportPortal>
  );
});
