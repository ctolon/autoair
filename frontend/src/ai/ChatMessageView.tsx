import { Badge, Box, Button, Collapsible, HStack, IconButton, Link, Spinner, Stack, Text } from "@chakra-ui/react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import {
  LuBookOpen,
  LuChevronRight,
  LuCircleCheck,
  LuCircleX,
  LuCopy,
  LuFileCode,
  LuFileText,
  LuGlobe,
  LuRefreshCw,
  LuScrollText,
  LuSettings2,
  LuShieldAlert,
  LuUser,
  LuWrench,
} from "react-icons/lu";

import type { ChatMessage, Citation, Proposal, ToolCallRecord } from "../api/gen/types.gen";
import type { ToolRun } from "../lifecycle/components/AiStream";
import { UsageLine } from "../lifecycle/components/AiFindings";
import { toaster } from "../ui/toaster";
import { ChatMarkdown, copyText } from "./ChatMarkdown";
import { argsLabel, citationHref, citedSources } from "./chatText";
import { ProposalCard } from "./ProposalCard";

const KIND_ICON: Record<Citation["kind"], typeof LuBookOpen> = {
  doc: LuBookOpen,
  source: LuFileCode,
  audit: LuScrollText,
  spec: LuFileText,
  web: LuGlobe,
  log: LuScrollText,
  config: LuSettings2,
  tool: LuWrench,
  file: LuFileText,
};

/** Tool calls of an answer as compact chips (expandable to the result previews). */
export function ToolChips({ tools }: { tools: Array<ToolCallRecord | ToolRun> }) {
  const { t } = useTranslation("ai");
  if (!tools.length) return null;
  return (
    <Collapsible.Root>
      <Collapsible.Trigger asChild>
        <HStack
          as="button"
          gap={1}
          wrap="wrap"
          fontSize="2xs"
          color="fg.muted"
          cursor="pointer"
          data-testid="chat-tools"
          css={{ "&[data-state=open] > svg:first-of-type": { transform: "rotate(90deg)" } }}
        >
          <LuChevronRight />
          <Text>{t("chat.toolsUsed", { count: tools.length })}</Text>
          {tools.slice(0, 8).map((tool, i) => {
            const state = "state" in tool ? tool.state : tool.ok ? "ok" : "failed";
            return (
              <Badge
                key={`${tool.id}-${i}`}
                size="xs"
                variant="surface"
                colorPalette={state === "failed" ? "red" : state === "running" ? "blue" : "gray"}
                fontFamily="mono"
                data-testid="chat-tool-chip"
              >
                {state === "running" ? <Spinner size="xs" /> : state === "ok" ? <LuCircleCheck /> : <LuCircleX />}
                {tool.name}
              </Badge>
            );
          })}
          {tools.length > 8 ? <Text>+{tools.length - 8}</Text> : null}
        </HStack>
      </Collapsible.Trigger>
      <Collapsible.Content>
        <Stack gap={1} mt={1} pl={4}>
          {tools.map((tool, i) => {
            const args = "state" in tool ? tool.args : argsLabel(tool.arguments);
            const ms = "state" in tool ? tool.durationMs : tool.duration_ms;
            return (
              <Box key={`${tool.id}-${i}`} fontSize="2xs" borderLeftWidth="2px" pl={2}>
                <Text fontFamily="mono" fontWeight="semibold">
                  {tool.name}{" "}
                  <Text as="span" color="fg.muted" fontWeight="normal">
                    {args}
                  </Text>
                  {ms != null ? (
                    <Text as="span" color="fg.subtle">
                      {" "}
                      · {ms} ms
                    </Text>
                  ) : null}
                </Text>
                {tool.preview ? (
                  <Text color="fg.muted" fontFamily="mono" lineClamp={3} wordBreak="break-all">
                    {tool.preview}
                  </Text>
                ) : null}
              </Box>
            );
          })}
        </Stack>
      </Collapsible.Content>
    </Collapsible.Root>
  );
}

export function Citations({ items }: { items: Citation[] }) {
  const { t } = useTranslation("ai");
  if (!items.length) return null;
  return (
    <Box data-testid="chat-citations">
      <Text fontSize="2xs" fontWeight="semibold" color="fg.muted" textTransform="uppercase" mb={0.5}>
        {t("chat.sources")}
      </Text>
      <Stack gap={0.5} as="ol" listStyleType="none" m={0} p={0}>
        {items.map((c) => {
          const Icon = KIND_ICON[c.kind] ?? LuBookOpen;
          const href = citationHref(c);
          return (
            <HStack as="li" key={c.n} gap={1.5} fontSize="2xs" minW={0} title={c.snippet || c.ref}>
              <Badge size="xs" variant="outline" flexShrink={0}>
                {c.n}
              </Badge>
              <Box color="fg.muted" flexShrink={0}>
                <Icon />
              </Box>
              {href ? (
                <Link href={href} target="_blank" rel="noopener noreferrer" color="blue.fg" truncate>
                  {c.title}
                </Link>
              ) : (
                <Text truncate>{c.title}</Text>
              )}
              <Text color="fg.subtle" fontFamily="mono" truncate flex="1" minW={0}>
                {c.ref}
              </Text>
            </HStack>
          );
        })}
      </Stack>
    </Box>
  );
}

