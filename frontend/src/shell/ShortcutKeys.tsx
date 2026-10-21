import { HStack, Kbd } from "@chakra-ui/react";

import { shortcutParts } from "../lib/commands";

/** A shortcut rendered as keycaps. */
export function ShortcutKeys({ shortcut, size = "sm" }: { shortcut: string; size?: "sm" | "md" }) {
  return (
    <HStack gap={0.5} flexShrink={0} aria-label={shortcutParts(shortcut).join(" + ")}>
      {shortcutParts(shortcut).map((p, i) => (
        <Kbd key={i} size={size} aria-hidden>
          {p}
        </Kbd>
      ))}
    </HStack>
  );
}
