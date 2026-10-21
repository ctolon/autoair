import { Box, Button, HStack, Skeleton, Spinner, Stack, Text } from "@chakra-ui/react";
import { useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { LuRefreshCw } from "react-icons/lu";

import { errorMessage } from "../../api/setup";
import {
  blueprintPreviewOptions,
  useBlueprintDetail,
  useBlueprintSummaries,
  useLivePreview,
  versionsOf,
} from "../../canvas/blueprints";
import type { JsonSchema } from "../../lib/formSchema";
import type { BlueprintFlowNode } from "../../lib/types";
import { effectiveTaskId } from "../../lib/xcom";
import { useBuilder } from "../../store/builder";
import { Hint, SelectField, SwitchField, TextField } from "../../widgets/fields";
import { SchemaForm } from "../../widgets/SchemaForm";
import { ParentGroupSelect } from "./OtherInspectors";
import { useFieldError, useUpdate } from "./shared";

const asConfig = (v: unknown): Record<string, unknown> =>
  v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : {};

/** Inspector of an Astronomer Blueprint step: identity, version, config form and task preview. */
export function BlueprintInspector({ node }: { node: BlueprintFlowNode }) {
  const { t } = useTranslation("inspector");
  const update = useUpdate<"blueprint">(node.id);
  const spec = node.data.spec;
  const nodes = useBuilder((s) => s.nodes);
  const stepError = useFieldError(node.id, "step_id");
  const configError = useFieldError(node.id, "config");
  const name = typeof spec.blueprint === "string" ? spec.blueprint : "";
  const version = typeof spec.version === "number" ? spec.version : 1;
  const summaries = useBlueprintSummaries();
  const detail = useBlueprintDetail(name, version);
  const preview = useLivePreview(spec, detail.data);
  const queryClient = useQueryClient();
  const config = asConfig(spec.config);
  const versions = versionsOf(summaries.data, name);
  if (!versions.includes(version)) versions.push(version);
  const classPath = typeof spec.class_path === "string" ? spec.class_path : "";
  const stepId = typeof spec.step_id === "string" ? spec.step_id : "";
  const module = classPath.split(".").slice(0, -1).join(".");
  const schema = detail.data?.config_schema as JsonSchema | null | undefined;
  const effective = effectiveTaskId(nodes, node.id);

  return (
    <Stack gap={3} data-testid="blueprint-inspector">
      <TextField
        label={t("blueprint.stepId")}
        field="step_id"
        mono
        required
        value={spec.step_id}
        error={stepError}
        help={effective !== spec.step_id ? t("node.effectiveId", { id: effective }) : t("blueprint.stepIdHelp")}
        onChange={(v) => update({ step_id: v ?? "" })}
      />
      <TextField
        help={t("node.labelHelp")}
        label={t("node.label")}
        field="label"
        value={spec.label}
        onChange={(v) => update({ label: v })}
      />
      <SelectField
        label={t("blueprint.version")}
        help={t("blueprint.versionHelp")}
        field="version"
        value={String(version)}
        options={versions.map((v) => ({ value: String(v), label: `v${v}` }))}
        onChange={(v) => {
          const next = Number(v);
          const target = summaries.data?.find((s) => s.name === name && s.version === next);
          if (!target) return;
          // Each version is its own class and config model.
          update({ version: next, class_path: target.class_path, config_class: target.config_class ?? null });
        }}
      />
      <ParentGroupSelect node={node} />
      <SwitchField
        label={t("blueprint.disabled")}
        help={t("blueprint.disabledHelp")}
        field="disabled"
        checked={!!spec.disabled}
        onChange={(v) => update({ disabled: v })}
      />
      <Box borderWidth="1px" rounded="md" p={2}>
        <Text fontSize="xs" fontWeight="semibold">
          {name} · v{version}
        </Text>
        <Text fontSize="xs" fontFamily="mono" color="fg.muted" truncate title={classPath}>
          {classPath}
        </Text>
        {detail.data?.docstring ? (
          <Text fontSize="xs" color="fg.muted" mt={1} whiteSpace="pre-wrap" lineClamp={6}>
            {detail.data.docstring}
          </Text>
        ) : null}
      </Box>
      <Hint>{t("blueprint.runtime", { module })}</Hint>

      <Stack gap={1}>
        <Text fontSize="sm" fontWeight="semibold">
          {t("blueprint.config")}
        </Text>
        {configError ? <Hint tone="error">{configError}</Hint> : null}
        {detail.isPending && detail.fetchStatus !== "idle" ? (
          <Skeleton h="16" />
        ) : detail.isError ? (
          <Hint tone="error">{t("blueprint.missing", { name, version, error: errorMessage(detail.error) })}</Hint>
        ) : schema ? (
          <SchemaForm schema={schema} value={config} validate onChange={(c) => update({ config: c })} />
        ) : (
          <Hint tone="warning">
            {t("blueprint.noSchema", { error: detail.data?.schema_error ?? t("blueprint.unknownReason") })}
          </Hint>
        )}
      </Stack>

      <Stack gap={1}>
        <HStack justify="space-between">
          <Text fontSize="sm" fontWeight="semibold">
            {t("blueprint.preview")}
          </Text>
          <Button
            size="2xs"
            variant="ghost"
            disabled={!schema || preview.isFetching}
            onClick={() =>
              void queryClient.invalidateQueries({ queryKey: blueprintPreviewOptions(spec).queryKey, exact: true })
            }
          >
            {preview.isFetching ? <Spinner size="xs" /> : <LuRefreshCw />} {t("blueprint.refresh")}
          </Button>
        </HStack>
        {preview.isError ? (
          <Hint tone="error">{errorMessage(preview.error)}</Hint>
        ) : preview.data?.error ? (
          <Hint tone="warning">{t("blueprint.previewFailed", { error: preview.data.error })}</Hint>
        ) : preview.data?.tasks?.length ? (
          <Stack gap={0} data-testid="blueprint-preview">
            {(preview.data.tasks ?? []).map((task, i) => (
              <Text key={`${i}:${task}`} fontSize="xs" fontFamily="mono" color="fg.muted" truncate>
                {preview.data?.kind === "task" ? task : `${stepId}.${task}`}
              </Text>
            ))}
          </Stack>
        ) : (
          <Hint>{t("blueprint.previewHelp")}</Hint>
        )}
      </Stack>
    </Stack>
  );
}
