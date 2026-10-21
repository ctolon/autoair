import {
  Box,
  Button,
  ButtonGroup,
  CloseButton,
  Drawer,
  HStack,
  IconButton,
  Portal,
  Spinner,
  Stack,
  Text,
  Textarea,
} from "@chakra-ui/react";
import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef } from "react";
import { useTranslation } from "react-i18next";
import {
  LuBot,
  LuHistory,
  LuMaximize2,
  LuMessageSquarePlus,
  LuMinimize2,
  LuSend,
  LuShieldCheck,
  LuSquare,
} from "react-icons/lu";

import { assistantInfoOptions } from "../api/gen/@tanstack/react-query.gen";
import { UsageLine } from "../lifecycle/components/AiFindings";
import { getAiStatus, requestModel } from "../lifecycle/lib/ai";
import { useBuilder } from "../store/builder";
import { ChatMarkdown } from "./ChatMarkdown";
import { AssistantMessage, ToolChips, UserBubble } from "./ChatMessageView";
import { visibleStreamText } from "./chatText";
import { type AssistantMode, type SendContext, awaitingApproval, useAiChat } from "./store";
import { ThreadList } from "./ThreadList";

const EXAMPLES: Record<AssistantMode, string[]> = {
  developer: ["explain", "retries", "failure", "sensor"],
  admin: ["deploy", "operators", "changes", "usage"],
};

function sendContext(): SendContext {
  const st = useBuilder.getState();
  return {
    spec: st.nodes.length ? st.toSpec() : undefined,
    model: requestModel(getAiStatus()),
  };
}

function Composer({ disabled }: { disabled: boolean }) {
  const { t } = useTranslation("ai");
  const draft = useAiChat((s) => s.draft);
  const pending = useAiChat((s) => !!s.pending);
  const paused = useAiChat((s) => awaitingApproval(s.messages));
  const ref = useRef<HTMLTextAreaElement>(null);
  const open = useAiChat((s) => s.open);
  useEffect(() => {
    if (open) requestAnimationFrame(() => ref.current?.focus());
  }, [open]);
  const send = () => void useAiChat.getState().send(useAiChat.getState().draft, sendContext());
  return (
    <Stack gap={1} w="full">
      <HStack align="end" gap={2}>
        <Textarea
          ref={ref}
          size="sm"
          rows={2}
          autoresize
          maxH="200px"
          value={draft}
          disabled={disabled}
          placeholder={t("chat.placeholder")}
          aria-label={t("chat.placeholder")}
          onChange={(e) => useAiChat.getState().setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              if (!pending) send();
            }
          }}
          data-testid="chat-input"
        />
        {pending ? (
          <IconButton
            aria-label={t("chat.stop")}
            title={t("chat.stop")}
            colorPalette="red"
            variant="subtle"
            onClick={() => useAiChat.getState().stop()}
            data-testid="chat-stop"
          >
            <LuSquare />
          </IconButton>
        ) : (
          <IconButton
            aria-label={t("chat.send")}
            title={t("chat.send")}
            colorPalette="blue"
            disabled={disabled || !draft.trim()}
            onClick={send}
            data-testid="chat-send"
          >
            <LuSend />
          </IconButton>
        )}
      </HStack>
      <Text fontSize="2xs" color={paused ? "orange.fg" : "fg.subtle"} data-testid="chat-hint">
        {paused ? t("approval.newMessageCancels") : t("chat.hint")}
      </Text>
    </Stack>
  );
}

function EmptyState({ mode }: { mode: AssistantMode }) {
  const { t } = useTranslation("ai");
  return (
    <Stack gap={3} py={6} align="center" textAlign="center" data-testid="chat-empty">
      <Box color="blue.fg" fontSize="2xl">
        {mode === "admin" ? <LuShieldCheck /> : <LuBot />}
      </Box>
      <Text fontWeight="semibold">{t(`modes.${mode}.title`)}</Text>
      <Text fontSize="sm" color="fg.muted" maxW="420px">
        {t(`modes.${mode}.intro`)}
      </Text>
      <Stack gap={1.5} w="full" maxW="440px">
        {EXAMPLES[mode].map((key) => (
          <Button
            key={key}
            size="sm"
            variant="outline"
            justifyContent="flex-start"
            whiteSpace="normal"
            h="auto"
            py={2}
            textAlign="start"
            onClick={() => void useAiChat.getState().send(t(`examples.${mode}.${key}`), sendContext())}
          >
            {t(`examples.${mode}.${key}`)}
          </Button>
        ))}
      </Stack>
    </Stack>
  );
}

