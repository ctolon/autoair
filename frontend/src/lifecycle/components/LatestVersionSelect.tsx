import { Field, NativeSelect } from "@chakra-ui/react";
import { useTranslation } from "react-i18next";

/** `run_on_latest_version`: the DAG's/Airflow's default (null), the latest bundle version, or the run's own. */
export function LatestVersionSelect({
  value,
  onChange,
}: {
  value: boolean | null;
  onChange: (v: boolean | null) => void;
}) {
  const { t } = useTranslation("lifecycle");
  const current = value === null ? "default" : value ? "latest" : "original";
  return (
    <Field.Root>
      <Field.Label fontSize="sm">{t("runOps.latestVersion")}</Field.Label>
      <NativeSelect.Root size="sm">
        <NativeSelect.Field
          value={current}
          onChange={(e) => onChange(e.target.value === "default" ? null : e.target.value === "latest")}
        >
          {(["default", "latest", "original"] as const).map((o) => (
            <option key={o} value={o}>
              {t(`runOps.latestOptions.${o}`)}
            </option>
          ))}
        </NativeSelect.Field>
        <NativeSelect.Indicator />
      </NativeSelect.Root>
      <Field.HelperText fontSize="xs">{t("runOps.latestHelp")}</Field.HelperText>
    </Field.Root>
  );
}
