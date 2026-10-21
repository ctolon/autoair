import {
  Alert,
  Badge,
  Box,
  Button,
  Field,
  HStack,
  NativeSelect,
  Spinner,
  Stack,
  Table,
  Tabs,
  Text,
} from "@chakra-ui/react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { LuFlaskConical, LuPlay, LuPlus, LuX } from "react-icons/lu";

import { testDagMutation, testTaskMutation } from "../../api/gen/@tanstack/react-query.gen";
import { getTaskXcom } from "../../api/gen/sdk.gen";
import type { DagTestResult, TaskTestResult, TestDagRequest } from "../../api/gen/types.gen";
import { errorMessage } from "../../api/setup";
import { upstreamTasks } from "../../canvas/graph";
import { isTaskNode, nodeKey } from "../../lib/types";
import { useBuilder } from "../../store/builder";
import { toaster } from "../../ui/toaster";
import { JsonField } from "../../widgets/fields";
import { DialogShell } from "../components/DialogShell";
import { JsonBlock } from "../components/JsonBlock";
import { CodeView } from "../components/lazy";
import { formatDuration } from "../lib/dates";
import { paramDefaults } from "../lib/params";
import { nodeTaskId } from "../lib/runOverlay";
import { mockKeys } from "../lib/xcomRefs";

const STATE_COLOR: Record<string, string> = {
  success: "green",
  failed: "red",
  error: "red",
  import_error: "red",
  timeout: "red",
  upstream_failed: "orange",
  skipped: "gray",
  deferred: "purple",
};
const isObj = (v: unknown): v is Record<string, unknown> => !!v && typeof v === "object" && !Array.isArray(v);

function StateBadge({ state }: { state: string }) {
  return <Badge colorPalette={STATE_COLOR[state] ?? "blue"}>{state}</Badge>;
}

function LogView({ log }: { log: string | undefined }) {
  const { t } = useTranslation("lifecycle");
  if (!log) return null;
  return (
    <Box>
      <Text fontSize="xs" fontWeight="semibold" mb={1}>
        {t("test.log")}
      </Text>
      <CodeView value={log} language="text" readOnly height="240px" label={t("test.log")} wrap />
    </Box>
  );
}

function TaskResult({ result }: { result: TaskTestResult }) {
  const { t } = useTranslation("lifecycle");
  return (
    <Stack gap={3} aria-live="polite">
      <HStack gap={3} wrap="wrap">
        <StateBadge state={result.state} />
        <Text fontSize="sm">{t("test.duration", { value: formatDuration(result.duration) })}</Text>
        {result.mapped ? <Badge>{t("test.mapped")}</Badge> : null}
      </HStack>
      {result.error ? (
        <Alert.Root status="error" size="sm">
          <Alert.Indicator />
          <Alert.Description fontSize="xs" whiteSpace="pre-wrap">
            {result.error}
          </Alert.Description>
        </Alert.Root>
      ) : null}
      {result.return_value !== undefined || result.return_value_repr ? (
        <Box>
          <Text fontSize="xs" fontWeight="semibold" mb={1}>
            {t("test.returnValue")}
          </Text>
          <JsonBlock value={result.return_value !== undefined ? result.return_value : result.return_value_repr} />
        </Box>
      ) : null}
      {result.xcom_pushes && Object.keys(result.xcom_pushes).length ? (
        <Box>
          <Text fontSize="xs" fontWeight="semibold" mb={1}>
            {t("test.xcomPushes")}
          </Text>
          <JsonBlock value={result.xcom_pushes} />
        </Box>
      ) : null}
      {result.skipped_downstream?.length ? (
        <Text fontSize="xs">{t("test.skippedDownstream", { list: result.skipped_downstream.join(", ") })}</Text>
      ) : null}
      <LogView log={result.log} />
    </Stack>
  );
}

function DagResult({ result }: { result: DagTestResult }) {
  const { t } = useTranslation("lifecycle");
  return (
    <Stack gap={3} aria-live="polite">
      <HStack gap={3}>
        <StateBadge state={result.state} />
        <Text fontSize="sm">{t("test.duration", { value: formatDuration(result.duration) })}</Text>
        {result.mode ? <Badge variant="outline">{result.mode}</Badge> : null}
      </HStack>
      {result.error ? (
        <Text fontSize="xs" color="red.500" whiteSpace="pre-wrap">
          {result.error}
        </Text>
      ) : null}
      {result.tasks?.length ? (
        <Table.Root size="sm">
          <Table.Header>
            <Table.Row>
              <Table.ColumnHeader>{t("test.task")}</Table.ColumnHeader>
              <Table.ColumnHeader>{t("test.state")}</Table.ColumnHeader>
              <Table.ColumnHeader>{t("test.durationCol")}</Table.ColumnHeader>
              <Table.ColumnHeader>{t("test.returnValue")}</Table.ColumnHeader>
            </Table.Row>
          </Table.Header>
          <Table.Body>
            {result.tasks.map((task) => (
              <Table.Row key={task.task_id}>
                <Table.Cell fontFamily="mono" fontSize="xs">
                  {task.task_id}
                </Table.Cell>
                <Table.Cell>
                  <StateBadge state={task.state} />
                </Table.Cell>
                <Table.Cell fontSize="xs">{formatDuration(task.duration)}</Table.Cell>
                <Table.Cell fontSize="xs" fontFamily="mono" maxW="240px" truncate title={task.error ?? undefined}>
                  {task.error ?? task.return_value_repr ?? ""}
                </Table.Cell>
              </Table.Row>
            ))}
          </Table.Body>
        </Table.Root>
      ) : null}
      <LogView log={result.log} />
    </Stack>
  );
}

