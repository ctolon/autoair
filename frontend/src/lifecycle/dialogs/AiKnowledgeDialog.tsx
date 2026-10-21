import { Badge, Box, Button, Field, HStack, Input, Stack, Switch, Text, chakra } from "@chakra-ui/react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type ChangeEvent, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { LuFilePlus, LuRotateCcw, LuSave, LuUpload } from "react-icons/lu";

import {
  getAiKnowledgeOptions,
  getAiKnowledgeQueryKey,
  listAiKnowledgeQueryKey,
} from "../../api/gen/@tanstack/react-query.gen";
import { listAiKnowledge, putAiKnowledge, resetAiKnowledge, toggleAiKnowledge } from "../../api/gen/sdk.gen";
import type { KnowledgeContent, KnowledgeDoc } from "../../api/gen/types.gen";
import { errorMessage } from "../../api/setup";
import { toaster } from "../../ui/toaster";
import { AiCodeEditor } from "../components/AiCodeEditor";
import { ErrorBox, Pending } from "../components/AiFindings";
import { DialogShell } from "../components/DialogShell";

const ORIGIN_PALETTE: Record<KnowledgeDoc["origin"], string> = {
  builtin: "gray",
  extra: "purple",
  overridden: "orange",
  custom: "blue",
};
const DOC_ID = /^[a-z0-9][a-z0-9._-]{0,80}$/;
const MAX_UPLOAD = 512 * 1024;

/** An API error that keeps the HTTP status (the generated client only throws the body). */
type StatusError = { detail: unknown; status: number | null };

/** Whether a failed request was refused for lack of permission (exported for tests). */
export function isForbidden(err: unknown): boolean {
  return !!err && typeof err === "object" && (err as Partial<StatusError>).status === 403;
}

async function fetchKnowledgeList({ signal }: { signal: AbortSignal }) {
  const res = await listAiKnowledge({ signal, throwOnError: false });
  if (res.error !== undefined || !res.response?.ok || !res.data) {
    const body = res.error as { detail?: unknown } | undefined;
    const error: StatusError = {
      detail: body?.detail ?? body ?? res.response?.statusText ?? "request failed",
      status: res.response?.status ?? null,
    };
    throw error;
  }
  return res.data;
}

