import { Badge, Box, Button, Collapsible, Field, HStack, Stack, Text, Textarea, chakra } from "@chakra-ui/react";
import { useMutation } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { LuCheck, LuChevronRight, LuCircleStop, LuCrosshair, LuMessagesSquare, LuSparkles } from "react-icons/lu";

import { explainDag, writeTaskCode } from "../../api/gen/sdk.gen";
import type { AgentResult, DagSpec, ExplainCodeResult } from "../../api/gen/types.gen";
import { MarkdownPreview } from "../../widgets/markdown";
import { useBuilder } from "../../store/builder";
import { toaster } from "../../ui/toaster";
import { ErrorBox, FindingsList, Pending, ProposalReview, UsageLine } from "../components/AiFindings";
import { AiStreamProgress, useAiStream } from "../components/AiStream";
import { DialogShell } from "../components/DialogShell";
import { requestModel, revealFinding, useAiStatus } from "../lib/ai";
import { useLifecycle } from "../store";

const pythonNode = (nodeId: string | null) => {
  const node = useBuilder.getState().nodes.find((n) => n.id === nodeId);
  return node?.type === "python_task" ? node : null;
};

/** Structured walkthrough of the DAG (or the selected node); steps link to their nodes. */
export function AiExplainDagDialog() {
  const { t } = useTranslation("lifecycle");
  const close = useLifecycle((s) => s.closeDialog);
  const status = useAiStatus();
  const [question, setQuestion] = useState("");
  const [input] = useState(() => ({
    spec: useBuilder.getState().toSpec(),
    nodeId: useBuilder.getState().selectedId,
  }));
  const explain = useMutation({
    mutationFn: async (q: string | null) =>
      (
        await explainDag({
          body: { spec: input.spec, node_id: input.nodeId, question: q, model: requestModel(status) },
          throwOnError: true,
        })
      ).data,
  });
  const { mutate } = explain;
  const started = useRef(false); // StrictMode runs effects twice: one LLM call only
  useEffect(() => {
    if (started.current) return;
    started.current = true;
    mutate(null);
  }, [mutate]);
  const data = explain.data;
  return (
    <DialogShell title={t("ai.explainDag.title")} description={input.nodeId ?? undefined} size="lg">
      <Stack gap={3}>
        {explain.isPending ? <Pending label={t("ai.thinking")} /> : null}
        {explain.isError ? <ErrorBox error={explain.error} /> : null}
        {data ? (
          <Stack gap={3} aria-live="polite">
            <Text fontSize="sm">{data.summary}</Text>
            <Text fontSize="xs" color="fg.muted">
              {t("ai.explainDag.schedule")}: {data.schedule}
            </Text>
            <Stack gap={1} as="ol" m={0} pl={4}>
              {data.steps.map((s, i) => (
                <Box as="li" key={i} fontSize="sm">
                  <HStack gap={1} wrap="wrap">
                    <Text fontWeight="semibold">{s.title}</Text>
                    {s.node_id ? (
                      <Button
                        size="2xs"
                        variant="ghost"
                        onClick={() => {
                          close();
                          revealFinding({ node_id: s.node_id, field: null });
                        }}
                      >
                        <LuCrosshair /> {s.node_id}
                      </Button>
                    ) : null}
                  </HStack>
                  <Text fontSize="xs" color="fg.muted">
                    {s.explanation}
                  </Text>
                </Box>
              ))}
            </Stack>
            {data.data_flow ? (
              <Text fontSize="sm">
                <b>{t("ai.explainDag.dataFlow")}:</b> {data.data_flow}
              </Text>
            ) : null}
            {data.connections.length ? (
              <HStack gap={1} wrap="wrap">
                <Text fontSize="xs">{t("ai.explainDag.connections")}:</Text>
                {data.connections.map((c) => (
                  <Badge key={c} size="xs" variant="outline" fontFamily="mono">
                    {c}
                  </Badge>
                ))}
              </HStack>
            ) : null}
            {data.risks.length ? (
              <Stack gap={0.5}>
                <Text fontSize="xs" fontWeight="semibold">
                  {t("ai.explainDag.risks")}
                </Text>
                {data.risks.map((r, i) => (
                  <Text key={i} fontSize="xs" color="orange.500">
                    • {r}
                  </Text>
                ))}
              </Stack>
            ) : null}
            <UsageLine model={data.model} usage={data.usage} />
          </Stack>
        ) : null}
        <HStack gap={2} align="end">
          <Field.Root flex="1">
            <Field.Label fontSize="xs">{t("ai.explainDag.question")}</Field.Label>
            <Textarea size="sm" rows={2} value={question} onChange={(e) => setQuestion(e.target.value)} />
          </Field.Root>
          <Button
            size="sm"
            variant="outline"
            disabled={!question.trim()}
            loading={explain.isPending}
            onClick={() => explain.mutate(question.trim())}
          >
            <LuMessagesSquare /> {t("ai.ask.send")}
          </Button>
        </HStack>
      </Stack>
    </DialogShell>
  );
}

