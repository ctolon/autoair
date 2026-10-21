import { HStack, Stack } from "@chakra-ui/react";
import { useTranslation } from "react-i18next";

import type { TaskFlowNode } from "../../lib/types";
import { useBuilder } from "../../store/builder";
import { PoolPicker } from "../../widgets/AirflowPickers";
import { JsonField, NumberField, SectionTitle, SelectField, TextField } from "../../widgets/fields";
import { useFieldError, useUpdate } from "./shared";

type Spec = TaskFlowNode["data"]["spec"];
type TriggerRule = NonNullable<Spec["trigger_rule"]>;

const TRIGGER_RULES: TriggerRule[] = [
  "all_success",
  "all_failed",
  "all_done",
  "all_done_min_one_success",
  "all_done_setup_success",
  "all_skipped",
  "one_success",
  "one_done",
  "one_failed",
  "none_failed",
  "none_failed_min_one_success",
  "none_skipped",
  "always",
];

const WEIGHT_RULES = ["downstream", "upstream", "absolute"] as const;

/** Tri-state boolean select: inherit (null) / true / false. */
function TriState({
  label,
  help,
  value,
  inherit,
  field,
  onChange,
}: {
  label: string;
  help?: string;
  value: boolean | null | undefined;
  inherit: string;
  field: string;
  onChange: (v: boolean | null) => void;
}) {
  const { t } = useTranslation("inspector");
  return (
    <SelectField
      label={label}
      help={help}
      field={field}
      value={value === true ? "true" : value === false ? "false" : null}
      emptyLabel={inherit}
      options={[
        { value: "true", label: t("execution.yes") },
        { value: "false", label: t("execution.no") },
      ]}
      onChange={(v) => onChange(v === null ? null : v === "true")}
    />
  );
}

/** Scheduling / resources / retries of a task (BaseOperator arguments). */
export function ExecutionTab({ node }: { node: TaskFlowNode }) {
  const update = useUpdate<"operator">(node.id);
  const da = useBuilder((s) => s.dag.default_args);
  const retriesError = useFieldError(node.id, "retries");
  const ownerError = useFieldError(node.id, "owner");
  return (
    <ExecutionFields
      spec={node.data.spec}
      update={update}
      defaults={da}
      errors={{ retries: retriesError, owner: ownerError }}
    />
  );
}

/**
 * The execution settings form on its own (no canvas): the inspector's Execution tab and the
 * operator template editor. `defaults`: the DAG's default_args shown as placeholders.
 */
