import { Button, HStack, Text } from "@chakra-ui/react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { LuSparkles } from "react-icons/lu";

import { aiDeployMessage } from "../api/gen/sdk.gen";
import type { DeployMessage } from "../api/gen/types.gen";
import { errorMessage } from "../api/setup";
import { useAccess } from "../lib/access";
import { requestModel, useAiStatus } from "../lifecycle/lib/ai";
import { useBuilder } from "../store/builder";
import { toaster } from "../ui/toaster";

/**
 * The Generate button: a commit subject and PR description summarising the spec diff against the
 * deployed version (`POST /ai/deploy-message`; works without a model from the diff alone).
 */
export function GenerateDeployMessage({
  environment,
  onGenerated,
}: {
  environment: string | null;
  onGenerated: (message: DeployMessage) => void;
}) {
  const { t } = useTranslation("ai");
  const access = useAccess();
  const status = useAiStatus();
  const [busy, setBusy] = useState(false);
  const [title, setTitle] = useState<string | null>(null);
  if (!access.feature("ai") || !access.can("ai:use")) return null;
  const generate = async () => {
    setBusy(true);
    try {
      const model = requestModel(status);
      const { data } = await aiDeployMessage({
        body: {
          spec: useBuilder.getState().toSpec(),
          environment,
          use_ai: status?.configured !== false,
          ...(model ? { model } : {}),
        },
        throwOnError: true,
      });
      setTitle(data.title);
      onGenerated(data);
    } catch (err) {
      toaster.create({ type: "error", title: t("deployMessage.failed"), description: errorMessage(err) });
    } finally {
      setBusy(false);
    }
  };
  return (
    <HStack gap={2} minW={0}>
      {title ? (
        <Text fontSize="2xs" color="fg.muted" truncate maxW="260px" title={title} data-testid="generated-title">
          {t("deployMessage.subject", { title })}
        </Text>
      ) : null}
      <Button
        size="2xs"
        variant="subtle"
        colorPalette="purple"
        loading={busy}
        onClick={() => void generate()}
        title={t("deployMessage.hint")}
        data-testid="generate-pr-description"
      >
        <LuSparkles /> {t("deployMessage.generate")}
      </Button>
    </HStack>
  );
}
