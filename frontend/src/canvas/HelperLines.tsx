import { Box } from "@chakra-ui/react";
import { ViewportPortal, useStore } from "@xyflow/react";

import type { HelperLine } from "./helperLines";

/** Renders alignment guides in canvas coordinates (1 screen pixel thick at any zoom). */
export function HelperLines({ lines }: { lines: HelperLine[] }) {
  const zoom = useStore((s) => s.transform[2]);
  if (!lines.length) return null;
  const px = 1 / zoom;
  return (
    <ViewportPortal>
      {lines.map((l, i) => (
        <Box
          key={i}
          position="absolute"
          pointerEvents="none"
          bg="pink.500"
          zIndex={1000}
          aria-hidden
          style={
            l.orientation === "v"
              ? { left: l.pos - px / 2, top: l.from, width: px, height: l.to - l.from }
              : { top: l.pos - px / 2, left: l.from, height: px, width: l.to - l.from }
          }
        />
      ))}
    </ViewportPortal>
  );
}
