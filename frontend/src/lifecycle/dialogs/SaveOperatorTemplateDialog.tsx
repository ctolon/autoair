import { Badge, Box, Button, Field, HStack, Input, NativeSelect, Stack, Table, Text, Textarea } from "@chakra-ui/react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { LuSave } from "react-icons/lu";

import {
  listTemplatesOptions,
  listTemplatesQueryKey,
  saveTemplateMutation,
} from "../../api/gen/@tanstack/react-query.gen";
import { errorMessage } from "../../api/setup";
import { type FieldMode, configurablePaths, getPath, makeBlueprint } from "../../canvas/operatorTemplates";
import { isTaskNode, type TaskSpec } from "../../lib/types";
import { IconField } from "../../panels/inspector/IconPicker";
import { useBuilder } from "../../store/builder";
import { toaster } from "../../ui/toaster";
import { DialogShell } from "../components/DialogShell";
import { useLifecycle } from "../store";

const slug = (s: string) =>
  s
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");

const MODES: FieldMode[] = ["default", "locked", "required"];

function preview(v: unknown): string {
  if (v === undefined || v === null) return "—";
  const text = typeof v === "string" ? v : JSON.stringify(v);
  return text.length > 60 ? `${text.slice(0, 57)}…` : text;
}

/**
 * Saves one configured task as an operator template (blueprint): every configured field is
 * kept as a default, locked (read-only, enforced by validation) or required (asked on insert).
 */
