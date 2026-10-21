import { Button } from "@chakra-ui/react";
import { useTranslation } from "react-i18next";
import { LuSparkles } from "react-icons/lu";

import { useAccess } from "../lib/access";
import { useMeta } from "../lifecycle/hooks";
import { aiUsable, useAiStatus } from "../lifecycle/lib/ai";
import { type TroubleshootTarget, useAiChat } from "./store";

/** "Analyze with AI" for a DAG run or task instance (Runs / Task tabs). */
export function AnalyzeWithAiButton({ target }: { target: TroubleshootTarget | null }) {
  const { t } = useTranslation("ai");
  const access = useAccess();
  const meta = useMeta();
  const status = useAiStatus();
  if (!access.feature("ai") || !access.can("ai:use")) return null;
  const usable = aiUsable(status, !!meta?.ai_enabled) && access.can("airflow:read");
  return (
    <Button
      size="xs"
      variant="outline"
      colorPalette="purple"
      disabled={!target || !usable}
      title={!target ? t("troubleshoot.selectRun") : !usable ? t("troubleshoot.unavailable") : t("troubleshoot.hint")}
      onClick={() => target && useAiChat.getState().openTroubleshoot(target)}
      data-testid="analyze-with-ai"
    >
      <LuSparkles /> {t("troubleshoot.button")}
    </Button>
  );
}
