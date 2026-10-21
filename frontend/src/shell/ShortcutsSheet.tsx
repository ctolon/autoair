import { Box, CloseButton, Dialog, HStack, Portal, SimpleGrid, Stack, Text } from "@chakra-ui/react";
import { useMemo } from "react";
import { useTranslation } from "react-i18next";

import { shortcutsOf, useCommandList } from "../lib/commands";
import { useShell } from "./shellStore";
import { ShortcutKeys } from "./ShortcutKeys";

/** "?" — every registered command that has a keyboard shortcut, grouped. */
export function ShortcutsSheet() {
  const { t } = useTranslation("canvas");
  const open = useShell((s) => s.shortcutsOpen);
  const commands = useCommandList();
  const groups = useMemo(() => {
    const out = new Map<string, typeof commands>();
    for (const c of commands) {
      if (!shortcutsOf(c).length) continue;
      const g = c.group ?? t("groups.general");
      out.set(g, [...(out.get(g) ?? []), c]);
    }
    return [...out.entries()];
  }, [commands, t]);

  return (
    <Dialog.Root
      open={open}
      onOpenChange={(e) => useShell.getState().setShortcutsOpen(e.open)}
      size="lg"
      scrollBehavior="inside"
      lazyMount
      unmountOnExit
    >
      <Portal>
        <Dialog.Backdrop />
        <Dialog.Positioner>
          <Dialog.Content>
            <Dialog.Header>
              <Dialog.Title>{t("shortcuts.title")}</Dialog.Title>
            </Dialog.Header>
            <Dialog.CloseTrigger asChild>
              <CloseButton size="sm" aria-label={t("shortcuts.close")} />
            </Dialog.CloseTrigger>
            <Dialog.Body>
              <SimpleGrid columns={{ base: 1, md: 2 }} gap={6}>
                {groups.map(([group, cmds]) => (
                  <Box key={group}>
                    <Text fontSize="xs" fontWeight="bold" textTransform="uppercase" color="fg.muted" mb={2}>
                      {group}
                    </Text>
                    <Stack as="dl" gap={1.5}>
                      {cmds.map((c) => (
                        <HStack key={c.id} justify="space-between" gap={3}>
                          <Text as="dt" fontSize="sm">
                            {c.title}
                          </Text>
                          <HStack as="dd" gap={1.5}>
                            {shortcutsOf(c).map((s) => (
                              <ShortcutKeys key={s} shortcut={s} />
                            ))}
                          </HStack>
                        </HStack>
                      ))}
                    </Stack>
                  </Box>
                ))}
              </SimpleGrid>
              <Text fontSize="xs" color="fg.muted" mt={6}>
                {t("shortcuts.footer")}
              </Text>
            </Dialog.Body>
          </Dialog.Content>
        </Dialog.Positioner>
      </Portal>
    </Dialog.Root>
  );
}
