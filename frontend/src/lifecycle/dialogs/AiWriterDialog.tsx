import { Badge, Box, Button, Field, HStack, Input, NativeSelect, Stack, Table, Text, Textarea } from "@chakra-ui/react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { LuCheck, LuCircleStop, LuCopy, LuSparkles } from "react-icons/lu";

import type { WriteRequest, WriterResult } from "../../api/gen/types.gen";
import { useBuilder } from "../../store/builder";
import { toaster } from "../../ui/toaster";
import { MarkdownPreview } from "../../widgets/markdown";
import { AiCodeEditor, type AiCodeLanguage } from "../components/AiCodeEditor";
import { FindingsList, UsageLine } from "../components/AiFindings";
import { AiStreamProgress, useAiStream } from "../components/AiStream";
import { DialogShell } from "../components/DialogShell";
import { requestModel, useAiStatus } from "../lib/ai";
import { useLifecycle } from "../store";

type Kind = WriteRequest["kind"];
const KINDS: Kind[] = ["task_body", "inline_function", "bash", "sql", "operator_config", "custom_operator", "dag_spec"];
const DIALECTS = ["postgres", "mysql", "snowflake", "bigquery", "redshift", "duckdb", "tsql", "spark", "ansi"];

function languageOf(result: WriterResult): AiCodeLanguage {
  switch (result.language) {
    case "python":
    case "bash":
    case "sql":
    case "json":
      return result.language;
    default:
      return "text";
  }
}

/**
 * The node a writer result may be applied to (exported for tests): the python task the
 * run was *started* for — never whichever node happens to be selected now. Null when that
 * node is gone, is no longer a python task, or the result is not an applicable body.
 */
export function writerApplyTarget(
  result: Pick<WriterResult, "ok" | "kind"> | null,
  targetId: string | null,
  nodes: readonly { id: string; type?: string | undefined }[],
): string | null {
  if (!result?.ok || result.kind !== "task_body" || !targetId) return null;
  const node = nodes.find((n) => n.id === targetId);
  return node?.type === "python_task" ? node.id : null;
}

const SEVERITY_PALETTE = { error: "red", warning: "orange", info: "gray" } as const;

/** Specialised code writers (Python task, callbacks, Bash, SQL, operator configs, custom
 * operators, whole DAGs) with server-side validate-and-repair, streamed as progress. */
export function AiWriterDialog() {
  const { t } = useTranslation("lifecycle");
  const close = useLifecycle((s) => s.closeDialog);
  const status = useAiStatus();
  const selected = useBuilder((s) => s.nodes.find((n) => n.id === s.selectedId) ?? null);
  const pythonSelected = selected?.type === "python_task" ? selected : null;
  const [kind, setKind] = useState<Kind>(pythonSelected ? "task_body" : "bash");
  const [description, setDescription] = useState("");
  const [dialect, setDialect] = useState("postgres");
  const [functionName, setFunctionName] = useState("on_failure");
  const { state, start, cancel } = useAiStream();
  // The python task the current run was started for; results apply to it only.
  const [targetId, setTargetId] = useState<string | null>(null);
  const result = state.result?.kind === "writer" ? (state.result.data as WriterResult) : null;
  const nameOk = kind !== "inline_function" || /^[A-Za-z_]\w*$/.test(functionName);

  const run = () => {
    if (!description.trim() || !nameOk) return;
    const st = useBuilder.getState();
    const nodeId = kind === "task_body" && pythonSelected ? pythonSelected.id : null;
    setTargetId(nodeId);
    const body: WriteRequest = {
      kind,
      description: description.trim(),
      spec: st.toSpec(),
      node_id: nodeId,
      dialect: kind === "sql" ? dialect : null,
      function_name: kind === "inline_function" ? functionName : null,
      model: requestModel(status),
    };
    void start("/ai/write/stream", body);
  };

  const copy = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      toaster.create({ type: "success", title: t("ai.writer.copied") });
    } catch {
      toaster.create({ type: "error", title: t("ai.writer.copyFailed") });
    }
  };

  const applyTo = useBuilder((s) => writerApplyTarget(result, targetId, s.nodes));
  return (
    <DialogShell
      title={t("ai.writer.title")}
      description={t("ai.writer.description")}
      size="xl"
      footer={
        <>
          {result?.code ? (
            <Button size="sm" variant="ghost" onClick={() => void copy(result.code)}>
              <LuCopy /> {t("ai.writer.copy")}
            </Button>
          ) : null}
          {applyTo && result ? (
            <Button
              size="sm"
              variant="outline"
              onClick={() => {
                useBuilder.getState().updateSpec<"python_task">(applyTo, { code: result.code });
                toaster.create({ type: "success", title: t("ai.taskCode.inserted") });
                close();
              }}
            >
              <LuCheck /> {t("ai.writer.apply", { node: applyTo })}
            </Button>
          ) : null}
          {state.running ? (
            <Button size="sm" variant="outline" colorPalette="red" onClick={cancel}>
              <LuCircleStop /> {t("ai.stream.cancel")}
            </Button>
          ) : (
            <Button size="sm" colorPalette="blue" disabled={!description.trim() || !nameOk} onClick={run}>
              <LuSparkles /> {result ? t("ai.regenerate") : t("ai.generate")}
            </Button>
          )}
        </>
      }
    >
      <Stack gap={3}>
        <HStack gap={3} align="end" wrap="wrap">
          <Field.Root w="auto" minW="14rem">
            <Field.Label fontSize="xs">{t("ai.writer.kind")}</Field.Label>
            <NativeSelect.Root size="sm">
              <NativeSelect.Field
                value={kind}
                aria-label={t("ai.writer.kind")}
                onChange={(e) => setKind(e.target.value as Kind)}
              >
                {KINDS.map((k) => (
                  <option key={k} value={k}>
                    {t(`ai.writer.kinds.${k}`)}
                  </option>
                ))}
              </NativeSelect.Field>
              <NativeSelect.Indicator />
            </NativeSelect.Root>
          </Field.Root>
          {kind === "sql" ? (
            <Field.Root w="auto" minW="10rem">
              <Field.Label fontSize="xs">{t("ai.writer.dialect")}</Field.Label>
              <NativeSelect.Root size="sm">
                <NativeSelect.Field value={dialect} onChange={(e) => setDialect(e.target.value)}>
                  {DIALECTS.map((d) => (
                    <option key={d} value={d}>
                      {d}
                    </option>
                  ))}
                </NativeSelect.Field>
                <NativeSelect.Indicator />
              </NativeSelect.Root>
            </Field.Root>
          ) : null}
          {kind === "inline_function" ? (
            <Field.Root w="auto" minW="12rem" invalid={!nameOk}>
              <Field.Label fontSize="xs">{t("ai.writer.functionName")}</Field.Label>
              <Input
                size="sm"
                fontFamily="mono"
                value={functionName}
                onChange={(e) => setFunctionName(e.target.value)}
              />
              {!nameOk ? <Field.ErrorText>{t("ai.writer.badName")}</Field.ErrorText> : null}
            </Field.Root>
          ) : null}
          {kind === "task_body" ? (
            <Text fontSize="xs" color="fg.muted" pb={2}>
              {pythonSelected ? t("ai.writer.forNode", { node: pythonSelected.id }) : t("ai.writer.noNode")}
            </Text>
          ) : null}
        </HStack>
        <Field.Root>
          <Field.Label fontSize="xs">{t("ai.writer.prompt")}</Field.Label>
          <Textarea
            autoFocus
            rows={3}
            value={description}
            placeholder={t(`ai.writer.placeholder.${kind}`)}
            onChange={(e) => setDescription(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && !state.running) run();
            }}
          />
          <Field.HelperText fontSize="2xs">{t("ai.writer.helper")}</Field.HelperText>
        </Field.Root>
        {state.running || state.error || state.cancelled || state.interrupted ? (
          <AiStreamProgress state={state} showText={false} />
        ) : null}
        {result ? <WriterResultView result={result} /> : null}
      </Stack>
    </DialogShell>
  );
}

