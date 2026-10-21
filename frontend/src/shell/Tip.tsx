import { Portal, Tooltip } from "@chakra-ui/react";
import type { ReactElement, ReactNode } from "react";

/**
 * Small hover/focus tooltip. Unlike the native `title`, it also shows on menu items that are
 * disabled (data-disabled) and in screenshots/tests. Renders the child untouched without content.
 * The tip ignores the pointer, so it never blocks clicks on whatever it happens to cover.
 */
export function Tip({
  content,
  children,
  placement = "bottom",
  openDelay = 250,
}: {
  content: ReactNode;
  children: ReactElement;
  placement?: "top" | "bottom" | "left" | "left-start" | "right" | "right-start" | "bottom-start" | "bottom-end";
  openDelay?: number;
}) {
  if (content == null || content === "" || content === false) return children;
  return (
    <Tooltip.Root openDelay={openDelay} closeDelay={50} positioning={{ placement }} interactive={false}>
      <Tooltip.Trigger asChild>{children}</Tooltip.Trigger>
      <Portal>
        {/* Non-interactive: the tip must never take clicks meant for what it overlaps (menu items). */}
        <Tooltip.Positioner pointerEvents="none">
          <Tooltip.Content maxW="320px" fontSize="xs" whiteSpace="pre-line">
            {content}
          </Tooltip.Content>
        </Tooltip.Positioner>
      </Portal>
    </Tooltip.Root>
  );
}
