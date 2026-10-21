import { Badge, Box, Button, HStack, Heading, Stack, Tabs, Text } from "@chakra-ui/react";
import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { LuCircleAlert, LuInfo, LuPower, LuTrash2, LuTriangleAlert } from "react-icons/lu";

import type { Problem } from "../api/gen/types.gen";
import type { BuilderNode, TaskFlowNode } from "../lib/types";
import { nodeKey } from "../lib/types";
import { effectiveTaskId } from "../lib/xcom";
import { selectedNode, useBuilder } from "../store/builder";
import { useFocusRequest } from "../store/focus";
import { problemText } from "../lib/problemMessages";
import { ProblemCode, splitLine } from "../widgets/ProblemCode";
import { listDecoratorsOptions } from "../api/gen/@tanstack/react-query.gen";
import { EditingNodeContext } from "../widgets/context";
import { focusField, TextField } from "../widgets/fields";
import { useInsertTarget } from "../widgets/xcomDnd";
import { useIsRaw } from "../codemode/store";
import { DagSettingsPanel, RawDagSettingsPanel } from "./DagSettings";
import { BehaviourTab } from "./inspector/BehaviourTab";
import { DataTab } from "./inspector/DataTab";
import { DocsTab } from "./inspector/DocsTab";
import { ExecutionTab } from "./inspector/ExecutionTab";
import { InputsPanel } from "./inspector/InputsPanel";
import { OperatorParams, useOperatorDetail } from "./inspector/OperatorParams";
import {
  CustomParams,
  EdgeInspector,
  GroupInspector,
  NoteInspector,
  ParentGroupSelect,
} from "./inspector/OtherInspectors";
import { decoratorIcon } from "../canvas/icons";
import { useOperatorSummary } from "../canvas/insertables";
import { IconField } from "./inspector/IconPicker";
import { PythonEditor } from "./inspector/PythonEditor";
import { TemplateBanner } from "./inspector/TemplateBanner";
import { TASK_TABS, type TaskTab, tabForProblem, useFieldError, useNodeProblems, useUpdate } from "./inspector/shared";

const SEVERITY_ICON = { error: LuCircleAlert, warning: LuTriangleAlert, info: LuInfo } as const;
const SEVERITY_COLOR = { error: "red.500", warning: "orange.500", info: "blue.500" } as const;

/** Problems of the selected node; clicking one with a field focuses that field. */
function ProblemsBanner({ problems, onFocus }: { problems: Problem[]; onFocus: (p: Problem) => void }) {
  const { t } = useTranslation("inspector");
  if (!problems.length) return null;
  return (
    <Stack gap={0.5} borderWidth="1px" borderColor="red.muted" bg="red.subtle" rounded="md" p={2} role="status">
      <Text fontSize="xs" fontWeight="semibold">
        {t("problems.title", { count: problems.length })}
      </Text>
      {problems.map((p, i) => {
        const Icon = SEVERITY_ICON[p.severity];
        const content = (
          <HStack gap={1} align="start">
            <Box color={SEVERITY_COLOR[p.severity]} pt={0.5} flexShrink={0}>
              <Icon size={12} aria-label={t(`problems.${p.severity}`)} />
            </Box>
            <Text fontSize="xs" textAlign="start" flex="1">
              {p.field ? (
                <Text as="span" fontFamily="mono" fontWeight="medium">
                  {p.field}:{" "}
                </Text>
              ) : null}
              {splitLine(problemText(p), p.line)[1]}
            </Text>
            <ProblemCode code={p.code} line={splitLine(p.message, p.line)[0]} docUrl={p.doc_url} />
          </HStack>
        );
        return p.field ? (
          <Button
            key={`${p.code}-${p.field}-${i}`}
            variant="plain"
            size="xs"
            h="auto"
            px={0}
            py={0.5}
            justifyContent="start"
            whiteSpace="normal"
            title={t("problems.focus")}
            onClick={() => onFocus(p)}
          >
            {content}
          </Button>
        ) : (
          <Box key={`${p.code}-${i}`} py={0.5}>
            {content}
          </Box>
        );
      })}
    </Stack>
  );
}

