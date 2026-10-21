import { Stack } from "@chakra-ui/react";
import { useTranslation } from "react-i18next";

import {
  SOURCE_POLICIES,
  type SourcePolicyChoice,
  isHandWritten,
  policyApplies,
  policyOf,
  withPolicy,
} from "../lib/sourcePolicy";
import { useBuilder } from "../store/builder";
import { Hint, SelectField } from "./fields";

/**
 * The DAG's source policy (``source.policy``): how its Python file is written on deploy —
 * generated from the spec, or the existing hand-written file patched. A document change,
 * so it is undoable like any other DAG setting. With ``value``/``onChange`` it edits a
 * draft instead (the deploy dialog applies it only when the user deploys).
 */
export function SourcePolicyField({
  value,
  onChange,
}: {
  value?: SourcePolicyChoice;
  onChange?: (policy: SourcePolicyChoice) => void;
} = {}) {
  const { t } = useTranslation("inspector");
  const dag = useBuilder((s) => s.dag);
  const setDag = useBuilder((s) => s.setDag);
  if (dag.raw_code != null) return null; // raw code DAGs are deployed as written
  const applies = policyApplies(dag);
  const policy = value ?? policyOf(dag);
  const autoMeans = isHandWritten(dag) ? "preserve" : "generate";
  return (
    <Stack gap={1}>
      <SelectField
        help={t("dag.general.sourcePolicyHelp")}
        label={t("dag.general.sourcePolicy")}
        field="source.policy"
        value={policy}
        disabled={!applies}
        options={SOURCE_POLICIES.map((value) => ({
          value,
          label:
            value === "auto"
              ? t("dag.general.sourcePolicyAuto", { policy: t(`dag.general.sourcePolicyName.${autoMeans}`) })
              : t(`dag.general.sourcePolicyName.${value}`),
        }))}
        onChange={(v) => {
          const next = policyOf({ source: { policy: v } });
          if (onChange) onChange(next);
          else setDag({ source: withPolicy(dag, next) });
        }}
      />
      <Hint tone="muted">
        {applies
          ? t(`dag.general.sourcePolicyNote.${policy === "auto" ? autoMeans : policy}`)
          : t("dag.general.sourcePolicyNotApplicable")}
      </Hint>
    </Stack>
  );
}