/** Admin: built-in knowledge documents, site overrides, custom docs; disable / reset. */
export function AiKnowledgeDialog() {
  const { t } = useTranslation("lifecycle");
  const qc = useQueryClient();
  const list = useQuery({ queryKey: listAiKnowledgeQueryKey(), queryFn: fetchKnowledgeList, retry: false });
  const [selected, setSelected] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const refresh = () => void qc.invalidateQueries({ queryKey: listAiKnowledgeQueryKey() });
  const pick = (id: string) => {
    setSelected(id);
    setCreating(false);
  };
  const toggle = useMutation({
    mutationFn: async ({ id, enabled }: { id: string; enabled: boolean }) =>
      (await toggleAiKnowledge({ path: { doc_id: id }, body: { enabled }, throwOnError: true })).data,
    onSuccess: refresh,
    onError: (err) => toaster.create({ type: "error", title: errorMessage(err) }),
  });
  const docs = list.data ?? [];

  return (
    <DialogShell title={t("ai.knowledge.title")} description={t("ai.knowledge.description")} size="cover">
      {list.isPending ? <Pending label={t("ai.knowledge.loading")} /> : null}
      {list.isError ? (
        isForbidden(list.error) ? (
          <Text fontSize="sm" color="fg.muted">
            {t("ai.knowledge.forbidden")}
          </Text>
        ) : (
          <ErrorBox error={list.error} />
        )
      ) : null}
      {list.data ? (
        <Stack direction={{ base: "column", lg: "row" }} gap={4} align="stretch" minH="60vh">
          <Stack gap={1} w={{ base: "100%", lg: "22rem" }} flexShrink={0} overflowY="auto" maxH={{ lg: "70vh" }}>
            <Button
              size="xs"
              variant="outline"
              alignSelf="start"
              onClick={() => {
                setCreating(true);
                setSelected(null);
              }}
            >
              <LuFilePlus /> {t("ai.knowledge.new")}
            </Button>
            {docs.map((doc) => (
              <HStack
                key={doc.id}
                gap={2}
                p={2}
                rounded="md"
                borderWidth="1px"
                borderColor={selected === doc.id ? "blue.400" : "border"}
                bg={selected === doc.id ? "blue.subtle" : undefined}
                cursor="pointer"
                role="button"
                tabIndex={0}
                aria-pressed={selected === doc.id}
                aria-label={doc.title}
                _focusVisible={{ outline: "2px solid", outlineColor: "blue.500", outlineOffset: "1px" }}
                onClick={() => pick(doc.id)}
                onKeyDown={(e) => {
                  if (e.target !== e.currentTarget) return; // the switch handles its own keys
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    pick(doc.id);
                  }
                }}
                data-testid={`kb-doc-${doc.id}`}
              >
                <Stack gap={0.5} flex="1" minW={0}>
                  <Text fontSize="sm" fontWeight="medium" truncate title={doc.title}>
                    {doc.title}
                  </Text>
                  <HStack gap={1} wrap="wrap">
                    <Text fontSize="2xs" fontFamily="mono" color="fg.muted" truncate>
                      {doc.id}
                    </Text>
                    <Badge size="xs" colorPalette={ORIGIN_PALETTE[doc.origin]}>
                      {t(`ai.knowledge.origin.${doc.origin}`)}
                    </Badge>
                    {doc.disabled_by ? (
                      <Badge size="xs" variant="outline">
                        {t(`ai.knowledge.disabledBy.${doc.disabled_by}`)}
                      </Badge>
                    ) : null}
                  </HStack>
                </Stack>
                <Switch.Root
                  size="sm"
                  checked={doc.enabled}
                  disabled={toggle.isPending}
                  onClick={(e) => e.stopPropagation()}
                  onCheckedChange={(e) => toggle.mutate({ id: doc.id, enabled: !!e.checked })}
                  aria-label={t("ai.knowledge.enabled", { id: doc.id })}
                >
                  <Switch.HiddenInput />
                  <Switch.Control />
                </Switch.Root>
              </HStack>
            ))}
          </Stack>
          <Box flex="1" minW={0}>
            {creating ? (
              <DocEditor
                key="new"
                doc={null}
                onSaved={(id) => {
                  refresh();
                  setCreating(false);
                  setSelected(id);
                }}
              />
            ) : selected ? (
              <DocEditor
                key={selected}
                doc={docs.find((d) => d.id === selected) ?? null}
                docId={selected}
                onSaved={refresh}
                onReset={(gone) => {
                  refresh();
                  if (gone) setSelected(null);
                }}
              />
            ) : (
              <Text fontSize="sm" color="fg.muted" p={4}>
                {t("ai.knowledge.pick")}
              </Text>
            )}
          </Box>
        </Stack>
      ) : null}
    </DialogShell>
  );
}

