import { Badge } from "@chakra-ui/react";

import { runStateStyle } from "../../canvas/runState";

/** Airflow state badge, coloured like the canvas run overlay and its legend. */
export function StateBadge({ state }: { state: string | null | undefined }) {
  return (
    <Badge size="xs" colorPalette={runStateStyle(state).palette}>
      {state ?? "none"}
    </Badge>
  );
}
