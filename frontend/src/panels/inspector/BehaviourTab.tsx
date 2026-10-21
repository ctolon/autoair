import { Box, Stack } from "@chakra-ui/react";
import { useTranslation } from "react-i18next";

import type { TaskFlowNode } from "../../lib/types";
import { CallbacksEditor, TASK_CALLBACK_EVENTS } from "../../widgets/CallbacksEditor";
import { FIELD_ATTR, Hint, SectionTitle, SelectField, SwitchField } from "../../widgets/fields";
import { RetryPolicyEditor } from "./RetryPolicyEditor";
import { useFieldError, useUpdate } from "./shared";

/** Setup/teardown, enable state, notifier callbacks and the retry policy of a task. */
export function BehaviourTab({ node }: { node: TaskFlowNode }) {
  const { t } = useTranslation("inspector");
  const update = useUpdate<"operator">(node.id);
  const spec = node.data.spec;
  const disabledError = useFieldError(node.id, "disabled");
  return (
    <Stack gap={2}>
      <SwitchField
        label={t("behaviour.disabled")}
        help={t("behaviour.disabledHelp")}
        field="disabled"
        error={disabledError}
        checked={!!spec.disabled}
        onChange={(v) => update({ disabled: v })}
      />
      <SelectField
        label={t("behaviour.lineage")}
        help={t("behaviour.lineageHelp")}
        field="lineage"
        value={spec.lineage ?? "inherit"}
        options={[
          { value: "inherit", label: t("behaviour.lineageOptions.inherit") },
          { value: "enabled", label: t("behaviour.lineageOptions.enabled") },
          { value: "disabled", label: t("behaviour.lineageOptions.disabled") },
        ]}
        onChange={(v) => update({ lineage: v ?? "inherit" })}
      />

      <SectionTitle>{t("behaviour.setupTeardown")}</SectionTitle>
      <Hint>{t("behaviour.setupTeardownHelp")}</Hint>
      <SwitchField
        label={t("behaviour.isSetup")}
        help={t("behaviour.isSetupHelp")}
        field="is_setup"
        checked={!!spec.is_setup}
        onChange={(v) => update(v ? { is_setup: true, is_teardown: false } : { is_setup: false })}
      />
      <SwitchField
        label={t("behaviour.isTeardown")}
        help={t("behaviour.isTeardownHelp")}
        field="is_teardown"
        checked={!!spec.is_teardown}
        onChange={(v) => update(v ? { is_teardown: true, is_setup: false } : { is_teardown: false })}
      />
      {spec.is_teardown && spec.trigger_rule && spec.trigger_rule !== "all_done_setup_success" ? (
        <Hint tone="warning">{t("behaviour.teardownTriggerRule")}</Hint>
      ) : null}

      <SectionTitle>{t("behaviour.callbacks")}</SectionTitle>
      <CallbacksEditor
        value={spec.callbacks}
        events={TASK_CALLBACK_EVENTS}
        onChange={(callbacks) => update({ callbacks })}
      />

      <SectionTitle>{t("behaviour.retryPolicy")}</SectionTitle>
      <Box {...{ [FIELD_ATTR]: "retry_policy" }}>
        <RetryPolicyEditor value={spec.retry_policy} onChange={(retry_policy) => update({ retry_policy })} />
      </Box>
    </Stack>
  );
}