function TaskTab({ initialNodeId, conf }: { initialNodeId: string | null; conf: Record<string, unknown> }) {
  const { t } = useTranslation("lifecycle");
  const nodes = useBuilder((s) => s.nodes);
  const edges = useBuilder((s) => s.edges);
  const runOverlay = useBuilder((s) => s.runOverlay);
  const tasks = useMemo(() => nodes.filter(isTaskNode), [nodes]);
  const [nodeId, setNodeId] = useState<string | null>(
    initialNodeId && tasks.some((n) => n.id === initialNodeId) ? initialNodeId : (tasks[0]?.id ?? null),
  );
  const node = tasks.find((n) => n.id === nodeId);
  const required = useMemo(() => (node ? mockKeys(node.data.spec) : []), [node]);
  const upstream = useMemo(() => (node ? upstreamTasks(nodes, edges, node.id) : []), [node, nodes, edges]);
  const [mocks, setMocks] = useState<Record<string, unknown>>({});
  const [extra, setExtra] = useState<string[]>([]);
  const keys = [...new Set([...required, ...extra])];
  const label = (key: string) => {
    const [id, xkey] = key.split(":");
    const n = nodes.find((x) => x.id === id);
    return `${n ? nodeKey(n) : id}${xkey ? ` · ${xkey}` : ""}`;
  };

  // Prefill mock values from the XComs of the run painted on the canvas.
  const prefill = useQuery({
    queryKey: ["lifecycle", "xcom-prefill", runOverlay?.dagId, runOverlay?.runId, required],
    enabled: !!runOverlay && required.length > 0,
    staleTime: 60_000,
    queryFn: async ({ signal }) => {
      if (!runOverlay) return {};
      const specNodes = useBuilder.getState().toSpec().nodes ?? [];
      const pairs = await Promise.all(
        required.map(async (key) => {
          const [id = "", xkey = "return_value"] = key.split(":");
          const taskId = nodeTaskId(specNodes, id);
          if (!taskId) return null;
          const res = await getTaskXcom({
            path: { dag_id: runOverlay.dagId, run_id: runOverlay.runId, task_id: taskId },
            signal,
            throwOnError: false,
          });
          const hit = (res.data ?? []).filter((x) => x.key === xkey);
          if (!hit.length) return null;
          return [key, hit.length > 1 ? hit.map((x) => x.value) : hit[0]?.value] as const;
        }),
      );
      return Object.fromEntries(pairs.filter((p): p is readonly [string, unknown] => p !== null));
    },
  });
  const valueOf = (key: string) => (key in mocks ? mocks[key] : prefill.data?.[key]);

  const run = useMutation({
    ...testTaskMutation(),
    onError: (e) => toaster.create({ type: "error", title: t("test.failed"), description: errorMessage(e) }),
  });

  if (!tasks.length) return <Text fontSize="sm">{t("test.noTasks")}</Text>;
  return (
    <Stack gap={4}>
      <Field.Root>
        <Field.Label>{t("test.task")}</Field.Label>
        <NativeSelect.Root size="sm">
          <NativeSelect.Field
            value={nodeId ?? ""}
            onChange={(e) => {
              setNodeId(e.target.value);
              run.reset();
              setExtra([]);
            }}
          >
            {tasks.map((n) => (
              <option key={n.id} value={n.id}>
                {nodeKey(n)}
              </option>
            ))}
          </NativeSelect.Field>
          <NativeSelect.Indicator />
        </NativeSelect.Root>
      </Field.Root>

      <Stack gap={2}>
        <HStack>
          <Text fontWeight="semibold" fontSize="sm" flex="1">
            {t("test.mocks")}
          </Text>
          {prefill.isFetching ? <Spinner size="xs" /> : null}
          {runOverlay ? (
            <Text fontSize="xs" color="fg.muted">
              {t("test.prefilledFrom", { runId: runOverlay.runId })}
            </Text>
          ) : null}
        </HStack>
        {!keys.length ? (
          <Text fontSize="xs" color="fg.muted">
            {t("test.noMocks")}
          </Text>
        ) : null}
        {keys.map((key) => (
          <HStack key={key} align="start">
            <Box flex="1">
              <JsonField
                label={label(key)}
                rows={2}
                value={valueOf(key)}
                onChange={(v) => setMocks((m) => ({ ...m, [key]: v }))}
              />
            </Box>
            {!required.includes(key) ? (
              <Button
                size="xs"
                variant="ghost"
                mt={6}
                aria-label={t("common.remove")}
                onClick={() => setExtra((x) => x.filter((k) => k !== key))}
              >
                <LuX />
              </Button>
            ) : null}
          </HStack>
        ))}
        {upstream.filter((u) => !keys.includes(u.id)).length ? (
          <HStack>
            <NativeSelect.Root size="xs" maxW="260px">
              <NativeSelect.Field
                aria-label={t("test.addMock")}
                value=""
                onChange={(e) => e.target.value && setExtra((x) => [...x, e.target.value])}
              >
                <option value="">{t("test.addMock")}</option>
                {upstream
                  .filter((u) => !keys.includes(u.id))
                  .map((u) => (
                    <option key={u.id} value={u.id}>
                      {nodeKey(u)}
                    </option>
                  ))}
              </NativeSelect.Field>
              <NativeSelect.Indicator />
            </NativeSelect.Root>
            <LuPlus aria-hidden />
          </HStack>
        ) : null}
      </Stack>

      <HStack>
        <Button
          size="sm"
          colorPalette="blue"
          loading={run.isPending}
          disabled={!node}
          onClick={() =>
            node &&
            run.mutate({
              body: {
                spec: useBuilder.getState().toSpec(),
                task_node_id: node.id,
                conf,
                mock_xcoms: Object.fromEntries(
                  keys.map((k) => [k, valueOf(k)] as const).filter(([, v]) => v !== undefined),
                ),
              },
            })
          }
        >
          <LuPlay /> {t("test.runTask")}
        </Button>
        {run.isPending ? (
          <Text fontSize="xs" color="fg.muted">
            {t("test.running")}
          </Text>
        ) : null}
      </HStack>
      {run.data ? <TaskResult result={run.data} /> : null}
    </Stack>
  );
}