/** Writes (or explains) the body of the selected Python task. */
export function AiTaskCodeDialog({ nodeId }: { nodeId: string }) {
  const { t } = useTranslation("lifecycle");
  const close = useLifecycle((s) => s.closeDialog);
  const status = useAiStatus();
  const [description, setDescription] = useState("");
  const current = pythonNode(nodeId)?.data.spec.code ?? "";
  const write = useMutation({
    mutationFn: async () =>
      (
        await writeTaskCode({
          body: {
            description: description.trim(),
            spec: useBuilder.getState().toSpec(),
            node_id: nodeId,
            model: requestModel(status),
          },
          throwOnError: true,
        })
      ).data,
  });
  const explain = useAiStream();
  const explained =
    explain.state.result?.kind === "explain_code" ? (explain.state.result.data as ExplainCodeResult) : null;
  const result = write.data;
  return (
    <DialogShell
      title={t("ai.taskCode.title", { nodeId })}
      description={t("ai.taskCode.description")}
      size="xl"
      footer={
        <>
          <Button
            size="sm"
            variant="ghost"
            disabled={!current.trim() || current.trim() === "pass"}
            loading={explain.state.running}
            onClick={() =>
              void explain.start("/ai/explain-code/stream", {
                code: current,
                language: "python",
                context: `Airflow task ${nodeId}`,
                model: requestModel(status),
              })
            }
          >
            {t("ai.taskCode.explainCurrent")}
          </Button>
          <Button
            size="sm"
            variant="outline"
            disabled={!description.trim()}
            loading={write.isPending}
            onClick={() => write.mutate()}
          >
            <LuSparkles /> {result ? t("ai.regenerate") : t("ai.generate")}
          </Button>
          <Button
            size="sm"
            colorPalette="blue"
            disabled={!result?.code || !result.syntax_ok}
            onClick={() => {
              if (!result || !pythonNode(nodeId)) return;
              useBuilder.getState().updateSpec<"python_task">(nodeId, { code: result.code });
              toaster.create({ type: "success", title: t("ai.taskCode.inserted") });
              close();
            }}
          >
            <LuCheck /> {t("ai.taskCode.insert")}
          </Button>
        </>
      }
    >
      <Stack gap={3}>
        <Field.Root>
          <Field.Label>{t("ai.taskCode.prompt")}</Field.Label>
          <Textarea
            autoFocus
            rows={4}
            value={description}
            placeholder={t("ai.taskCode.placeholder")}
            onChange={(e) => setDescription(e.target.value)}
          />
        </Field.Root>
        {write.isPending ? <Pending label={t("ai.thinking")} /> : null}
        {write.isError ? <ErrorBox error={write.error} /> : null}
        {explain.state.running || explain.state.error || explain.state.text ? (
          <Box borderWidth="1px" rounded="md" p={3}>
            {explained ? (
              <>
                <MarkdownPreview text={explained.explanation} />
                <UsageLine model={explained.model} usage={explained.usage} />
              </>
            ) : (
              <AiStreamProgress state={explain.state} />
            )}
          </Box>
        ) : null}
        {result ? (
          <Stack gap={2} aria-live="polite">
            <HStack gap={2} wrap="wrap">
              <Badge colorPalette={result.syntax_ok ? "green" : "red"}>
                {result.syntax_ok ? t("ai.taskCode.syntaxOk") : t("ai.taskCode.syntaxError")}
              </Badge>
              {(result.requirements ?? []).map((r) => (
                <Badge key={r} size="xs" variant="outline" fontFamily="mono">
                  pip: {r}
                </Badge>
              ))}
              {(result.connections ?? []).map((c) => (
                <Badge key={c} size="xs" variant="outline" fontFamily="mono">
                  conn: {c}
                </Badge>
              ))}
              <UsageLine model={result.model} usage={result.usage} />
            </HStack>
            <chakra.pre
              fontSize="xs"
              fontFamily="mono"
              bg="bg.muted"
              p={3}
              rounded="md"
              overflow="auto"
              maxH="320px"
              data-testid="ai-task-code"
            >
              {result.code}
            </chakra.pre>
            {result.explanation ? (
              <Text fontSize="xs" color="fg.muted" whiteSpace="pre-wrap">
                {result.explanation}
              </Text>
            ) : null}
            {result.findings?.length ? <FindingsList findings={result.findings} /> : null}
          </Stack>
        ) : null}
      </Stack>
    </DialogShell>
  );
}

