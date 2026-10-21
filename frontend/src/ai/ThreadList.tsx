import { Box, Button, HStack, IconButton, Input, Spinner, Stack, Text } from "@chakra-ui/react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { LuCheck, LuMessageSquarePlus, LuPencil, LuTrash2, LuX } from "react-icons/lu";

import { deleteAssistantThread, listAssistantThreads, renameAssistantThread } from "../api/gen/sdk.gen";
import type { ThreadSummary } from "../api/gen/types.gen";
import { errorMessage } from "../api/setup";
import { formatUsage } from "../lifecycle/lib/ai";
import { toaster } from "../ui/toaster";
import { useAiChat } from "./store";

function ThreadRow({ thread, active }: { thread: ThreadSummary; active: boolean }) {
  const { t, i18n } = useTranslation("ai");
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState(thread.title);
  const client = useQueryClient();
  const date = useMemo(
    () => new Intl.DateTimeFormat(i18n.language, { dateStyle: "short", timeStyle: "short" }),
    [i18n.language],
  );
  const changed = () => {
    useAiChat.getState().threadsChanged();
    void client.invalidateQueries({ queryKey: ["ai", "threads"] });
  };
  const save = async () => {
    const value = title.trim();
    setEditing(false);
    if (!value || value === thread.title) return;
    try {
      await renameAssistantThread({ path: { thread_id: thread.id }, body: { title: value }, throwOnError: true });
      if (useAiChat.getState().threadId === thread.id) useAiChat.setState({ title: value });
      changed();
    } catch (err) {
      toaster.create({ type: "error", title: t("threads.renameFailed"), description: errorMessage(err) });
    }
  };
  const remove = async () => {
    if (!window.confirm(t("threads.confirmDelete", { title: thread.title }))) return;
    try {
      await deleteAssistantThread({ path: { thread_id: thread.id }, throwOnError: true });
      if (useAiChat.getState().threadId === thread.id) useAiChat.getState().newChat();
      changed();
    } catch (err) {
      toaster.create({ type: "error", title: t("threads.deleteFailed"), description: errorMessage(err) });
    }
  };
  const usage = formatUsage(thread.usage);
  return (
    <HStack
      gap={1}
      px={2}
      py={1.5}
      rounded="md"
      bg={active ? "bg.emphasized" : undefined}
      _hover={{ bg: active ? "bg.emphasized" : "bg.muted" }}
      data-testid="chat-thread"
    >
      {editing ? (
        <>
          <Input
            size="xs"
            autoFocus
            value={title}
            aria-label={t("threads.rename")}
            onChange={(e) => setTitle(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") void save();
              if (e.key === "Escape") setEditing(false);
            }}
          />
          <IconButton size="2xs" variant="ghost" aria-label={t("threads.save")} onClick={() => void save()}>
            <LuCheck />
          </IconButton>
          <IconButton size="2xs" variant="ghost" aria-label={t("threads.cancel")} onClick={() => setEditing(false)}>
            <LuX />
          </IconButton>
        </>
      ) : (
        <>
          <Box
            as="button"
            flex="1"
            minW={0}
            textAlign="start"
            onClick={() => void useAiChat.getState().selectThread(thread.id, thread.mode)}
            aria-current={active ? "true" : undefined}
          >
            <Text fontSize="sm" truncate fontWeight={active ? "semibold" : "normal"}>
              {thread.title}
            </Text>
            <Text fontSize="2xs" color="fg.muted" truncate>
              {thread.updated_at ? date.format(new Date(thread.updated_at)) : ""}
              {" · "}
              {t("threads.messages", { count: thread.message_count ?? 0 })}
              {usage ? ` · ${usage}` : ""}
            </Text>
          </Box>
          <IconButton
            size="2xs"
            variant="ghost"
            aria-label={t("threads.rename")}
            title={t("threads.rename")}
            onClick={() => {
              setTitle(thread.title);
              setEditing(true);
            }}
          >
            <LuPencil />
          </IconButton>
          <IconButton
            size="2xs"
            variant="ghost"
            aria-label={t("threads.delete")}
            title={t("threads.delete")}
            onClick={() => void remove()}
            data-testid="chat-thread-delete"
          >
            <LuTrash2 />
          </IconButton>
        </>
      )}
    </HStack>
  );
}

/** The user's conversations with the current assistant (newest first). */
export function ThreadList() {
  const { t } = useTranslation("ai");
  const mode = useAiChat((s) => s.mode);
  const active = useAiChat((s) => s.threadId);
  const version = useAiChat((s) => s.threadsVersion);
  const threads = useQuery({
    queryKey: ["ai", "threads", mode, version],
    queryFn: async ({ signal }) => (await listAssistantThreads({ query: { mode }, signal, throwOnError: true })).data,
    staleTime: 10_000,
  });
  return (
    <Stack gap={1} data-testid="chat-thread-list" h="100%" minH={0}>
      <Button size="xs" variant="outline" onClick={() => useAiChat.getState().newChat()}>
        <LuMessageSquarePlus /> {t("chat.newChat")}
      </Button>
      {threads.isPending ? <Spinner size="sm" m={2} /> : null}
      {threads.isError ? (
        <Text fontSize="xs" color="fg.error" p={2}>
          {errorMessage(threads.error)}
        </Text>
      ) : null}
      {threads.data && !threads.data.length ? (
        <Text fontSize="xs" color="fg.muted" p={2}>
          {t("threads.empty")}
        </Text>
      ) : null}
      <Stack gap={0.5} overflowY="auto" minH={0}>
        {(threads.data ?? []).map((th) => (
          <ThreadRow key={th.id} thread={th} active={th.id === active} />
        ))}
      </Stack>
    </Stack>
  );
}
