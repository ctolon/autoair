import { Badge, Box, Button, Collapsible, HStack, Spinner, Stack, Text } from "@chakra-ui/react";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { LuChevronDown, LuGripVertical, LuCornerDownLeft } from "react-icons/lu";

import { getTaskXcomOptions } from "../../api/gen/@tanstack/react-query.gen";
import type { XComInfo } from "../../api/gen/types.gen";
import { type RunOverlay, useBuilder } from "../../store/builder";
import { Hint } from "../../widgets/fields";
import { type UpstreamOutput, useUpstreamOutputs } from "../../widgets/upstream";
import { type XComPayload, applyToTarget, setXcomDragData, useInsertTarget } from "../../widgets/xcomDnd";

const preview = (v: unknown): string => {
  const s = typeof v === "string" ? JSON.stringify(v) : JSON.stringify(v ?? null);
  return s.length > 80 ? `${s.slice(0, 77)}…` : s;
};

function OutputItem({
  payload,
  label,
  sample,
  canInsert,
  onInsert,
}: {
  payload: XComPayload;
  label: string;
  sample?: { value: unknown } | undefined;
  canInsert: boolean;
  onInsert: () => void;
}) {
  const { t } = useTranslation("inspector");
  return (
    <HStack
      draggable
      onDragStart={(e) => setXcomDragData(e, payload)}
      gap={1}
      px={1}
      py={0.5}
      rounded="sm"
      cursor="grab"
      _hover={{ bg: "bg.muted" }}
      title={t("inputs.dragHint")}
    >
      <LuGripVertical aria-hidden size={12} />
      <Box flex="1" minW={0}>
        <Text fontSize="xs" fontFamily="mono" truncate>
          {label}
        </Text>
        {sample ? (
          <Text fontSize="2xs" fontFamily="mono" color="fg.muted" truncate title={JSON.stringify(sample.value)}>
            = {preview(sample.value)}
          </Text>
        ) : null}
      </Box>
      <Button
        size="2xs"
        variant="ghost"
        disabled={!canInsert}
        onClick={onInsert}
        aria-label={t("inputs.insertLabel", { output: label })}
      >
        <LuCornerDownLeft /> {t("inputs.insert")}
      </Button>
    </HStack>
  );
}

function UpstreamBlock({ up, run }: { up: UpstreamOutput; run: RunOverlay | null }) {
  const { t } = useTranslation("inspector");
  const target = useInsertTarget((s) => s.target);
  const xcom = useQuery({
    ...getTaskXcomOptions({
      path: { dag_id: run?.dagId ?? "", run_id: run?.runId ?? "", task_id: up.taskId },
    }),
    enabled: !!run,
    retry: false,
    staleTime: 60_000,
  });
  const values: XComInfo[] = xcom.data ?? [];
  const sampleFor = (key: string) => {
    const hit = values.find((x) => x.key === key && (x.map_index ?? -1) <= 0);
    return hit ? { value: hit.value } : undefined;
  };
  // Real XCom keys from the run complement keys detected from the code.
  const keys = [...new Set([...up.keys, ...values.map((v) => v.key).filter((k) => k !== "return_value")])];
  const insert = (p: XComPayload) => target && applyToTarget(target, p);
  return (
    <Box borderWidth="1px" rounded="md" p={1}>
      <HStack gap={1} px={1}>
        <Text fontSize="xs" fontWeight="semibold" truncate>
          {up.label}
        </Text>
        {up.taskId !== up.label ? (
          <Text fontSize="2xs" color="fg.muted" fontFamily="mono" truncate>
            {up.taskId}
          </Text>
        ) : null}
        {up.multipleOutputs ? (
          <Badge size="xs" variant="outline">
            multiple_outputs
          </Badge>
        ) : null}
        {xcom.isFetching ? <Spinner size="xs" /> : null}
      </HStack>
      <OutputItem
        payload={{ node: up.node.id, taskId: up.taskId }}
        label="return_value"
        sample={sampleFor("return_value")}
        canInsert={!!target}
        onInsert={() => insert({ node: up.node.id, taskId: up.taskId })}
      />
      {keys.map((k) => (
        <OutputItem
          key={k}
          payload={{ node: up.node.id, taskId: up.taskId, key: k }}
          label={`[${k}]`}
          sample={sampleFor(k)}
          canInsert={!!target}
          onInsert={() => insert({ node: up.node.id, taskId: up.taskId, key: k })}
        />
      ))}
      {run && xcom.isError ? <Hint>{t("inputs.noRunValues")}</Hint> : null}
    </Box>
  );
}

/** Upstream task outputs that can be dragged (or inserted) into parameter fields. */
export function InputsPanel({ nodeId }: { nodeId: string }) {
  const { t } = useTranslation("inspector");
  const upstream = useUpstreamOutputs(nodeId);
  const run = useBuilder((s) => s.runOverlay);
  const target = useInsertTarget((s) => s.target);
  return (
    <Collapsible.Root defaultOpen={upstream.length > 0}>
      <Collapsible.Trigger asChild>
        <Button size="xs" variant="ghost" px={1} w="100%" justifyContent="space-between">
          <HStack gap={1}>
            <LuChevronDown /> {t("inputs.title")}
            <Badge size="xs">{upstream.length}</Badge>
          </HStack>
          {run ? (
            <Text fontSize="2xs" color="fg.muted" truncate>
              {t("inputs.fromRun", { run: run.runId })}
            </Text>
          ) : null}
        </Button>
      </Collapsible.Trigger>
      <Collapsible.Content>
        <Stack gap={1} pt={1}>
          {upstream.length === 0 ? <Hint>{t("inputs.empty")}</Hint> : <Hint>{t("inputs.help")}</Hint>}
          {target ? (
            <Text fontSize="2xs" color="fg.muted">
              {t("inputs.target", { field: target.label })}
            </Text>
          ) : null}
          {upstream.map((u) => (
            <UpstreamBlock key={u.node.id} up={u} run={run} />
          ))}
        </Stack>
      </Collapsible.Content>
    </Collapsible.Root>
  );
}