/** Free-form question answered by the tool-using agent, streamed live (tokens, tool calls);
 * proposals are reviewed as a diff. */
export function AiAskDialog() {
  const { t } = useTranslation("lifecycle");
  const close = useLifecycle((s) => s.closeDialog);
  const status = useAiStatus();
  const [question, setQuestion] = useState("");
  const [spec, setSpec] = useState<DagSpec>(() => useBuilder.getState().toSpec());
  const { state, start, cancel } = useAiStream();
  const data = state.result?.kind === "agent" ? (state.result.data as AgentResult) : null;
  const send = () => {
    if (!question.trim() || state.running) return;
    const snapshot = useBuilder.getState().toSpec();
    setSpec(snapshot);
    void start("/ai/ask/stream", { question: question.trim(), spec: snapshot, model: requestModel(status) });
  };
  return (
    <DialogShell title={t("ai.ask.title")} description={t("ai.ask.description")} size="xl">
      <Stack gap={3}>
        <HStack gap={2} align="end">
          <Field.Root flex="1">
            <Textarea
              autoFocus
              rows={3}
              value={question}
              placeholder={t("ai.ask.placeholder")}
              aria-label={t("ai.ask.title")}
              onChange={(e) => setQuestion(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) send();
              }}
            />
          </Field.Root>
          {state.running ? (
            <Button size="sm" variant="outline" colorPalette="red" onClick={cancel}>
              <LuCircleStop /> {t("ai.stream.cancel")}
            </Button>
          ) : (
            <Button size="sm" colorPalette="blue" disabled={!question.trim()} onClick={send}>
              <LuMessagesSquare /> {t("ai.ask.send")}
            </Button>
          )}
        </HStack>
        <AiStreamProgress state={state} showText={!data} showUsage={!data} />
        {data ? (
          <Stack gap={3} aria-live="polite">
            <Box fontSize="sm" data-testid="ai-answer">
              <MarkdownPreview text={data.answer ?? ""} />
            </Box>
            <HStack gap={2} wrap="wrap">
              {data.stopped && data.stopped !== "answer" ? (
                <Badge colorPalette="orange">
                  {t(`ai.ask.stopped.${data.stopped}`, { defaultValue: data.stopped })}
                </Badge>
              ) : null}
              {data.runtime ? (
                <Badge size="xs" variant="outline">
                  {t("ai.stream.runtime", { runtime: data.runtime })}
                </Badge>
              ) : null}
              <UsageLine model={data.model} usage={data.usage} />
            </HStack>
            {data.steps?.length && !state.tools.length ? (
              <Collapsible.Root>
                <Collapsible.Trigger asChild>
                  <Button size="2xs" variant="ghost">
                    <LuChevronRight /> {t("ai.ask.steps", { count: data.steps.length })}
                  </Button>
                </Collapsible.Trigger>
                <Collapsible.Content>
                  <Stack gap={1} pl={4}>
                    {data.steps.map((s) => (
                      <Text key={s.index} fontSize="2xs" fontFamily="mono" color={s.ok ? "fg.muted" : "red.500"}>
                        {s.tool}({JSON.stringify(s.arguments ?? {}).slice(0, 120)}) · {s.duration_ms ?? 0}ms
                      </Text>
                    ))}
                  </Stack>
                </Collapsible.Content>
              </Collapsible.Root>
            ) : null}
            {data.findings?.length ? (
              <FindingsList
                findings={data.findings}
                onReveal={(f) => {
                  close();
                  revealFinding(f);
                }}
              />
            ) : null}
            {data.proposed_spec ? <ProposalReview spec={spec} proposed={data.proposed_spec} onApplied={close} /> : null}
          </Stack>
        ) : null}
      </Stack>
    </DialogShell>
  );
}