function Messages() {
  const { t } = useTranslation("ai");
  const messages = useAiChat((s) => s.messages);
  const pending = useAiChat((s) => s.pending);
  const threadId = useAiChat((s) => s.threadId);
  const mode = useAiChat((s) => s.mode);
  const loading = useAiChat((s) => s.loading);
  const error = useAiChat((s) => s.error);
  const end = useRef<HTMLDivElement>(null);
  const streamText = pending ? visibleStreamText(pending.stream.text) : "";
  useEffect(() => {
    end.current?.scrollIntoView({ block: "end" });
  }, [messages.length, streamText.length, pending?.stream.tools.length]);
  const lastAssistant = [...messages].reverse().find((m) => m.role === "assistant");
  const followUp = (q: string) => void useAiChat.getState().send(q, sendContext());
  const regenerate = () => void useAiChat.getState().regenerate(sendContext());
  if (loading) return <Spinner size="sm" m={4} />;
  return (
    <Stack gap={4} py={3} data-testid="chat-messages">
      {!messages.length && !pending ? <EmptyState mode={mode} /> : null}
      {messages.map((m) =>
        m.role === "user" ? (
          <UserBubble key={m.id} text={m.content} />
        ) : (
          <AssistantMessage
            key={m.id}
            message={m}
            threadId={threadId}
            isLast={m === lastAssistant && !pending}
            busy={!!pending}
            onFollowUp={followUp}
            onRegenerate={regenerate}
            onApproval={
              m.id
                ? (approvalId, decisions) => void useAiChat.getState().decideApproval(m.id ?? "", approvalId, decisions)
                : undefined
            }
            onProposal={(p) =>
              useAiChat.getState().replaceMessage({
                ...m,
                proposals: (m.proposals ?? []).map((x) => (x.id === p.id ? p : x)),
              })
            }
          />
        ),
      )}
      {pending ? (
        <>
          {pending.resumeOf ? null : <UserBubble text={pending.userText} />}
          <Stack gap={2} data-testid="chat-pending" aria-busy="true">
            <ToolChips tools={pending.stream.tools} />
            <HStack gap={2} color="fg.muted" fontSize="xs">
              <Spinner size="xs" />
              <Text>
                {pending.stream.stage && pending.stream.stage !== "thread"
                  ? t(`stage.${pending.stream.stage}`, { defaultValue: pending.stream.stage })
                  : t("chat.thinking")}
              </Text>
            </HStack>
            {streamText ? <ChatMarkdown text={streamText} /> : null}
            {pending.stream.usage ? <UsageLine usage={pending.stream.usage} /> : null}
          </Stack>
        </>
      ) : null}
      {error ? (
        <Box
          borderWidth="1px"
          borderColor="red.muted"
          bg="red.subtle"
          rounded="md"
          p={2}
          role="alert"
          data-testid="chat-error"
        >
          <Text fontSize="sm" color="fg.error">
            {error === "interrupted" ? t("chat.interrupted") : error}
          </Text>
        </Box>
      ) : null}
      <div ref={end} />
    </Stack>
  );
}

