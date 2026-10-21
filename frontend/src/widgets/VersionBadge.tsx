import { Badge } from "@chakra-ui/react";
import { useTranslation } from "react-i18next";

import { useAirflowGate } from "./airflowMeta";

/**
 * "requires Airflow ≥ x" badge; orange when the (target) Airflow version is too old, unless
 * `neutral` (informational notes, e.g. an optional output format).
 */
export function VersionBadge({ min, neutral = false }: { min: string; neutral?: boolean }) {
  const { t } = useTranslation("inspector");
  const { supports, version } = useAirflowGate();
  const ok = neutral || supports(min);
  return (
    <Badge
      size="xs"
      variant={ok ? "outline" : "solid"}
      colorPalette={ok ? "gray" : "orange"}
      title={version ? t("common.currentAirflow", { version }) : undefined}
    >
      {t("common.requiresAirflow", { version: min })}
    </Badge>
  );
}