function WriterResultView({ result }: { result: WriterResult }) {
  const { t } = useTranslation("lifecycle");
  const issues = result.issues ?? [];
  return (
    <Stack gap={2} aria-live="polite" data-testid="ai-writer-result">
      <HStack gap={2} wrap="wrap">
        <Badge colorPalette={result.ok ? "green" : "red"}>
          {result.ok ? t("ai.writer.passed") : t("ai.writer.notPassed")}
        </Badge>
        {(result.checks ?? []).map((c) => (
          <Badge key={c} size="xs" variant="outline">
            ✓ {c}
          </Badge>
        ))}
        {result.rounds && result.rounds > 1 ? (
          <Badge size="xs" variant="subtle" colorPalette="blue">
            {t("ai.writer.rounds", { count: result.rounds })}
          </Badge>
        ) : null}
        <UsageLine model={result.model} usage={result.usage} />
      </HStack>
      {result.code ? (
        <AiCodeEditor
          value={result.code}
          language={languageOf(result)}
          readOnly
          label={t("ai.writer.code")}
          height={`${Math.min(360, Math.max(96, result.code.split("\n").length * 18 + 28))}px`}
        />
      ) : null}
      {issues.length ? (
        <Box borderWidth="1px" rounded="md" overflowX="auto">
          <Table.Root size="sm">
            <Table.Header>
              <Table.Row>
                <Table.ColumnHeader>{t("ai.writer.severity")}</Table.ColumnHeader>
                <Table.ColumnHeader>{t("ai.writer.source")}</Table.ColumnHeader>
                <Table.ColumnHeader>{t("ai.writer.line")}</Table.ColumnHeader>
                <Table.ColumnHeader>{t("ai.writer.message")}</Table.ColumnHeader>
              </Table.Row>
            </Table.Header>
            <Table.Body>
              {issues.map((issue, i) => (
                <Table.Row key={i}>
                  <Table.Cell>
                    <Badge size="xs" colorPalette={SEVERITY_PALETTE[issue.severity] ?? "gray"}>
                      {issue.severity}
                    </Badge>
                  </Table.Cell>
                  <Table.Cell fontFamily="mono" fontSize="xs">
                    {issue.source}
                  </Table.Cell>
                  <Table.Cell fontSize="xs">{issue.line ?? "–"}</Table.Cell>
                  <Table.Cell fontSize="xs" wordBreak="break-word">
                    {issue.message}
                  </Table.Cell>
                </Table.Row>
              ))}
            </Table.Body>
          </Table.Root>
        </Box>
      ) : null}
      {(result.requirements ?? []).length || (result.connections ?? []).length ? (
        <HStack gap={1} wrap="wrap">
          {(result.requirements ?? []).map((r) => (
            <Badge key={`pip-${r}`} size="xs" variant="outline" fontFamily="mono">
              pip: {r}
            </Badge>
          ))}
          {(result.connections ?? []).map((c) => (
            <Badge key={`conn-${c}`} size="xs" variant="outline" fontFamily="mono">
              conn: {c}
            </Badge>
          ))}
        </HStack>
      ) : null}
      {result.explanation ? (
        <Box fontSize="sm" color="fg.muted">
          <MarkdownPreview text={result.explanation} />
        </Box>
      ) : null}
      {result.findings?.length ? <FindingsList findings={result.findings} /> : null}
    </Stack>
  );
}