function NodeIconField({ node }: { node: TaskFlowNode }) {
  const update = useUpdate<"operator">(node.id);
  const spec = node.data.spec;
  const operator = spec.kind === "operator" ? spec.operator : undefined;
  const summary = useOperatorSummary(operator ?? "");
  const effective =
    spec.kind === "operator" ? summary?.icon : spec.kind === "python_task" ? decoratorIcon(spec.decorator) : null;
  return (
    <IconField
      value={spec.icon}
      effective={effective}
      operator={operator}
      onChange={(icon) => update({ icon: icon ?? null })}
    />
  );
}

function TaskHeaderFields({ node }: { node: TaskFlowNode }) {
  const { t } = useTranslation("inspector");
  const update = useUpdate<"operator">(node.id);
  const spec = node.data.spec;
  const taskIdError = useFieldError(node.id, "task_id");
  const nodes = useBuilder((s) => s.nodes);
  const effective = effectiveTaskId(nodes, node.id);
  return (
    <Stack gap={2}>
      <HStack align="start">
        <TextField
          label={t("node.taskId")}
          field="task_id"
          required
          mono
          value={spec.task_id}
          error={taskIdError}
          help={effective !== spec.task_id ? t("node.effectiveId", { id: effective }) : undefined}
          onChange={(v) => update({ task_id: v ?? "" })}
        />
        <TextField
          help={t("node.labelHelp")}
          label={t("node.label")}
          field="label"
          value={spec.label}
          onChange={(v) => update({ label: v })}
        />
      </HStack>
      <NodeIconField node={node} />
      <ParentGroupSelect node={node} />
    </Stack>
  );
}

function OperatorDocsTab({ node }: { node: Extract<TaskFlowNode, { type: "operator" }> }) {
  const detail = useOperatorDetail(node.data.spec.operator);
  return <DocsTab node={node} docstring={detail.data?.docstring} />;
}

/** `@task.<name>` docs from the provider's decorator (plain `@task` = the standard `python` one). */
function DecoratorDocsTab({ node }: { node: Extract<TaskFlowNode, { type: "python_task" }> }) {
  const decorators = useQuery({ ...listDecoratorsOptions(), retry: false, staleTime: 300_000 });
  const dec = node.data.spec.decorator ?? "task";
  const name = dec === "task" ? "python" : dec;
  const info = decorators.data?.find((d) => d.name === name);
  return <DocsTab node={node} docstring={info?.docstring} />;
}

