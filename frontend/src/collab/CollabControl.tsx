import { Box, Button, Circle, HStack, Input, Popover, Portal, Stack, Text } from "@chakra-ui/react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { LuUsers } from "react-icons/lu";
import { useAccess } from "../lib/access";

import { useMeta } from "../lifecycle/hooks";

import { displayName, setDisplayName, startCollab, stopCollab, useCollab } from "./session";

const initials = (name: string) =>
  name
    .split(/[\s._-]+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((p) => p.charAt(0).toUpperCase())
    .join("") || "?";

/** Toolbar control: join/leave the DAG's live session, presence avatars, display name. */
export function CollabControl() {
  const { t } = useTranslation("lifecycle");
  const meta = useMeta();
  const access = useAccess();
  const status = useCollab((s) => s.status);
  const peers = useCollab((s) => s.peers);
  const error = useCollab((s) => s.error);
  const [name, setName] = useState(displayName);

  useEffect(() => () => stopCollab(), []);

  if (!meta?.collab_enabled || !access.allows("collab", "spec:write")) return null;
  const active = status !== "off";
  const statusText =
    status === "connected"
      ? t("collab.connected", { count: peers.length })
      : status === "error"
        ? (error ?? t("collab.error"))
        : t("collab.connecting");

  return (
    <HStack gap={1}>
      {peers.slice(0, 5).map((p) => (
        <Circle
          key={p.clientId}
          size="6"
          bg={p.color}
          color="white"
          fontSize="2xs"
          fontWeight="bold"
          title={p.name}
          data-testid="collab-peer"
        >
          {initials(p.name)}
        </Circle>
      ))}
      {peers.length > 5 ? <Text fontSize="xs">+{peers.length - 5}</Text> : null}
      <Popover.Root lazyMount unmountOnExit>
        <Popover.Trigger asChild>
          <Button size="xs" variant={active ? "subtle" : "ghost"} colorPalette={active ? "green" : undefined}>
            <LuUsers />
            {active ? (
              <Box
                as="span"
                w="2"
                h="2"
                borderRadius="full"
                bg={status === "connected" ? "green.500" : status === "error" ? "red.500" : "orange.400"}
                data-testid="collab-status"
                data-status={status}
              />
            ) : null}
            {t("collab.start")}
          </Button>
        </Popover.Trigger>
        <Portal>
          <Popover.Positioner>
            <Popover.Content maxW="320px">
              <Popover.Arrow />
              <Popover.Body p={3}>
                <Stack gap={3}>
                  <Text fontWeight="semibold" fontSize="sm">
                    {t("collab.title")}
                  </Text>
                  <Text fontSize="xs" color="fg.muted">
                    {t("collab.hint")}
                  </Text>
                  <Stack gap={1}>
                    <Text fontSize="xs">{t("collab.yourName")}</Text>
                    <Input
                      size="xs"
                      value={name}
                      maxLength={40}
                      onChange={(e) => setName(e.target.value)}
                      onBlur={() => name.trim() && setDisplayName(name.trim())}
                    />
                  </Stack>
                  {active ? (
                    <>
                      <Text fontSize="xs" color={status === "error" ? "red.500" : "fg.muted"}>
                        {statusText}
                      </Text>
                      {status === "connected" && !peers.length ? (
                        <Text fontSize="xs" color="fg.muted">
                          {t("collab.alone")}
                        </Text>
                      ) : null}
                      <Button size="xs" variant="outline" onClick={() => stopCollab()}>
                        {t("collab.stop")}
                      </Button>
                    </>
                  ) : (
                    <Button
                      size="xs"
                      colorPalette="green"
                      onClick={() => {
                        if (name.trim()) setDisplayName(name.trim());
                        void startCollab();
                      }}
                    >
                      {t("collab.start")}
                    </Button>
                  )}
                </Stack>
              </Popover.Body>
            </Popover.Content>
          </Popover.Positioner>
        </Portal>
      </Popover.Root>
    </HStack>
  );
}
