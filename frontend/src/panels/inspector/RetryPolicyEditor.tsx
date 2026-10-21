import { Box, Button, HStack, IconButton, Stack, Text } from "@chakra-ui/react";
import { useTranslation } from "react-i18next";
import { LuPlus, LuTrash2 } from "react-icons/lu";

import type { RetryPolicySpec, RetryRuleSpec } from "../../api/gen/types.gen";
import { useAirflowGate } from "../../widgets/airflowMeta";
import { Autocomplete } from "../../widgets/Autocomplete";
import { FIELD_ATTR, Hint, NumberField, SelectField, SwitchField, TextField } from "../../widgets/fields";
import { VersionBadge } from "../../widgets/VersionBadge";

/** Common exception classes offered as suggestions (any dotted path is accepted). */
const EXCEPTION_SUGGESTIONS = [
  "builtins.Exception",
  "builtins.ValueError",
  "builtins.KeyError",
  "builtins.TypeError",
  "builtins.TimeoutError",
  "builtins.ConnectionError",
  "builtins.PermissionError",
  "builtins.FileNotFoundError",
  "builtins.OSError",
  "requests.exceptions.HTTPError",
  "requests.exceptions.ConnectionError",
  "requests.exceptions.Timeout",
  "urllib3.exceptions.MaxRetryError",
  "botocore.exceptions.ClientError",
  "google.api_core.exceptions.ServiceUnavailable",
  "psycopg2.OperationalError",
  "sqlalchemy.exc.OperationalError",
  "airflow.exceptions.AirflowException",
  "airflow.exceptions.AirflowFailException",
  "airflow.exceptions.AirflowSkipException",
];

const ACTIONS = ["retry", "fail", "default"] as const;
const RETRY_POLICY_MIN_AIRFLOW = "3.3";

function RuleRow({
  rule,
  index,
  onChange,
  onRemove,
  disabled,
}: {
  rule: RetryRuleSpec;
  index: number;
  onChange: (r: RetryRuleSpec) => void;
  onRemove: () => void;
  disabled: boolean;
}) {
  const { t } = useTranslation("inspector");
  const action = rule.action ?? "retry";
  return (
    <Box borderWidth="1px" rounded="md" p={2} {...{ [FIELD_ATTR]: `retry_policy.rules.${index}` }}>
      <HStack justify="space-between" mb={1}>
        <Text fontSize="xs" fontWeight="semibold">
          {t("retryPolicy.rule", { n: index + 1 })}
        </Text>
        <IconButton
          aria-label={t("common.removeItem", { item: rule.exception || index + 1 })}
          size="2xs"
          variant="ghost"
          colorPalette="red"
          disabled={disabled}
          onClick={onRemove}
        >
          <LuTrash2 />
        </IconButton>
      </HStack>
      <Stack gap={2}>
        <Autocomplete
          help={t("retryPolicy.exceptionHelp")}
          label={t("retryPolicy.exception")}
          field={`retry_policy.rules.${index}.exception`}
          mono
          required
          disabled={disabled}
          value={rule.exception}
          options={EXCEPTION_SUGGESTIONS.map((e) => ({ value: e }))}
          placeholder="builtins.ValueError"
          onChange={(v) => onChange({ ...rule, exception: v ?? "" })}
        />
        <HStack align="start">
          <SelectField
            help={t("retryPolicy.actionHelp")}
            label={t("retryPolicy.action")}
            field={`retry_policy.rules.${index}.action`}
            disabled={disabled}
            value={action}
            options={ACTIONS.map((a) => ({ value: a, label: t(`retryPolicy.actions.${a}`) }))}
            onChange={(v) => onChange({ ...rule, action: v ?? "retry" })}
          />
          <NumberField
            dtype="timedelta (s)"
            help={t("retryPolicy.delayHelp")}
            label={t("retryPolicy.delay")}
            field={`retry_policy.rules.${index}.retry_delay_seconds`}
            disabled={disabled || action !== "retry"}
            min={0}
            value={rule.retry_delay_seconds}
            placeholder={t("retryPolicy.taskDelay")}
            onChange={(v) => onChange({ ...rule, retry_delay_seconds: v })}
          />
        </HStack>
        <TextField
          help={t("retryPolicy.reasonHelp")}
          label={t("retryPolicy.reason")}
          field={`retry_policy.rules.${index}.reason`}
          disabled={disabled}
          value={rule.reason}
          onChange={(v) => onChange({ ...rule, reason: v })}
        />
        <SwitchField
          help={t("retryPolicy.matchSubclassesHelp")}
          label={t("retryPolicy.matchSubclasses")}
          field={`retry_policy.rules.${index}.match_subclasses`}
          disabled={disabled}
          checked={rule.match_subclasses ?? true}
          onChange={(v) => onChange({ ...rule, match_subclasses: v })}
        />
      </Stack>
    </Box>
  );
}

/** Airflow ≥ 3.3 ExceptionRetryPolicy: per-exception retry / fail decisions. */
export function RetryPolicyEditor({
  value,
  onChange,
}: {
  value: RetryPolicySpec | null | undefined;
  onChange: (v: RetryPolicySpec | null) => void;
}) {
  const { t } = useTranslation("inspector");
  const { supports, version } = useAirflowGate();
  const supported = supports(RETRY_POLICY_MIN_AIRFLOW);
  // Keep editing possible for an existing policy (so it can be removed) even when unsupported.
  const disabled = !supported && !value;
  const rules = value?.rules ?? [];
  const setRules = (next: RetryRuleSpec[]) => onChange({ rules: next, default: value?.default ?? "default" });
  return (
    <Stack gap={2} {...{ [FIELD_ATTR]: "retry_policy" }}>
      <HStack justify="space-between">
        <Text fontSize="sm" fontWeight="semibold">
          {t("retryPolicy.title")}
        </Text>
        <VersionBadge min={RETRY_POLICY_MIN_AIRFLOW} />
      </HStack>
      <Hint>{t("retryPolicy.help")}</Hint>
      {!supported ? <Hint tone="warning">{t("retryPolicy.unsupported", { version: version ?? "?" })}</Hint> : null}
      {rules.map((r, i) => (
        <RuleRow
          key={i}
          rule={r}
          index={i}
          disabled={disabled}
          onChange={(nr) => setRules(rules.map((x, j) => (j === i ? nr : x)))}
          onRemove={() => {
            const next = rules.filter((_, j) => j !== i);
            if (next.length) setRules(next);
            else onChange(null);
          }}
        />
      ))}
      <HStack align="end">
        <Button
          size="xs"
          variant="outline"
          disabled={disabled}
          onClick={() => setRules([...rules, { exception: "", action: "retry", match_subclasses: true }])}
        >
          <LuPlus /> {t("retryPolicy.addRule")}
        </Button>
        {value ? (
          <Box flex="1">
            <SelectField
              help={t("retryPolicy.defaultHelp")}
              label={t("retryPolicy.default")}
              field="retry_policy.default"
              value={value.default ?? "default"}
              options={ACTIONS.map((a) => ({ value: a, label: t(`retryPolicy.actions.${a}`) }))}
              onChange={(v) => onChange({ ...value, default: v ?? "default" })}
            />
          </Box>
        ) : null}
      </HStack>
    </Stack>
  );
}
