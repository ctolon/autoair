import { Box, Button, Field, HStack, IconButton, Stack } from "@chakra-ui/react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { LuBookOpen, LuCode, LuMaximize2 } from "react-icons/lu";

import { DialogShell } from "../../lifecycle/components/DialogShell";
import { docstringToMarkdown } from "../../lib/docstring";
import type { TaskFlowNode } from "../../lib/types";
import { MarkdownPreview } from "../../widgets/markdown";
import { CodeEditor } from "../../widgets/code";
import { FIELD_ATTR, SectionTitle } from "../../widgets/fields";
import { useUpdate } from "./shared";

/**
 * Class/decorator docstring rendered like an IDE hover (reST, Google, NumPy and Epytext →
 * Markdown with highlighted examples and a parameter list). "Source" shows the raw text.
 */
export function Docstring({ text, title }: { text: string | null | undefined; title: string }) {
  const { t } = useTranslation("inspector");
  const [raw, setRaw] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const markdown = useMemo(() => (text ? docstringToMarkdown(text) : ""), [text]);
  if (!text) return null;
  const body = (maxH: string | undefined) =>
    raw ? (
      <Box
        as="pre"
        fontSize="xs"
        fontFamily="mono"
        whiteSpace="pre-wrap"
        bg="bg.subtle"
        borderWidth="1px"
        rounded="md"
        p={2}
        maxH={maxH}
        overflowY="auto"
        tabIndex={0}
        aria-label={title}
      >
        {text}
      </Box>
    ) : (
      <Box borderWidth="1px" rounded="md" px={3} py={1} maxH={maxH} overflowY="auto" tabIndex={0} aria-label={title}>
        <MarkdownPreview text={markdown} />
      </Box>
    );
  return (
    <Box>
      <HStack justify="space-between" gap={1}>
        <SectionTitle>{title}</SectionTitle>
        <HStack gap={0}>
          <Button size="2xs" variant="ghost" aria-pressed={raw} onClick={() => setRaw((r) => !r)}>
            {raw ? <LuBookOpen /> : <LuCode />} {raw ? t("docs.rendered") : t("docs.source")}
          </Button>
          <IconButton
            size="2xs"
            variant="ghost"
            aria-label={t("docs.expand")}
            title={t("docs.expand")}
            onClick={() => setExpanded(true)}
          >
            <LuMaximize2 />
          </IconButton>
        </HStack>
      </HStack>
      {body("360px")}
      {expanded ? (
        <DialogShell title={title} size="xl" onClose={() => setExpanded(false)}>
          {body(undefined)}
        </DialogShell>
      ) : null}
    </Box>
  );
}

export function DocsTab({ node, docstring }: { node: TaskFlowNode; docstring?: string | null | undefined }) {
  const { t } = useTranslation("inspector");
  const update = useUpdate<"operator">(node.id);
  return (
    <Stack gap={2}>
      <Field.Root {...{ [FIELD_ATTR]: "doc_md" }}>
        <Field.Label fontSize="xs">{t("docs.docMd")}</Field.Label>
        <Box w="100%">
          <CodeEditor
            language="markdown"
            ariaLabel={t("docs.docMd")}
            expandTitle={`${t("docs.docMd")} — ${node.data.spec.task_id}`}
            minHeight="140px"
            maxHeight="400px"
            value={node.data.spec.doc_md ?? ""}
            onChange={(v) => update({ doc_md: v || null })}
          />
        </Box>
        <Field.HelperText fontSize="xs">{t("docs.docMdHelp")}</Field.HelperText>
      </Field.Root>
      <Docstring text={docstring} title={t("docs.docstring")} />
    </Stack>
  );
}
