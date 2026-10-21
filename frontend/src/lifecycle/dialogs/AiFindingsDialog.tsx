import { Badge, Box, Button, Checkbox, Field, HStack, Input, Stack, Table, Text, Textarea } from "@chakra-ui/react";
import { useMutation } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { LuRefreshCw } from "react-icons/lu";

import { fixDag, optimizeDag, reviewDag, reviewDagRules, scanDagSecrets, scanLeaks } from "../../api/gen/sdk.gen";
import type { DagSpec, Finding, FindingsReport, OptimizeReport } from "../../api/gen/types.gen";
import { useBuilder } from "../../store/builder";
import { ErrorBox, FindingsList, Pending, ProposalReview, UsageLine } from "../components/AiFindings";
import { DialogShell } from "../components/DialogShell";
import { type FindingsMode, patchable, requestModel, revealFinding, useAiResults, useAiStatus } from "../lib/ai";
import { useLifecycle } from "../store";

type Report = FindingsReport | OptimizeReport;

type FixInput = { taskLog: string; importErrors: string; runId: string; taskId: string };

async function runAnalysis(
  mode: FindingsMode,
  spec: DagSpec,
  useAi: boolean,
  model: string | null,
  fix: FixInput,
  signal?: AbortSignal,
): Promise<Report> {
  const common = { spec, use_ai: useAi, model };
  // Rules-only checks use the non-AI routes: they work without the AI extra or features.ai.
  switch (mode) {
    case "review":
      return useAi
        ? (await reviewDag({ body: common, signal, throwOnError: true })).data
        : (await reviewDagRules({ body: { spec }, signal, throwOnError: true })).data;
    case "leaks":
      return useAi
        ? (await scanLeaks({ body: common, signal, throwOnError: true })).data
        : (await scanDagSecrets({ body: { spec }, signal, throwOnError: true })).data;
    case "optimize":
      return (await optimizeDag({ body: common, signal, throwOnError: true })).data;
    case "fix": {
      const problems = useBuilder.getState().problems;
      return (
        await fixDag({
          body: {
            spec: spec as unknown as Record<string, unknown>,
            problems: problems.length ? problems : null,
            import_errors: fix.importErrors.trim() ? [fix.importErrors.trim()] : [],
            task_log: fix.taskLog.trim() || null,
            run_id: fix.runId.trim() || null,
            task_id: fix.taskId.trim() || null,
            dag_id: useBuilder.getState().savedDagId,
            model,
          },
          signal,
          throwOnError: true,
        })
      ).data;
    }
  }
}

function StatsTable({ report }: { report: OptimizeReport }) {
  const { t } = useTranslation("lifecycle");
  const stats = report.stats;
  if (!stats?.runs) return null;
  const fmt = (v: number | null | undefined) => (v == null ? "–" : `${Math.round(v)}s`);
  return (
    <Box>
      <Text fontSize="xs" color="fg.muted" mb={1}>
        {t("ai.optimize.history", {
          runs: stats.runs,
          rate: Math.round((stats.run_success_rate ?? 0) * 100),
          p95: fmt(stats.run_p95_seconds),
        })}
      </Text>
      <Table.Root size="sm" variant="outline">
        <Table.Header>
          <Table.Row>
            <Table.ColumnHeader>{t("ai.optimize.task")}</Table.ColumnHeader>
            <Table.ColumnHeader>p50</Table.ColumnHeader>
            <Table.ColumnHeader>p95</Table.ColumnHeader>
            <Table.ColumnHeader>{t("ai.optimize.failures")}</Table.ColumnHeader>
            <Table.ColumnHeader>{t("ai.optimize.retried")}</Table.ColumnHeader>
          </Table.Row>
        </Table.Header>
        <Table.Body>
          {(stats.tasks ?? []).map((s) => (
            <Table.Row key={s.task_id}>
              <Table.Cell fontFamily="mono" fontSize="xs">
                {s.task_id}
              </Table.Cell>
              <Table.Cell>{fmt(s.p50_seconds)}</Table.Cell>
              <Table.Cell>{fmt(s.p95_seconds)}</Table.Cell>
              <Table.Cell>{s.failed ?? 0}</Table.Cell>
              <Table.Cell>{s.retried_then_succeeded ?? 0}</Table.Cell>
            </Table.Row>
          ))}
        </Table.Body>
      </Table.Root>
    </Box>
  );
}

function FixInputs({ value, onChange }: { value: FixInput; onChange: (v: FixInput) => void }) {
  const { t } = useTranslation("lifecycle");
  return (
    <Stack gap={2}>
      <Text fontSize="xs" color="fg.muted">
        {t("ai.fix.evidenceHelp")}
      </Text>
      <HStack gap={2}>
        <Field.Root>
          <Field.Label fontSize="xs">{t("ai.fix.runId")}</Field.Label>
          <Input size="sm" value={value.runId} onChange={(e) => onChange({ ...value, runId: e.target.value })} />
        </Field.Root>
        <Field.Root>
          <Field.Label fontSize="xs">{t("ai.fix.taskId")}</Field.Label>
          <Input size="sm" value={value.taskId} onChange={(e) => onChange({ ...value, taskId: e.target.value })} />
        </Field.Root>
      </HStack>
      <Field.Root>
        <Field.Label fontSize="xs">{t("ai.fix.taskLog")}</Field.Label>
        <Textarea
          size="sm"
          rows={3}
          fontFamily="mono"
          value={value.taskLog}
          placeholder={t("ai.fix.taskLogPlaceholder")}
          onChange={(e) => onChange({ ...value, taskLog: e.target.value })}
        />
      </Field.Root>
      <Field.Root>
        <Field.Label fontSize="xs">{t("ai.fix.importError")}</Field.Label>
        <Textarea
          size="sm"
          rows={2}
          fontFamily="mono"
          value={value.importErrors}
          onChange={(e) => onChange({ ...value, importErrors: e.target.value })}
        />
      </Field.Root>
    </Stack>
  );
}