export function ExecutionFields({
  spec,
  update,
  defaults,
  errors = {},
}: {
  spec: Spec;
  update: ReturnType<typeof useUpdate<"operator">>;
  defaults?: object | null | undefined;
  errors?: Readonly<Record<string, string | undefined>>;
}) {
  const { t } = useTranslation("inspector");
  const da = (defaults ?? {}) as Record<string, unknown> & { pool?: string | null; queue?: string | null };
  const retriesError = errors.retries;
  const ownerError = errors.owner;
  const inherited = (v: unknown) =>
    v === undefined || v === null || v === ""
      ? t("execution.dagDefault")
      : t("execution.inherits", { value: String(v) });
  const rule = spec.trigger_rule ?? "all_success";

  return (
    <Stack gap={2}>
      <SectionTitle>{t("execution.identity")}</SectionTitle>
      <HStack align="start">
        <TextField
          label={t("execution.displayName")}
          field="task_display_name"
          value={spec.task_display_name}
          help={t("execution.displayNameHelp")}
          onChange={(v) => update({ task_display_name: v })}
        />
        <TextField
          help={t("execution.ownerHelp")}
          label={t("execution.owner")}
          field="owner"
          error={ownerError}
          value={spec.owner}
          placeholder={inherited(da.owner)}
          onChange={(v) => update({ owner: v })}
        />
      </HStack>

      <SectionTitle>{t("execution.dependencies")}</SectionTitle>
      <SelectField
        label={t("execution.triggerRule")}
        field="trigger_rule"
        value={spec.trigger_rule}
        options={TRIGGER_RULES.map((r) => ({ value: r, label: r }))}
        emptyLabel={t("execution.triggerRuleDefault")}
        help={t(`triggerRules.${rule}`)}
        onChange={(v) => update({ trigger_rule: v })}
      />
      <TriState
        label={t("execution.dependsOnPast")}
        help={t("execution.dependsOnPastHelp")}
        field="depends_on_past"
        value={spec.depends_on_past}
        inherit={da.depends_on_past == null ? t("execution.dagDefault") : inherited(da.depends_on_past)}
        onChange={(v) => update({ depends_on_past: v })}
      />
      <NumberField
        label={t("execution.maxActiveTis")}
        help={t("execution.maxActiveTisHelp")}
        field="max_active_tis_per_dagrun"
        min={1}
        value={spec.max_active_tis_per_dagrun}
        onChange={(v) => update({ max_active_tis_per_dagrun: v })}
      />

      <SectionTitle>{t("execution.retries")}</SectionTitle>
      <HStack align="start">
        <NumberField
          help={t("execution.retriesCountHelp")}
          label={t("execution.retriesCount")}
          field="retries"
          error={retriesError}
          min={0}
          value={spec.retries}
          placeholder={inherited(da.retries)}
          onChange={(v) => update({ retries: v })}
        />
        <NumberField
          dtype="timedelta (s)"
          help={t("execution.retryDelayHelp")}
          label={t("execution.retryDelay")}
          field="retry_delay_seconds"
          min={0}
          value={spec.retry_delay_seconds}
          placeholder={inherited(da.retry_delay_seconds)}
          onChange={(v) => update({ retry_delay_seconds: v })}
        />
      </HStack>
      <HStack align="start">
        <NumberField
          label={t("execution.backoff")}
          help={t("execution.backoffHelp")}
          field="retry_exponential_backoff"
          min={0}
          step={0.5}
          value={spec.retry_exponential_backoff}
          onChange={(v) => update({ retry_exponential_backoff: v })}
        />
        <NumberField
          dtype="timedelta (s)"
          help={t("execution.timeoutHelp")}
          label={t("execution.timeout")}
          field="execution_timeout_seconds"
          min={1}
          value={spec.execution_timeout_seconds}
          placeholder={inherited(da.execution_timeout_seconds)}
          onChange={(v) => update({ execution_timeout_seconds: v })}
        />
      </HStack>

      <SectionTitle>{t("execution.resources")}</SectionTitle>
      <HStack align="start">
        <PoolPicker
          help={t("execution.poolHelp")}
          label={t("execution.pool")}
          field="pool"
          value={spec.pool}
          placeholder={da.pool ?? undefined}
          onChange={(v) => update({ pool: v })}
        />
        <NumberField
          help={t("execution.poolSlotsHelp")}
          label={t("execution.poolSlots")}
          field="pool_slots"
          min={1}
          value={spec.pool_slots}
          placeholder="1"
          onChange={(v) => update({ pool_slots: v })}
        />
      </HStack>
      <HStack align="start">
        <TextField
          help={t("execution.queueHelp")}
          label={t("execution.queue")}
          field="queue"
          value={spec.queue}
          placeholder={da.queue ?? undefined}
          onChange={(v) => update({ queue: v })}
        />
        <NumberField
          help={t("execution.priorityWeightHelp")}
          label={t("execution.priorityWeight")}
          field="priority_weight"
          value={spec.priority_weight}
          placeholder="1"
          onChange={(v) => update({ priority_weight: v })}
        />
      </HStack>
      <SelectField
        help={t("execution.weightRuleHelp")}
        label={t("execution.weightRule")}
        field="weight_rule"
        value={spec.weight_rule}
        options={WEIGHT_RULES.map((r) => ({ value: r, label: t(`execution.weightRules.${r}`) }))}
        emptyLabel={t("execution.weightRuleDefault")}
        onChange={(v) => update({ weight_rule: v })}
      />
      <TextField
        label={t("execution.executor")}
        help={t("execution.executorHelp")}
        field="executor"
        mono
        value={spec.executor}
        placeholder="CeleryExecutor"
        onChange={(v) => update({ executor: v })}
      />
      <JsonField
        label={t("execution.executorConfig")}
        help={t("execution.executorConfigHelp")}
        field="executor_config"
        rows={3}
        value={spec.executor_config ?? undefined}
        validate={(v) => (v && typeof v === "object" && !Array.isArray(v) ? undefined : t("common.mustBeObject"))}
        onChange={(v) => update({ executor_config: (v as Record<string, unknown> | undefined) ?? null })}
      />
    </Stack>
  );
}
