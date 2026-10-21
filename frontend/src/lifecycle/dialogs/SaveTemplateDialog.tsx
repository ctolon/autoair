import { Button, Field, Input, Stack, Text, Textarea } from "@chakra-ui/react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { LuSave } from "react-icons/lu";

import { listTemplatesQueryKey, saveTemplateMutation } from "../../api/gen/@tanstack/react-query.gen";
import { errorMessage } from "../../api/setup";
import { useBuilder } from "../../store/builder";
import { toaster } from "../../ui/toaster";
import { selectionFragment } from "../actions";
import { DialogShell } from "../components/DialogShell";
import { useLifecycle } from "../store";

const slug = (s: string) =>
  s
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");

/** Save the current DAG, or the selected nodes, as a reusable template. */
export default function SaveTemplateDialog({ mode }: { mode: "dag" | "fragment" }) {
  const { t } = useTranslation("lifecycle");
  const qc = useQueryClient();
  const close = useLifecycle((s) => s.closeDialog);
  const dag = useBuilder((s) => s.dag);
  // Snapshot what is saved when the dialog opens.
  const [fragment] = useState(() => (mode === "fragment" ? selectionFragment() : null));
  const [spec] = useState(() => (mode === "dag" ? useBuilder.getState().toSpec() : null));
  const [title, setTitle] = useState(mode === "dag" ? (dag.display_name ?? dag.dag_id) : "");
  const [id, setId] = useState<string | null>(null);
  const [description, setDescription] = useState(mode === "dag" ? (dag.description ?? "") : "");
  const [category, setCategory] = useState("");
  const [tags, setTags] = useState(mode === "dag" ? (dag.tags ?? []).join(", ") : "");
  const effectiveId = id ?? slug(title);
  const tagList = useMemo(
    () =>
      tags
        .split(",")
        .map((x) => x.trim())
        .filter(Boolean),
    [tags],
  );
  const save = useMutation({
    ...saveTemplateMutation(),
    onSuccess: async (tpl) => {
      await qc.invalidateQueries({ queryKey: listTemplatesQueryKey() });
      toaster.create({ type: "success", title: t("templates.saved", { title: tpl.title }) });
      close();
    },
    onError: (e) => toaster.create({ type: "error", title: t("templates.saveFailed"), description: errorMessage(e) }),
  });
  const empty = mode === "fragment" ? !fragment?.nodes.length : !spec;
  const valid = !empty && !!title.trim() && /^[a-z0-9_-]+$/.test(effectiveId);

  return (
    <DialogShell
      title={mode === "dag" ? t("menu.saveTemplate") : t("menu.saveFragment")}
      description={
        mode === "fragment"
          ? t("templates.fragmentSummary", { nodes: fragment?.nodes.length ?? 0, edges: fragment?.edges?.length ?? 0 })
          : undefined
      }
      size="md"
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
              save.mutate({
                body: {
                  id: effectiveId,
                  title: title.trim(),
                  description,
                  category: category.trim() || undefined,
                  tags: tagList,
                  kind: mode,
                  spec: mode === "dag" ? spec : null,
                  fragment: mode === "fragment" ? fragment : null,
                },
              })
            }
          >
            <LuSave /> {t("common.save")}
          </Button>
        </>
      }
    >
      {empty ? (
        <Text fontSize="sm" color="orange.500">
          {t("templates.noSelection")}
        </Text>
      ) : (
        <Stack gap={3}>
          <Field.Root required>
            <Field.Label>{t("templates.fieldTitle")}</Field.Label>
            <Input size="sm" autoFocus value={title} onChange={(e) => setTitle(e.target.value)} />
          </Field.Root>
          <Field.Root invalid={!/^[a-z0-9_-]*$/.test(effectiveId)}>
            <Field.Label>{t("templates.fieldId")}</Field.Label>
            <Input size="sm" fontFamily="mono" value={effectiveId} onChange={(e) => setId(e.target.value)} />
            <Field.HelperText>{t("templates.idHelp")}</Field.HelperText>
          </Field.Root>
          <Field.Root>
            <Field.Label>{t("templates.fieldDescription")}</Field.Label>
            <Textarea size="sm" rows={2} value={description} onChange={(e) => setDescription(e.target.value)} />
          </Field.Root>
          <Field.Root>
            <Field.Label>{t("templates.fieldCategory")}</Field.Label>
            <Input size="sm" value={category} onChange={(e) => setCategory(e.target.value)} />
          </Field.Root>
          <Field.Root>
            <Field.Label>{t("templates.fieldTags")}</Field.Label>
            <Input size="sm" value={tags} onChange={(e) => setTags(e.target.value)} />
          </Field.Root>
        </Stack>
      )}
    </DialogShell>
  );
}
