import { Box, HStack, IconButton, Text } from "@chakra-ui/react";
import { memo, useState } from "react";
import { useTranslation } from "react-i18next";
import { LuCheck, LuCopy, LuCornerDownLeft } from "react-icons/lu";

import { useBuilder } from "../store/builder";
import { toaster } from "../ui/toaster";
import { MarkdownPreview } from "../widgets/markdown";
import { CodeBlock } from "../widgets/markdown/Markdown";
import { insertTarget, type Segment, splitSegments } from "./chatText";

export async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

function useSelectedNode() {
  return useBuilder((s) => {
    const node = s.nodes.find((n) => n.id === s.selectedId);
    if (!node) return null;
    const spec = node.data.spec as { params?: Record<string, unknown> };
    return { id: node.id, type: node.type, params: spec.params ?? {} };
  });
}

/** A fenced code block with copy and "insert into the selected task" actions. */
function CodeSegment({ segment }: { segment: Extract<Segment, { kind: "code" }> }) {
  const { t } = useTranslation("ai");
  const [copied, setCopied] = useState(false);
  const node = useSelectedNode();
  const target = segment.open ? null : insertTarget(segment.language, node);
  const insert = () => {
    if (!target) return;
    const builder = useBuilder.getState();
    if (target.kind === "code") builder.updateSpec(target.nodeId, { code: segment.code });
    else {
      const current = node?.params ?? {};
      builder.updateSpec(target.nodeId, { params: { ...current, [target.param]: segment.code } });
    }
    toaster.create({ type: "success", title: t("chat.inserted", { node: target.nodeId }) });
  };
  return (
    <Box data-testid="chat-code" my={2}>
      <HStack justify="flex-end" gap={0.5} mb={-1}>
        {segment.language ? (
          <Text fontSize="2xs" color="fg.muted" px={1} fontFamily="mono" me="auto">
            {segment.language}
          </Text>
        ) : null}
        {target ? (
          <IconButton
            size="2xs"
            variant="subtle"
            aria-label={t("chat.insert", { node: target.nodeId })}
            title={t("chat.insert", { node: target.nodeId })}
            onClick={insert}
            data-testid="chat-code-insert"
          >
            <LuCornerDownLeft />
          </IconButton>
        ) : null}
        <IconButton
          size="2xs"
          variant="subtle"
          aria-label={t("chat.copyCode")}
          title={t("chat.copyCode")}
          onClick={() =>
            void copyText(segment.code).then((ok) => {
              setCopied(ok);
              if (ok) setTimeout(() => setCopied(false), 1500);
            })
          }
        >
          {copied ? <LuCheck /> : <LuCopy />}
        </IconButton>
      </HStack>
      <CodeBlock code={segment.code} language={segment.language ?? undefined} />
    </Box>
  );
}

/** Markdown of a chat message; code blocks get their own actions. */
export const ChatMarkdown = memo(function ChatMarkdown({ text }: { text: string }) {
  const segments = splitSegments(text);
  return (
    <Box minW={0} css={{ "& .autoair-markdown > :first-of-type": { mt: 0 } }}>
      {segments.map((s, i) =>
        s.kind === "text" ? <MarkdownPreview key={i} text={s.text} /> : <CodeSegment key={i} segment={s} />,
      )}
    </Box>
  );
});