export function UserBubble({ text }: { text: string }) {
  return (
    <HStack justify="flex-end" align="start" gap={2} data-testid="chat-user-message">
      <Box
        bg="blue.subtle"
        color="fg"
        px={3}
        py={2}
        rounded="lg"
        roundedTopRight="sm"
        maxW="85%"
        fontSize="sm"
        whiteSpace="pre-wrap"
        wordBreak="break-word"
      >
        {text}
      </Box>
      <Box color="fg.muted" mt={2} flexShrink={0}>
        <LuUser />
      </Box>
    </HStack>
  );
}

/** One stored assistant answer: text, tools, sources, proposals, follow-ups, usage. */
export function AssistantMessage({
  message,
  threadId,
  isLast,
  busy,
  onFollowUp,
  onRegenerate,
  onProposal,
}: {
  message: ChatMessage;
  threadId: string | null;
  isLast: boolean;
  busy: boolean;
  onFollowUp: (q: string) => void;
  onRegenerate: () => void;
  onProposal: (p: Proposal) => void;
}) {
  const { t } = useTranslation("ai");
  const [copied, setCopied] = useState(false);
  const failed = !!message.error && !message.content;
  return (
    <Stack gap={2} data-testid="chat-assistant-message" align="stretch">
      {message.tools?.length ? <ToolChips tools={message.tools} /> : null}
      {failed ? (
        <Box borderWidth="1px" borderColor="red.muted" bg="red.subtle" rounded="md" p={2} role="alert">
          <Text fontSize="sm" color="fg.error">
            {message.error === "cancelled" ? t("chat.cancelled") : message.error}
          </Text>
        </Box>
      ) : (
        <ChatMarkdown text={message.content} />
      )}
      {message.stopped ? (
        <Text fontSize="2xs" color="orange.fg">
          {t("chat.stopped", { reason: message.stopped })}
        </Text>
      ) : null}
      {message.proposals?.map((p) => (
        <ProposalCard key={p.id} proposal={p} threadId={threadId} onChange={onProposal} />
      ))}
      <Citations items={citedSources(message)} />
      {message.guardrails?.length ? (
        <HStack gap={1} wrap="wrap">
          <Box color="orange.fg">
            <LuShieldAlert />
          </Box>
          {message.guardrails.map((g) => (
            <Badge key={g} size="xs" colorPalette="orange" variant="subtle">
              {g}
            </Badge>
          ))}
        </HStack>
      ) : null}
      <HStack gap={1} justify="space-between" wrap="wrap">
        <UsageLine model={message.model} usage={message.usage} />
        <HStack gap={0.5}>
          {!failed ? (
            <IconButton
              size="2xs"
              variant="ghost"
              aria-label={t("chat.copy")}
              title={t("chat.copy")}
              onClick={() =>
                void copyText(message.content).then((ok) => {
                  setCopied(ok);
                  if (ok) {
                    toaster.create({ type: "success", title: t("chat.copied"), duration: 1500 });
                    setTimeout(() => setCopied(false), 1500);
                  }
                })
              }
              data-testid="chat-copy"
            >
              {copied ? <LuCircleCheck /> : <LuCopy />}
            </IconButton>
          ) : null}
          {isLast ? (
            <IconButton
              size="2xs"
              variant="ghost"
              aria-label={t("chat.regenerate")}
              title={t("chat.regenerate")}
              disabled={busy}
              onClick={onRegenerate}
              data-testid="chat-regenerate"
            >
              <LuRefreshCw />
            </IconButton>
          ) : null}
        </HStack>
      </HStack>
      {isLast && message.follow_ups?.length ? (
        <HStack gap={1} wrap="wrap" data-testid="chat-follow-ups">
          {message.follow_ups.map((q) => (
            <Button
              key={q}
              size="xs"
              variant="outline"
              rounded="full"
              disabled={busy}
              onClick={() => onFollowUp(q)}
              whiteSpace="normal"
              h="auto"
              py={1}
              textAlign="start"
            >
              {q}
            </Button>
          ))}
        </HStack>
      ) : null}
    </Stack>
  );
}
