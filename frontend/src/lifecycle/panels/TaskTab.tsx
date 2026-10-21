import {
  Box,
  Button,
  Code,
  Field,
  HStack,
  Input,
  InputGroup,
  NativeSelect,
  Spinner,
  Stack,
  Tabs,
  Text,
} from "@chakra-ui/react";
import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { LuArrowDownToLine, LuSearch } from "react-icons/lu";

import { AnalyzeWithAiButton } from "../../ai/AnalyzeButton";
import { getTaskLogOptions, getTaskXcomOptions } from "../../api/gen/@tanstack/react-query.gen";
import { errorMessage } from "../../api/setup";
import { isTaskNode, nodeKey } from "../../lib/types";
import { useBuilder } from "../../store/builder";
import { JsonBlock } from "../components/JsonBlock";
import { StateBadge } from "../components/StateBadge";
import { useAirflowCapabilities } from "../capabilities";
import { isRunActive, latestLogTry, nodeTaskId } from "../lib/runOverlay";
import { useLifecycle } from "../store";
import { useSelectedRunOverlay } from "../useRunOverlaySync";

function LogViewer({ text, live }: { text: string; live: boolean }) {
  const { t } = useTranslation("lifecycle");
  const [q, setQ] = useState("");
  const [follow, setFollow] = useState(true);
  const ref = useRef<HTMLPreElement>(null);
  const lines = useMemo(() => text.split("\n"), [text]);
  const shown = useMemo(() => {
    if (!q) return lines.map((l, i) => [i, l] as const);
    const needle = q.toLowerCase();
    return lines.flatMap((l, i) => (l.toLowerCase().includes(needle) ? [[i, l] as const] : []));
  }, [lines, q]);
  useEffect(() => {
    if (follow && ref.current) ref.current.scrollTop = ref.current.scrollHeight;
  }, [shown, follow]);
  return (
    <Stack gap={1} h="100%">
      <HStack>
        <InputGroup startElement={<LuSearch />} maxW="320px">
          <Input
            size="xs"
            value={q}
            placeholder={t("task.searchLog")}
            aria-label={t("task.searchLog")}
            onChange={(e) => setQ(e.target.value)}
          />
        </InputGroup>
        {q ? (
          <Text fontSize="xs" color="fg.muted">
            {t("task.matches", { count: shown.length })}
          </Text>
        ) : null}
        <Box flex="1" />
        <Button
          size="xs"
          variant={follow ? "subtle" : "ghost"}
          aria-pressed={follow}
          onClick={() => setFollow((f) => !f)}
          title={t("task.follow")}
        >
          <LuArrowDownToLine /> {t("task.follow")}
        </Button>
        {live ? <Spinner size="xs" /> : null}
      </HStack>
      <Code
        ref={ref}
        as="pre"
        display="block"
        p={2}
        fontSize="xs"
        whiteSpace="pre-wrap"
        wordBreak="break-word"
        overflow="auto"
        flex="1"
        minH="160px"
        maxH="420px"
        role="log"
        aria-live={live ? "polite" : "off"}
        onScroll={(e) => {
          const el = e.currentTarget;
          const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
          if (atBottom !== follow) setFollow(atBottom);
        }}
      >
        {shown.map(([i, l]) => (
          <Box key={i} as="span" display="block" bg={q ? "yellow.subtle" : undefined}>
            {l || " "}
          </Box>
        ))}
      </Code>
    </Stack>
  );
}

