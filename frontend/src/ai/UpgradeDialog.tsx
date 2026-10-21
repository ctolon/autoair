import { Badge, Box, Button, Checkbox, HStack, RadioGroup, Spinner, Stack, Text } from "@chakra-ui/react";
import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { LuCircleArrowUp, LuCheck, LuPlay } from "react-icons/lu";

import { aiUpgradeDags } from "../api/gen/sdk.gen";
import type { DagUpgrade } from "../api/gen/types.gen";
import { errorMessage } from "../api/setup";
import { useAccess } from "../lib/access";
import { confirmDiscard, loadAsUnsaved, openDag } from "../lifecycle/actions";
import { UsageLine } from "../lifecycle/components/AiFindings";
import { DialogShell } from "../lifecycle/components/DialogShell";
import { requestModel, severityPalette, useAiStatus } from "../lifecycle/lib/ai";
import { useBuilder } from "../store/builder";
import { toaster } from "../ui/toaster";
import { SpecDiffReview } from "./SpecDiffReview";
import { useAiChat } from "./store";

function DagReport({ report }: { report: DagUpgrade }) {
  const { t } = useTranslation("ai");
  const access = useAccess();
  const apply = async () => {
    const spec = report.proposed_spec;
    if (!spec) return;
    const st = useBuilder.getState();
    if (st.savedDagId !== spec.dag_id) {
      const opened = await openDag(spec.dag_id);
      if (opened !== "opened") return;
    } else if (!confirmDiscard()) return;
    loadAsUnsaved(spec);
    toaster.create({ type: "success", title: t("upgrade.applied", { dagId: spec.dag_id }) });
  };
  return (
    <Box borderWidth="1px" rounded="md" p={3} data-testid="upgrade-dag">
      <HStack justify="space-between" mb={1} gap={2} wrap="wrap">
        <Text fontFamily="mono" fontWeight="semibold" fontSize="sm">
          {report.dag_id}
        </Text>
        <HStack gap={1}>
          {report.ai_used ? (
            <Badge size="xs" colorPalette="purple">
              AI
            </Badge>
          ) : null}
          <Badge size="xs" colorPalette={report.findings?.length ? "orange" : "green"}>
            {t("upgrade.findings", { count: report.findings?.length ?? 0 })}
          </Badge>
        </HStack>
      </HStack>
      <Text fontSize="sm" color="fg.muted" mb={2}>
        {report.summary}
      </Text>
      {report.error ? (
        <Text fontSize="xs" color="fg.error">
          {report.error}
        </Text>
      ) : null}
      <Stack gap={1} as="ul" listStyleType="none" m={0} p={0}>
        {(report.findings ?? []).map((f) => (
          <HStack as="li" key={f.id} gap={1.5} align="start" fontSize="xs">
            <Badge size="xs" colorPalette={severityPalette(f.severity)} flexShrink={0}>
              {f.severity}
            </Badge>
            <Box minW={0}>
              <Text fontWeight="medium">
                {f.title}
                {f.node_id ? (
                  <Text as="span" fontFamily="mono" color="fg.muted">
                    {" "}
                    @{f.node_id}
                  </Text>
                ) : null}
              </Text>
              {f.message ? <Text color="fg.muted">{f.message}</Text> : null}
            </Box>
            {f.patch?.length ? (
              <Badge size="xs" variant="outline" flexShrink={0}>
                {t("upgrade.fixable")}
              </Badge>
            ) : null}
          </HStack>
        ))}
      </Stack>
      {report.proposed_spec ? (
        <Box mt={2}>
          <SpecDiffReview
            base={report.base_spec}
            proposed={report.proposed_spec}
            problems={report.problems}
            codeBefore={report.code_before}
            codeAfter={report.code_after}
            testId="upgrade-diff"
          />
          <Button
            mt={2}
            size="xs"
            colorPalette="blue"
            disabled={!access.can("spec:write")}
            onClick={() => void apply()}
            data-testid="upgrade-apply"
          >
            <LuCheck /> {t("upgrade.apply")}
          </Button>
        </Box>
      ) : null}
    </Box>
  );
}

/** Airflow upgrade assistant: Airflow 2 idioms of the open DAG (or every saved DAG). */
export default function UpgradeDialog() {
  const { t } = useTranslation("ai");
  const status = useAiStatus();
  const [target, setTarget] = useState<"current" | "all">("current");
  const [useAi, setUseAi] = useState(true);
  const close = () => useAiChat.getState().setUpgradeOpen(false);
  const upgrade = useMutation({
    mutationFn: async () => {
      const model = requestModel(status);
      const body =
        target === "current"
          ? { spec: useBuilder.getState().toSpec(), use_ai: useAi, ...(model ? { model } : {}) }
          : { all: true, use_ai: false };
      return (await aiUpgradeDags({ body, throwOnError: true })).data;
    },
    onError: (e) => toaster.create({ type: "error", title: t("upgrade.failed"), description: errorMessage(e) }),
  });
  return (
    <DialogShell
      title={
        <HStack gap={2}>
          <LuCircleArrowUp />
          <span>{t("upgrade.title")}</span>
        </HStack>
      }
      description={t("upgrade.description")}
      size="xl"
      onClose={close}
      footer={
        <Button
          size="sm"
          colorPalette="blue"
          loading={upgrade.isPending}
          onClick={() => upgrade.mutate()}
          data-testid="upgrade-run"
        >
          <LuPlay /> {t("upgrade.run")}
        </Button>
      }
    >
      <Stack gap={4} data-testid="upgrade-dialog">
        <HStack gap={6} wrap="wrap">
          <RadioGroup.Root size="sm" value={target} onValueChange={(e) => setTarget(e.value as "current" | "all")}>
            <HStack gap={4}>
              {(["current", "all"] as const).map((v) => (
                <RadioGroup.Item key={v} value={v}>
                  <RadioGroup.ItemHiddenInput />
                  <RadioGroup.ItemIndicator />
                  <RadioGroup.ItemText>{t(`upgrade.target.${v}`)}</RadioGroup.ItemText>
                </RadioGroup.Item>
              ))}
            </HStack>
          </RadioGroup.Root>
          <Checkbox.Root
            size="sm"
            checked={useAi && target === "current"}
            disabled={target === "all"}
            onCheckedChange={(e) => setUseAi(!!e.checked)}
          >
            <Checkbox.HiddenInput />
            <Checkbox.Control />
            <Checkbox.Label>{t("upgrade.useAi")}</Checkbox.Label>
          </Checkbox.Root>
        </HStack>
        {upgrade.isPending ? (
          <HStack gap={2}>
            <Spinner size="sm" />
            <Text fontSize="sm">{t("upgrade.running")}</Text>
          </HStack>
        ) : null}
        {upgrade.data ? (
          <Stack gap={3}>
            {(upgrade.data.notes ?? []).map((n) => (
              <Text key={n} fontSize="xs" color="fg.muted">
                {n}
              </Text>
            ))}
            {upgrade.data.dags?.map((d) => (
              <DagReport key={d.dag_id} report={d} />
            ))}
            <UsageLine model={upgrade.data.model} usage={upgrade.data.usage} />
          </Stack>
        ) : null}
      </Stack>
    </DialogShell>
  );
}