function DagTab({ conf }: { conf: Record<string, unknown> }) {
  const { t } = useTranslation("lifecycle");
  const [mode, setMode] = useState<NonNullable<TestDagRequest["mode"]>>("memory");
  const run = useMutation({
    ...testDagMutation(),
    onError: (e) => toaster.create({ type: "error", title: t("test.failed"), description: errorMessage(e) }),
  });
  return (
    <Stack gap={4}>
      <Field.Root>
        <Field.Label>{t("test.mode")}</Field.Label>
        <NativeSelect.Root size="sm" maxW="320px">
          <NativeSelect.Field value={mode} onChange={(e) => setMode(e.target.value as typeof mode)}>
            {(["memory", "db", "auto"] as const).map((m) => (
              <option key={m} value={m}>
                {t(`test.modes.${m}`)}
              </option>
            ))}
          </NativeSelect.Field>
          <NativeSelect.Indicator />
        </NativeSelect.Root>
      </Field.Root>
      <HStack>
        <Button
          size="sm"
          colorPalette="blue"
          loading={run.isPending}
          onClick={() => run.mutate({ body: { spec: useBuilder.getState().toSpec(), conf, mode } })}
        >
          <LuFlaskConical /> {t("test.runDag")}
        </Button>
      </HStack>
      {run.data ? <DagResult result={run.data} /> : null}
    </Stack>
  );
}

/** Run a single task (with mocked upstream XComs) or the whole DAG in a sandbox. */
export default function TestTaskDialog({ nodeId, whole = false }: { nodeId: string | null; whole?: boolean }) {
  const { t } = useTranslation("lifecycle");
  const params = useBuilder((s) => s.dag.params);
  const [conf, setConf] = useState<Record<string, unknown>>(() => paramDefaults(params));
  return (
    <DialogShell title={t("test.title")} description={t("test.description")} size="xl">
      <Stack gap={4}>
        <JsonField
          help={t("test.confHelp")}
          label={t("test.conf")}
          rows={3}
          value={conf}
          validate={(v) => (isObj(v) ? undefined : t("common.mustBeObject"))}
          onChange={(v) => setConf(isObj(v) ? v : {})}
        />
        <Tabs.Root defaultValue={whole ? "dag" : "task"} size="sm" lazyMount>
          <Tabs.List>
            <Tabs.Trigger value="task">{t("test.tabTask")}</Tabs.Trigger>
            <Tabs.Trigger value="dag">{t("test.tabDag")}</Tabs.Trigger>
          </Tabs.List>
          <Tabs.Content value="task">
            <TaskTab initialNodeId={nodeId} conf={conf} />
          </Tabs.Content>
          <Tabs.Content value="dag">
            <DagTab conf={conf} />
          </Tabs.Content>
        </Tabs.Root>
      </Stack>
    </DialogShell>
  );
}