export default function SaveOperatorTemplateDialog({ nodeId }: { nodeId: string }) {
  const { t } = useTranslation("lifecycle");
  const qc = useQueryClient();
  const close = useLifecycle((s) => s.closeDialog);
  // Snapshot of the task when the dialog opens.
  const [spec] = useState<TaskSpec | null>(() => {
    const node = useBuilder.getState().nodes.find((n) => n.id === nodeId);
    return node && isTaskNode(node) ? structuredClone(node.data.spec) : null;
  });
  const paths = useMemo(() => (spec ? configurablePaths(spec) : []), [spec]);
  const existing = useQuery({ ...listTemplatesOptions(), retry: false });
  const [title, setTitle] = useState(() => spec?.label || spec?.task_id || "");
  const [id, setId] = useState<string | null>(null);
  const [description, setDescription] = useState("");
  const [ownerTeam, setOwnerTeam] = useState("");
  const [version, setVersion] = useState("");
  const [tags, setTags] = useState("");
  const [icon, setIcon] = useState<string | null>(() => spec?.icon ?? null);
  const [modes, setModes] = useState<Record<string, FieldMode>>({});
  const effectiveId = id ?? slug(title);
  const previous = existing.data?.find((tpl) => tpl.id === effectiveId);
  const clash = previous && previous.kind !== "operator";

  const save = useMutation({
    ...saveTemplateMutation(),
    onSuccess: async (tpl) => {
      await qc.invalidateQueries({ queryKey: listTemplatesQueryKey() });
      toaster.create({
        type: "success",
        title: t("operatorTemplates.saved", { title: tpl.title, version: tpl.version ?? "1" }),
      });
      close();
    },
    onError: (e) => toaster.create({ type: "error", title: t("templates.saveFailed"), description: errorMessage(e) }),
  });
  const valid = !!spec && !!title.trim() && /^[a-z0-9][a-z0-9_-]*$/.test(effectiveId) && !clash;

  return (
    <DialogShell
      title={t("operatorTemplates.saveTitle")}
      description={spec ? t("operatorTemplates.saveDescription", { task: spec.task_id }) : undefined}
      size="lg"
      footer={
        <>
          <Button size="sm" variant="ghost" onClick={close}>
            {t("common.cancel")}
          </Button>
          <Button
            size="sm"
            colorPalette="blue"
            disabled={!valid}
            loading={save.isPending}
            onClick={() =>
              spec &&
              save.mutate({
                body: {
                  id: effectiveId,
                  title: title.trim(),
                  description,
                  category: "operator",
                  tags: tags
                    .split(",")
                    .map((x) => x.trim())
                    .filter(Boolean),
                  kind: "operator",
                  owner_team: ownerTeam.trim() || null,
                  version: version.trim() || null,
                  icon,
                  blueprint: makeBlueprint(spec, modes),
                },
              })
            }
          >
            <LuSave /> {previous ? t("operatorTemplates.publishVersion") : t("common.save")}
          </Button>
        </>
      }
    >
      {!spec ? (
        <Text fontSize="sm" color="fg.warning">
          {t("operatorTemplates.noTask")}
        </Text>
      ) : (
        <Stack gap={3}>
          <HStack align="start" gap={3}>
            <Field.Root required flex="2">
              <Field.Label>{t("templates.fieldTitle")}</Field.Label>
              <Input size="sm" autoFocus value={title} onChange={(e) => setTitle(e.target.value)} />
            </Field.Root>
            <Field.Root flex="1" invalid={!/^[a-z0-9_-]*$/.test(effectiveId) || !!clash}>
              <Field.Label>{t("templates.fieldId")}</Field.Label>
              <Input
                size="sm"
                fontFamily="mono"
                value={effectiveId}
                onChange={(e) => setId(e.target.value)}
                aria-label={t("templates.fieldId")}
              />
              {clash ? <Field.ErrorText>{t("operatorTemplates.idTaken")}</Field.ErrorText> : null}
            </Field.Root>
          </HStack>
          {previous && !clash ? (
            <Text fontSize="xs" color="fg.muted">
              {t("operatorTemplates.newVersionHint", { version: previous.version ?? "1" })}
            </Text>
          ) : null}
          <Field.Root>
            <Field.Label>{t("templates.fieldDescription")}</Field.Label>
            <Textarea size="sm" rows={2} value={description} onChange={(e) => setDescription(e.target.value)} />
          </Field.Root>
          <HStack align="start" gap={3} wrap="wrap">
            <Field.Root flex="1" minW="140px">
              <Field.Label>{t("operatorTemplates.ownerTeam")}</Field.Label>
              <Input size="sm" value={ownerTeam} onChange={(e) => setOwnerTeam(e.target.value)} />
            </Field.Root>
            <Field.Root flex="1" minW="100px">
              <Field.Label>{t("operatorTemplates.version")}</Field.Label>
              <Input
                size="sm"
                placeholder={t("operatorTemplates.versionAuto")}
                value={version}
                onChange={(e) => setVersion(e.target.value)}
              />
            </Field.Root>
            <Field.Root flex="2" minW="160px">
              <Field.Label>{t("templates.fieldTags")}</Field.Label>
              <Input size="sm" value={tags} onChange={(e) => setTags(e.target.value)} />
            </Field.Root>
          </HStack>
          <IconField value={icon} effective={null} onChange={(v) => setIcon(v ?? null)} />

          <Stack gap={1}>
            <Text fontSize="sm" fontWeight="semibold">
              {t("operatorTemplates.fields")}
            </Text>
            <Text fontSize="xs" color="fg.muted">
              {t("operatorTemplates.fieldsHelp")}
            </Text>
            {paths.length ? (
              <Box borderWidth="1px" rounded="md" overflowX="auto">
                <Table.Root size="sm" aria-label={t("operatorTemplates.fields")}>
                  <Table.Body>
                    {paths.map((path) => {
                      const mode = modes[path] ?? "default";
                      return (
                        <Table.Row key={path} data-path={path}>
                          <Table.Cell fontFamily="mono" fontSize="xs" whiteSpace="nowrap">
                            {path}
                          </Table.Cell>
                          <Table.Cell fontFamily="mono" fontSize="xs" color="fg.muted" maxW="220px" truncate>
                            {preview(getPath(spec, path))}
                          </Table.Cell>
                          <Table.Cell w="140px">
                            <NativeSelect.Root size="xs">
                              <NativeSelect.Field
                                aria-label={t("operatorTemplates.modeOf", { field: path })}
                                value={mode}
                                onChange={(e) => setModes((m) => ({ ...m, [path]: e.target.value as FieldMode }))}
                              >
                                {MODES.map((m) => (
                                  <option key={m} value={m}>
                                    {t(`operatorTemplates.modes.${m}`)}
                                  </option>
                                ))}
                              </NativeSelect.Field>
                              <NativeSelect.Indicator />
                            </NativeSelect.Root>
                          </Table.Cell>
                        </Table.Row>
                      );
                    })}
                  </Table.Body>
                </Table.Root>
              </Box>
            ) : (
              <Badge alignSelf="start">{t("operatorTemplates.noFields")}</Badge>
            )}
          </Stack>
        </Stack>
      )}
    </DialogShell>
  );
}