/** Logs and XComs of the selected node in the selected run. */
export default function TaskTab() {
  const { t } = useTranslation("lifecycle");
  const dagId = useBuilder((s) => s.savedDagId);
  const runId = useLifecycle((s) => s.selectedRunId);
  const node = useBuilder((s) => s.nodes.find((n) => n.id === s.selectedId));
  const nodes = useBuilder((s) => s.nodes);
  const overlay = useSelectedRunOverlay();
  const taskId = useMemo(() => {
    if (!node || !isTaskNode(node)) return null;
    return nodeTaskId(useBuilder.getState().toSpec().nodes ?? [], node.id);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- recompute on graph changes
  }, [node, nodes]);
  const instances = useMemo(
    () => (overlay.data?.task_instances ?? []).filter((ti) => ti.task_id === taskId),
    [overlay.data, taskId],
  );
  const mapped = instances.filter((ti) => (ti.map_index ?? -1) >= 0);
  const [mapIndexSel, setMapIndex] = useState<number | null>(null);
  const mapIndex = mapIndexSel ?? (mapped.length ? (mapped[0]?.map_index ?? -1) : -1);
  const ti = instances.find((x) => (x.map_index ?? -1) === mapIndex);
  const caps = useAirflowCapabilities();
  const maxTry = latestLogTry(ti, !!caps.retry_try_is_next);
  const [trySel, setTry] = useState<number | null>(null);
  const tryNumber = Math.min(trySel ?? maxTry, maxTry);
  const live =
    isRunActive(overlay.data) &&
    !!ti &&
    ["running", "queued", "scheduled", "deferred", "up_for_retry"].includes(ti.state ?? "");
  // No state yet (or no instance at all): the task has not started in this run.
  const notStarted = !ti || !ti.state;
  const enabled = !!dagId && !!runId && !!taskId && !notStarted;
  const path = { dag_id: dagId ?? "_", run_id: runId ?? "_", task_id: taskId ?? "_" };
  const log = useQuery({
    ...getTaskLogOptions({ path, query: { try_number: tryNumber, map_index: mapIndex } }),
    enabled: enabled && !!ti,
    retry: false,
    refetchInterval: live ? 3000 : false,
  });
  const xcom = useQuery({
    ...getTaskXcomOptions({ path, query: { map_index: mapIndex } }),
    enabled: enabled && !!ti,
    retry: false,
    refetchInterval: live ? 5000 : false,
  });

  if (!dagId || !runId) {
    return (
      <Text fontSize="sm" color="fg.muted" p={3}>
        {t("task.selectRun")}
      </Text>
    );
  }
  if (!node || !taskId) {
    return (
      <Text fontSize="sm" color="fg.muted" p={3}>
        {t("task.selectNode")}
      </Text>
    );
  }
  const notFound = (e: unknown) => /not\s*found|404/i.test(errorMessage(e));
  const notStartedNote = (
    <Box p={4} borderWidth="1px" borderStyle="dashed" rounded="md" color="fg.muted" textAlign="center">
      <Text fontSize="sm">{t("task.notStarted", { task: nodeKey(node) })}</Text>
      <Text fontSize="xs" mt={1}>
        {t("task.notStartedHelp")}
      </Text>
    </Box>
  );
  return (
    <Stack p={3} gap={3} h="100%">
      <HStack gap={3} wrap="wrap" align="end">
        <Text fontFamily="mono" fontWeight="semibold" fontSize="sm">
          {nodeKey(node)}
        </Text>
        {ti ? <StateBadge state={ti.state} /> : null}
        {mapped.length ? (
          <Field.Root w="auto">
            <Field.Label fontSize="xs">{t("task.mapIndex")}</Field.Label>
            <NativeSelect.Root size="xs" w="200px">
              <NativeSelect.Field value={mapIndex} onChange={(e) => setMapIndex(Number(e.target.value))}>
                {mapped.map((m) => (
                  <option key={m.map_index} value={m.map_index}>
                    {m.rendered_map_index ? `${m.map_index} · ${m.rendered_map_index}` : m.map_index} ({m.state ?? "—"})
                  </option>
                ))}
              </NativeSelect.Field>
              <NativeSelect.Indicator />
            </NativeSelect.Root>
          </Field.Root>
        ) : null}
        {ti && !notStarted ? (
          <Field.Root w="auto">
            <Field.Label fontSize="xs">{t("task.try")}</Field.Label>
            <NativeSelect.Root size="xs" w="100px">
              <NativeSelect.Field value={tryNumber} onChange={(e) => setTry(Number(e.target.value))}>
                {Array.from({ length: maxTry }, (_, i) => i + 1).map((n) => (
                  <option key={n} value={n}>
                    {n}
                  </option>
                ))}
              </NativeSelect.Field>
              <NativeSelect.Indicator />
            </NativeSelect.Root>
          </Field.Root>
        ) : null}
        <AnalyzeWithAiButton target={{ dagId, runId, taskId, mapIndex, tryNumber: notStarted ? null : tryNumber }} />
      </HStack>
      {notStarted ? notStartedNote : null}
      {!notStarted ? (
        <Tabs.Root defaultValue="log" size="sm" variant="line" lazyMount flex="1">
          <Tabs.List>
            <Tabs.Trigger value="log">{t("task.logs")}</Tabs.Trigger>
            <Tabs.Trigger value="xcom">{t("task.xcom")}</Tabs.Trigger>
          </Tabs.List>
          <Tabs.Content value="log">
            {log.isPending ? <Spinner size="sm" /> : null}
            {log.isError ? (
              notFound(log.error) ? (
                notStartedNote
              ) : (
                <Text fontSize="sm" color="red.500">
                  {errorMessage(log.error)}
                </Text>
              )
            ) : null}
            {typeof log.data === "string" ? <LogViewer text={log.data} live={live} /> : null}
          </Tabs.Content>
          <Tabs.Content value="xcom">
            {xcom.isPending ? <Spinner size="sm" /> : null}
            {xcom.isError ? (
              notFound(xcom.error) ? (
                <Text fontSize="sm" color="fg.muted">
                  {t("task.noXcom")}
                </Text>
              ) : (
                <Text fontSize="sm" color="red.500">
                  {errorMessage(xcom.error)}
                </Text>
              )
            ) : null}
            {xcom.data && !xcom.data.length ? (
              <Text fontSize="sm" color="fg.muted">
                {t("task.noXcom")}
              </Text>
            ) : null}
            <Stack gap={2}>
              {(xcom.data ?? []).map((x) => (
                <Box key={`${x.key}-${x.map_index ?? -1}`}>
                  <Text fontSize="xs" fontWeight="semibold" fontFamily="mono">
                    {x.key}
                    {x.map_index !== undefined && x.map_index >= 0 ? ` [${x.map_index}]` : ""}
                  </Text>
                  <JsonBlock value={x.value} />
                </Box>
              ))}
            </Stack>
          </Tabs.Content>
        </Tabs.Root>
      ) : null}
    </Stack>
  );
}