function DocEditor({
  doc,
  docId,
  onSaved,
  onReset,
}: {
  doc: KnowledgeDoc | null;
  docId?: string;
  onSaved: (id: string) => void;
  onReset?: (gone: boolean) => void;
}) {
  const { t } = useTranslation("lifecycle");
  const qc = useQueryClient();
  const isNew = !docId;
  const content = useQuery({ ...getAiKnowledgeOptions({ path: { doc_id: docId ?? "" } }), enabled: !isNew });
  const [newId, setNewId] = useState("");
  const [text, setText] = useState<string | null>(isNew ? t("ai.knowledge.newTemplate") : null);
  const [showDefault, setShowDefault] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);
  const value = text ?? content.data?.content ?? null;
  const id = isNew ? newId.trim().toLowerCase().replace(/\.md$/, "") : (docId ?? "");
  const idOk = DOC_ID.test(id);
  const save = useMutation({
    mutationFn: async () =>
      (await putAiKnowledge({ path: { doc_id: id }, body: { content: value ?? "" }, throwOnError: true })).data,
    onSuccess: () => {
      // The saved text is now the server's: update the cached content so the editor is clean.
      const saved = value ?? "";
      qc.setQueryData(getAiKnowledgeQueryKey({ path: { doc_id: id } }), (old: KnowledgeContent | undefined) => ({
        default_content: old?.default_content ?? null,
        ...old,
        id,
        content: saved,
      }));
      setText(null);
      void qc.invalidateQueries({ queryKey: getAiKnowledgeQueryKey({ path: { doc_id: id } }) });
      toaster.create({ type: "success", title: t("ai.knowledge.saved") });
      onSaved(id);
    },
  });
  const reset = useMutation({
    mutationFn: async () => (await resetAiKnowledge({ path: { doc_id: id }, throwOnError: true })).data,
    onSuccess: (result) => {
      toaster.create({ type: "success", title: t("ai.knowledge.resetDone") });
      setText(null);
      void content.refetch();
      onReset?.(!result);
    },
  });
  const upload = (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    if (file.size > MAX_UPLOAD) {
      toaster.create({ type: "error", title: t("ai.knowledge.tooLarge") });
      return;
    }
    void file.text().then((value) => {
      setText(value);
      if (isNew && !newId) setNewId(file.name.replace(/\.md$/i, "").toLowerCase());
    });
  };
  const dirty = text !== null && text !== (content.data?.content ?? "");
  const canReset = !isNew && !!doc && (doc.origin === "overridden" || doc.origin === "custom" || !!doc.disabled_by);

  return (
    <Stack gap={2} h="100%">
      <HStack gap={2} wrap="wrap" justify="space-between">
        {isNew ? (
          <Field.Root w="auto" minW="16rem" invalid={!!newId && !idOk}>
            <Input
              size="sm"
              fontFamily="mono"
              placeholder={t("ai.knowledge.idPlaceholder")}
              value={newId}
              onChange={(e) => setNewId(e.target.value)}
              aria-label={t("ai.knowledge.id")}
            />
            {newId && !idOk ? <Field.ErrorText>{t("ai.knowledge.badId")}</Field.ErrorText> : null}
          </Field.Root>
        ) : (
          <HStack gap={2} minW={0}>
            <Text fontWeight="semibold" fontFamily="mono" fontSize="sm" truncate>
              {docId}
            </Text>
            {doc ? (
              <Badge size="xs" colorPalette={ORIGIN_PALETTE[doc.origin]}>
                {t(`ai.knowledge.origin.${doc.origin}`)}
              </Badge>
            ) : null}
          </HStack>
        )}
        <HStack gap={1} wrap="wrap">
          <chakra.input
            ref={fileInput}
            type="file"
            accept=".md,.markdown,text/markdown,text/plain"
            display="none"
            onChange={upload}
          />
          <Button size="xs" variant="ghost" onClick={() => fileInput.current?.click()}>
            <LuUpload /> {t("ai.knowledge.upload")}
          </Button>
          {content.data?.default_content != null && doc?.origin === "overridden" ? (
            <Button size="xs" variant="ghost" onClick={() => setShowDefault((v) => !v)}>
              {showDefault ? t("ai.knowledge.hideDefault") : t("ai.knowledge.showDefault")}
            </Button>
          ) : null}
          {canReset ? (
            <Button
              size="xs"
              variant="outline"
              colorPalette="orange"
              loading={reset.isPending}
              onClick={() => reset.mutate()}
            >
              <LuRotateCcw /> {doc?.origin === "custom" ? t("ai.knowledge.delete") : t("ai.knowledge.reset")}
            </Button>
          ) : null}
          <Button
            size="xs"
            colorPalette="blue"
            disabled={!idOk || !(value ?? "").trim() || (!isNew && !dirty)}
            loading={save.isPending}
            onClick={() => save.mutate()}
          >
            <LuSave />{" "}
            {doc && doc.origin !== "custom" && doc.origin !== "overridden"
              ? t("ai.knowledge.saveOverride")
              : t("ai.knowledge.save")}
          </Button>
        </HStack>
      </HStack>
      {content.isError ? <ErrorBox error={content.error} /> : null}
      {save.isError ? <ErrorBox error={save.error} /> : null}
      {reset.isError ? <ErrorBox error={reset.error} /> : null}
      {!isNew && content.isPending ? <Pending label={t("ai.knowledge.loading")} /> : null}
      {value !== null ? (
        <AiCodeEditor
          value={value}
          language="markdown"
          onChange={setText}
          label={t("ai.knowledge.content")}
          height="52vh"
        />
      ) : null}
      {showDefault && content.data?.default_content != null ? (
        <Stack gap={1}>
          <Text fontSize="xs" color="fg.muted">
            {t("ai.knowledge.defaultVersion")}
          </Text>
          <AiCodeEditor
            value={content.data.default_content}
            language="markdown"
            readOnly
            label={t("ai.knowledge.defaultVersion")}
            height="30vh"
          />
        </Stack>
      ) : null}
    </Stack>
  );
}