/**
 * Review / leak scan / optimise / fix: deterministic checks plus an optional LLM pass. Findings
 * reveal their node/field in the editor; selected patches are reviewed as a diff, then applied.
 */
export function AiFindingsDialog({ mode }: { mode: FindingsMode }) {
  const { t } = useTranslation("lifecycle");
  const close = useLifecycle((s) => s.closeDialog);
  const status = useAiStatus();
  const aiReady = !!status?.configured;
  const cached = useAiResults((s) => s.reports[mode]);
  const setReport = useAiResults((s) => s.setReport);
  const [spec, setSpec] = useState<DagSpec>(() => useBuilder.getState().toSpec());
  const [useAi, setUseAi] = useState(true);
  const [fix, setFix] = useState<FixInput>(() => ({
    taskLog: "",
    importErrors: "",
    runId: useLifecycle.getState().selectedRunId ?? "",
    taskId: useBuilder.getState().selectedId ?? "",
  }));
  const [selected, setSelected] = useState<Set<string> | null>(null);

  const run = useMutation({
    mutationFn: async () => {
      const snapshot = useBuilder.getState().toSpec();
      setSpec(snapshot);
      return runAnalysis(mode, snapshot, useAi && aiReady, requestModel(status), fix);
    },
    onSuccess: (report) => {
      setReport(mode, report);
      setSelected(null);
    },
  });
  // Open straight into results: run once unless a previous report is shown (fix needs input).
  const autoRun = !cached && mode !== "fix" && status !== undefined;
  const { mutate } = run;
  const started = useRef(false); // StrictMode runs effects twice: never pay for two LLM calls
  useEffect(() => {
    if (!autoRun || started.current) return;
    started.current = true;
    mutate();
  }, [autoRun, mutate]);
  const report: Report | undefined = run.data ?? cached;
  const findings = useMemo(() => report?.findings ?? [], [report]);
  const chosen = useMemo(() => selected ?? new Set(patchable(findings).map((f) => f.id)), [selected, findings]);
  const toggle = (id: string, on: boolean) => {
    const next = new Set(chosen);
    if (on) next.add(id);
    else next.delete(id);
    setSelected(next);
  };
  const reveal = (f: Finding) => {
    close();
    revealFinding(f);
  };
  const needsAi = mode === "fix";
  const canRun = !needsAi || aiReady;

  return (
    <DialogShell
      title={t(`ai.${mode}.title`)}
      description={t(`ai.${mode}.description`)}
      size="xl"
      footer={
        <HStack gap={3} w="full" justify="space-between" wrap="wrap">
          {!needsAi ? (
            <Checkbox.Root
              size="sm"
              checked={useAi && aiReady}
              disabled={!aiReady}
              onCheckedChange={(e) => setUseAi(e.checked === true)}
              title={!aiReady ? t("toolbar.aiDisabled") : undefined}
            >
              <Checkbox.HiddenInput />
              <Checkbox.Control />
              <Checkbox.Label fontSize="xs">{t("ai.useAi")}</Checkbox.Label>
            </Checkbox.Root>
          ) : (
            <span />
          )}
          <Button size="sm" variant="outline" loading={run.isPending} disabled={!canRun} onClick={() => run.mutate()}>
            <LuRefreshCw /> {report ? t("ai.runAgain") : t("ai.run")}
          </Button>
        </HStack>
      }
    >
      <Stack gap={3}>
        {needsAi && !aiReady ? <Text fontSize="sm">{t("toolbar.aiDisabled")}</Text> : null}
        {mode === "fix" ? <FixInputs value={fix} onChange={setFix} /> : null}
        {!report && !run.isPending && mode !== "fix" ? (
          <Text fontSize="sm" color="fg.muted">
            {t("ai.findings.intro")}
          </Text>
        ) : null}
        {run.isPending ? <Pending label={t("ai.analysing")} /> : null}
        {run.isError ? <ErrorBox error={run.error} /> : null}
        {report && !run.isPending ? (
          <Stack gap={3} aria-live="polite">
            <HStack gap={2} wrap="wrap">
              <Badge variant="outline">{t("ai.findings.count", { count: findings.length })}</Badge>
              {report.ai_used ? <Badge colorPalette="purple">AI</Badge> : <Badge>{t("ai.rulesOnly")}</Badge>}
              <UsageLine model={report.model} usage={report.usage} />
            </HStack>
            {report.summary ? (
              <Text fontSize="sm" whiteSpace="pre-wrap">
                {report.summary}
              </Text>
            ) : null}
            {(report.notes ?? []).map((n, i) => (
              <Text key={i} fontSize="xs" color="fg.muted">
                {n}
              </Text>
            ))}
            {"stats" in report && report.stats ? <StatsTable report={report} /> : null}
            <FindingsList findings={findings} selected={chosen} onToggle={toggle} onReveal={reveal} />
            {patchable(findings).length ? (
              <ProposalReview spec={spec} findings={findings} selected={chosen} onApplied={close} />
            ) : null}
          </Stack>
        ) : null}
      </Stack>
    </DialogShell>
  );
}
