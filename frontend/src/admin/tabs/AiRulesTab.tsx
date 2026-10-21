import { Badge, Box, Button, HStack, Input, Stack, Switch, Text, Textarea } from "@chakra-ui/react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { LuEye, LuPencil, LuPlus, LuSave, LuTrash2 } from "react-icons/lu";

import { listAiRulesOptions } from "../../api/gen/@tanstack/react-query.gen";
import { deleteAiRules, putAiRules } from "../../api/gen/sdk.gen";
import type { ProjectRules } from "../../api/gen/types.gen";
import { errorMessage } from "../../api/setup";
import { toaster } from "../../ui/toaster";
import { MarkdownPreview } from "../../widgets/markdown";
import { Empty, QueryState, Section } from "../common";

const SCOPE_RE = /^(global|team:[A-Za-z0-9][A-Za-z0-9_.-]{0,99})$/;

function RulesEditor({
  rules,
  canWrite,
  onSaved,
  isNew = false,
}: {
  rules: ProjectRules;
  canWrite: boolean;
  onSaved: () => void;
  isNew?: boolean;
}) {
  const { t, i18n } = useTranslation("admin");
  const [scope, setScope] = useState(rules.scope);
  const [content, setContent] = useState(rules.content ?? "");
  const [enabled, setEnabled] = useState(rules.enabled ?? true);
  const [preview, setPreview] = useState(false);
  const [busy, setBusy] = useState(false);
  const scopeValid = SCOPE_RE.test(scope);
  const save = async () => {
    setBusy(true);
    try {
      await putAiRules({ path: { scope }, body: { content, enabled }, throwOnError: true });
      toaster.create({ type: "success", title: t("aiRules.saved", { scope }) });
      onSaved();
    } catch (err) {
      toaster.create({ type: "error", title: t("error", { message: errorMessage(err) }) });
    } finally {
      setBusy(false);
    }
  };
  const remove = async () => {
    if (!window.confirm(t("aiRules.confirmDelete", { scope }))) return;
    setBusy(true);
    try {
      await deleteAiRules({ path: { scope }, throwOnError: true });
      onSaved();
    } catch (err) {
      toaster.create({ type: "error", title: t("error", { message: errorMessage(err) }) });
    } finally {
      setBusy(false);
    }
  };
  return (
    <Box borderWidth="1px" rounded="md" p={3} data-testid="ai-rules-editor">
      <HStack justify="space-between" mb={2} gap={2} wrap="wrap">
        {isNew ? (
          <Input
            size="sm"
            maxW="280px"
            value={scope}
            aria-label={t("aiRules.scope")}
            placeholder="team:data-eng"
            onChange={(e) => setScope(e.target.value.trim())}
          />
        ) : (
          <HStack gap={2}>
            <Badge colorPalette={scope === "global" ? "blue" : "purple"}>
              {scope === "global" ? t("aiRules.global") : scope}
            </Badge>
            {rules.updated_at ? (
              <Text fontSize="xs" color="fg.muted">
                {t("aiRules.updated", {
                  user: rules.updated_by ?? "—",
                  date: new Date(rules.updated_at).toLocaleString(i18n.language),
                })}
              </Text>
            ) : null}
          </HStack>
        )}
        <HStack gap={2}>
          <Switch.Root size="sm" checked={enabled} disabled={!canWrite} onCheckedChange={(e) => setEnabled(e.checked)}>
            <Switch.HiddenInput />
            <Switch.Control />
            <Switch.Label>{t("aiRules.enabled")}</Switch.Label>
          </Switch.Root>
          <Button size="xs" variant="ghost" onClick={() => setPreview((v) => !v)} aria-pressed={preview}>
            {preview ? <LuPencil /> : <LuEye />} {preview ? t("aiRules.edit") : t("aiRules.preview")}
          </Button>
        </HStack>
      </HStack>
      {preview ? (
        <Box borderWidth="1px" rounded="md" p={3} minH="120px" bg="bg.subtle">
          <MarkdownPreview text={content || "—"} />
        </Box>
      ) : (
        <Textarea
          size="sm"
          rows={8}
          fontFamily="mono"
          value={content}
          readOnly={!canWrite}
          placeholder={t("aiRules.placeholder")}
          aria-label={t("aiRules.content")}
          onChange={(e) => setContent(e.target.value)}
        />
      )}
      {canWrite ? (
        <HStack mt={2} gap={2}>
          <Button size="xs" colorPalette="blue" loading={busy} disabled={!scopeValid} onClick={() => void save()}>
            <LuSave /> {t("aiRules.save")}
          </Button>
          {!isNew ? (
            <Button size="xs" variant="ghost" colorPalette="red" loading={busy} onClick={() => void remove()}>
              <LuTrash2 /> {t("aiRules.delete")}
            </Button>
          ) : null}
          {isNew && scope && !scopeValid ? (
            <Text fontSize="xs" color="fg.error">
              {t("aiRules.badScope")}
            </Text>
          ) : null}
        </HStack>
      ) : null}
    </Box>
  );
}

/** Project AI rules: Markdown injected into every authoring/writing prompt (global, per team). */
export function AiRulesTab({ canWrite }: { canWrite: boolean }) {
  const { t } = useTranslation("admin");
  const client = useQueryClient();
  const q = useQuery(listAiRulesOptions());
  const [adding, setAdding] = useState(false);
  const rules = q.data ?? [];
  const refresh = () => {
    setAdding(false);
    void client.invalidateQueries({ queryKey: listAiRulesOptions().queryKey });
  };
  const hasGlobal = rules.some((r) => r.scope === "global");
  return (
    <Section
      help={t("aiRules.help")}
      actions={
        canWrite ? (
          <Button size="xs" variant="outline" onClick={() => setAdding(true)} disabled={adding}>
            <LuPlus /> {t("aiRules.add")}
          </Button>
        ) : null
      }
    >
      <QueryState isPending={q.isPending} error={q.error}>
        <Stack gap={3} data-testid="admin-ai-rules">
          {adding ? (
            <RulesEditor rules={{ scope: hasGlobal ? "team:" : "global" }} canWrite isNew onSaved={refresh} />
          ) : null}
          {rules.map((r) => (
            <RulesEditor key={`${r.scope}|${r.updated_at ?? ""}`} rules={r} canWrite={canWrite} onSaved={refresh} />
          ))}
          {!rules.length && !adding ? <Empty /> : null}
        </Stack>
      </QueryState>
    </Section>
  );
}