/** "Ask AI": the developer / admin assistant chat in a right-side drawer. */
export default function ChatPanel() {
  const { t, i18n } = useTranslation("ai");
  const open = useAiChat((s) => s.open);
  const expanded = useAiChat((s) => s.expanded);
  const threadsOpen = useAiChat((s) => s.threadsOpen);
  const mode = useAiChat((s) => s.mode);
  const title = useAiChat((s) => s.title);
  const info = useQuery({ ...assistantInfoOptions(), enabled: open, staleTime: 60_000 }).data;
  const adminAllowed = !!info?.admin.allowed && info.admin.enabled !== false;
  useEffect(() => {
    if (info && mode === "admin" && !adminAllowed) useAiChat.getState().setMode("developer");
  }, [info, mode, adminAllowed]);
  return (
    <Drawer.Root
      open={open}
      onOpenChange={(e) => !e.open && useAiChat.getState().close()}
      placement="end"
      size={expanded ? "full" : "md"}
      modal={false}
      closeOnInteractOutside={false}
      trapFocus={false}
      preventScroll={false}
    >
      <Portal>
        <Drawer.Positioner pointerEvents="none">
          <Drawer.Content
            pointerEvents="auto"
            data-testid="ai-chat-panel"
            lang={i18n.resolvedLanguage ?? i18n.language}
            bg="bg"
            borderStartWidth="1px"
            shadow="lg"
            css={{ colorScheme: "light", _dark: { colorScheme: "dark" } }}
          >
            <Drawer.Header borderBottomWidth="1px" py={2} px={3}>
              <Stack gap={1.5} w="full">
                <HStack gap={2} justify="space-between">
                  <HStack gap={2} minW={0}>
                    <Box color="blue.fg">
                      <LuBot />
                    </Box>
                    <Drawer.Title fontSize="md" truncate>
                      {t("chat.title")}
                    </Drawer.Title>
                  </HStack>
                  <HStack gap={0.5}>
                    <IconButton
                      size="xs"
                      variant={threadsOpen ? "subtle" : "ghost"}
                      aria-label={t("threads.title")}
                      title={t("threads.title")}
                      aria-pressed={threadsOpen}
                      onClick={() => useAiChat.getState().setThreadsOpen(!threadsOpen)}
                      data-testid="chat-threads-toggle"
                    >
                      <LuHistory />
                    </IconButton>
                    <IconButton
                      size="xs"
                      variant="ghost"
                      aria-label={t("chat.newChat")}
                      title={t("chat.newChat")}
                      onClick={() => useAiChat.getState().newChat()}
                      data-testid="chat-new"
                    >
                      <LuMessageSquarePlus />
                    </IconButton>
                    <IconButton
                      size="xs"
                      variant="ghost"
                      aria-label={expanded ? t("chat.collapse") : t("chat.expand")}
                      title={expanded ? t("chat.collapse") : t("chat.expand")}
                      onClick={() => useAiChat.getState().setExpanded(!expanded)}
                    >
                      {expanded ? <LuMinimize2 /> : <LuMaximize2 />}
                    </IconButton>
                    <Drawer.CloseTrigger asChild position="static">
                      <CloseButton size="xs" aria-label={t("chat.close")} />
                    </Drawer.CloseTrigger>
                  </HStack>
                </HStack>
                <HStack gap={2} justify="space-between" wrap="wrap">
                  <ButtonGroup size="2xs" attached variant="outline" role="radiogroup" aria-label={t("modes.label")}>
                    {(["developer", "admin"] as const).map((m) => (
                      <Button
                        key={m}
                        role="radio"
                        aria-checked={mode === m}
                        variant={mode === m ? "solid" : "outline"}
                        colorPalette={mode === m ? "blue" : "gray"}
                        disabled={m === "admin" && !adminAllowed}
                        title={m === "admin" && !adminAllowed ? t("modes.adminDenied") : undefined}
                        onClick={() => useAiChat.getState().setMode(m)}
                        data-testid={`chat-mode-${m}`}
                      >
                        {m === "admin" ? <LuShieldCheck /> : <LuBot />} {t(`modes.${m}.label`)}
                      </Button>
                    ))}
                  </ButtonGroup>
                  {title ? (
                    <Text fontSize="xs" color="fg.muted" truncate maxW="60%" data-testid="chat-thread-title">
                      {title}
                    </Text>
                  ) : null}
                </HStack>
              </Stack>
            </Drawer.Header>
            <Drawer.Body p={0} display="flex" minH={0}>
              {threadsOpen ? (
                <Box
                  w={expanded ? "300px" : "100%"}
                  borderEndWidth={expanded ? "1px" : 0}
                  p={2}
                  minH={0}
                  flexShrink={0}
                >
                  <ThreadList />
                </Box>
              ) : null}
              {!threadsOpen || expanded ? (
                <Box flex="1" minW={0} overflowY="auto" px={{ base: 3, md: expanded ? 8 : 4 }}>
                  <Box maxW={expanded ? "900px" : undefined} mx="auto">
                    <Messages />
                  </Box>
                </Box>
              ) : null}
            </Drawer.Body>
            <Drawer.Footer borderTopWidth="1px" p={3}>
              <Box w="full" maxW={expanded ? "900px" : undefined} mx="auto">
                <Composer disabled={false} />
              </Box>
            </Drawer.Footer>
          </Drawer.Content>
        </Drawer.Positioner>
      </Portal>
    </Drawer.Root>
  );
}