function TaskInspector({ node }: { node: TaskFlowNode }) {
  const { t } = useTranslation("inspector");
  const problems = useNodeProblems(node.id);
  const [tab, setTab] = useState<TaskTab>("parameters");
  const [pending, setPending] = useState<string | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);

  // A problem clicked elsewhere (Problems tab, AI findings) targeting this node: switch tab
  // while rendering (no effect cascade), then mark the request consumed.
  const request = useFocusRequest((s) => s.request);
  const [handled, setHandled] = useState(0);
  if (request && request.nodeId === node.id && request.seq !== handled) {
    setHandled(request.seq);
    const owner = tabForProblem({ field: request.field, code: request.code });
    if (owner) setTab(owner);
    if (request.field) setPending(request.field);
  }
  useEffect(() => {
    if (request && request.seq === handled) useFocusRequest.getState().clear();
  }, [request, handled]);

  // Focus after the owning tab has rendered (lazy editors may need a frame).
  useEffect(() => {
    if (!pending) return;
    // Lazy editors (CodeMirror, forms) may need a moment after the tab switch.
    const id = setTimeout(() => {
      focusField(pending, rootRef.current ?? document);
      setPending(null);
    }, 80);
    return () => clearTimeout(id);
  }, [pending, tab]);

  const counts: Partial<Record<TaskTab, number>> = {};
  for (const p of problems) {
    const owner = tabForProblem(p);
    if (owner && p.severity !== "info") counts[owner] = (counts[owner] ?? 0) + 1;
  }
  const paramsLabel = node.type === "python_task" ? t("tabs.code") : t("tabs.parameters");

  return (
    <Stack gap={3} ref={rootRef}>
      <ProblemsBanner
        problems={problems}
        onFocus={(p) => {
          const owner = tabForProblem(p);
          if (owner) setTab(owner);
          if (p.field) setPending(p.field);
        }}
      />
      <TemplateBanner nodeId={node.id} />
      <TaskHeaderFields node={node} />
      <Tabs.Root
        size="sm"
        variant="line"
        value={tab}
        onValueChange={(e) => setTab(e.value as TaskTab)}
        lazyMount
        unmountOnExit
      >
        <Tabs.List overflowX="auto" overflowY="hidden" flexWrap="nowrap">
          {TASK_TABS.map((k) => (
            <Tabs.Trigger key={k} value={k} fontSize="xs" px={2} flexShrink={0}>
              {k === "parameters" ? paramsLabel : t(`tabs.${k}`)}
              {counts[k] ? (
                <Badge size="xs" colorPalette="red" variant="solid" ms={1}>
                  {counts[k]}
                </Badge>
              ) : null}
            </Tabs.Trigger>
          ))}
        </Tabs.List>
        <Tabs.Content value="parameters" pt={3}>
          <Stack gap={3}>
            <InputsPanel nodeId={node.id} />
            {node.type === "operator" ? <OperatorParams node={node} /> : null}
            {node.type === "python_task" ? <PythonEditor node={node} /> : null}
            {node.type === "custom" ? <CustomParams node={node} /> : null}
          </Stack>
        </Tabs.Content>
        <Tabs.Content value="execution" pt={3}>
          <ExecutionTab node={node} />
        </Tabs.Content>
        <Tabs.Content value="behaviour" pt={3}>
          <BehaviourTab node={node} />
        </Tabs.Content>
        <Tabs.Content value="data" pt={3}>
          <DataTab node={node} />
        </Tabs.Content>
        <Tabs.Content value="docs" pt={3}>
          {node.type === "operator" ? (
            <OperatorDocsTab node={node} />
          ) : node.type === "python_task" ? (
            <DecoratorDocsTab node={node} />
          ) : (
            <DocsTab node={node} />
          )}
        </Tabs.Content>
      </Tabs.Root>
    </Stack>
  );
}

function kindLabel(node: BuilderNode, t: (k: string) => string): string {
  if (node.type === "operator") return node.data.spec.operator.split(".").pop() ?? t("kinds.operator");
  return t(`kinds.${node.type}`);
}

export function Inspector() {
  const { t } = useTranslation("inspector");
  const node = useBuilder(selectedNode);
  const removeNode = useBuilder((s) => s.removeNode);
  const edgeSelected = useBuilder((s) => s.edges.some((e) => e.selected));
  const raw = useIsRaw();
  const nodeId = node?.id ?? null;
  // The "Insert" target belongs to the previously selected node's form.
  useEffect(() => {
    useInsertTarget.setState({ target: null });
  }, [nodeId]);
  if (raw) return <RawDagSettingsPanel />;
  if (!node) return edgeSelected ? <EdgeInspector /> : <DagSettingsPanel />;
  const disabled = node.type !== "task_group" && node.type !== "note" && !!node.data.spec.disabled;
  return (
    <EditingNodeContext.Provider value={node.id}>
      <Stack gap={3} p={3}>
        <HStack justify="space-between">
          <HStack minW={0} gap={1}>
            <Heading size="sm" truncate>
              {nodeKey(node)}
            </Heading>
            <Badge size="sm">{kindLabel(node, t)}</Badge>
            {disabled ? (
              <Badge size="sm" colorPalette="gray" variant="outline">
                <LuPower /> {t("node.disabled")}
              </Badge>
            ) : null}
          </HStack>
          <Button size="xs" variant="ghost" colorPalette="red" onClick={() => removeNode(node.id)}>
            <LuTrash2 /> {t("node.delete")}
          </Button>
        </HStack>
        {node.type === "operator" || node.type === "python_task" || node.type === "custom" ? (
          <TaskInspector key={node.id} node={node} />
        ) : null}
        {node.type === "task_group" ? <GroupInspector key={node.id} node={node} /> : null}
        {node.type === "note" ? <NoteInspector key={node.id} node={node} /> : null}
      </Stack>
    </EditingNodeContext.Provider>
  );
}
