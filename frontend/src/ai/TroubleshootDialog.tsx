import {
  Badge,
  Box,
  Button,
  Checkbox,
  Code,
  Field,
  HStack,
  Progress,
  SimpleGrid,
  Spinner,
  Stack,
  Text,
  Textarea,
} from "@chakra-ui/react";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { LuCircleStop, LuFlaskConical, LuPlay, LuRefreshCw, LuRocket, LuSquarePen, LuX } from "react-icons/lu";

import { listTroubleshootAnalysesOptions, troubleshootOptionsOptions } from "../api/gen/@tanstack/react-query.gen";
import {
  aiMcpServerTools,
  decideTroubleshootAnalysis,
  getTroubleshootAnalysis,
  troubleshootAnalysisApplied,
} from "../api/gen/sdk.gen";
import type {
  McpServerOption,
  McpToolInfo,
  ToolDecision,
  TroubleshootAnalysis,
  TroubleshootOptions,
  TroubleshootScope,
} from "../api/gen/types.gen";
import { errorMessage } from "../api/setup";
import { useAccess } from "../lib/access";
import { loadAsUnsaved } from "../lifecycle/actions";
import { UsageLine } from "../lifecycle/components/AiFindings";
import { AiStreamProgress, useAiStream } from "../lifecycle/components/AiStream";
import { DialogShell } from "../lifecycle/components/DialogShell";
import { requestModel, severityPalette, useAiStatus } from "../lifecycle/lib/ai";
import { useLifecycle } from "../lifecycle/store";
import { useBuilder } from "../store/builder";
import { toaster } from "../ui/toaster";
import { CodeBlock } from "../widgets/markdown/Markdown";
import { type ActionsMode, ActionsModePicker, AirflowActions, clampMode } from "./AirflowActions";
import { Citations, ToolChips } from "./ChatMessageView";
import { watchNextDeploy } from "./deployWatch";
import { SpecDiffReview } from "./SpecDiffReview";
import { type TroubleshootTarget, useAiChat } from "./store";
import { ToolApprovalCard, isOpen } from "./ToolApprovalCard";

export const SCOPE_KEYS = [
  "connections",
  "variables",
  "pools",
  "xcom_keys",
  "xcom_values",
  "other_runs",
  "import_errors",
  "airflow_config",
  "filesystem",
  "research",
  "online",
] as const satisfies ReadonlyArray<keyof TroubleshootScope>;

type ScopeKey = (typeof SCOPE_KEYS)[number];
type McpPick = Record<string, string[] | null | undefined>;

/** The scope a request may ask for: the user's choice, never more than the server allows. */
export function clampScope(scope: TroubleshootScope, allowed: TroubleshootScope): TroubleshootScope {
  const out: TroubleshootScope = {};
  for (const key of SCOPE_KEYS) out[key] = !!scope[key] && !!allowed[key];
  return out;
}

function ScopeForm({
  options,
  scope,
  onScope,
  mcp,
  onMcp,
}: {
  options: TroubleshootOptions;
  scope: TroubleshootScope;
  onScope: (s: TroubleshootScope) => void;
  mcp: McpPick;
  onMcp: (m: McpPick) => void;
}) {
  const { t } = useTranslation("ai");
  return (
    <Stack gap={3}>
      <Box>
        <Text fontSize="sm" fontWeight="semibold" mb={1}>
          {t("troubleshoot.scope")}
        </Text>
        <Text fontSize="xs" color="fg.muted" mb={2}>
          {t("troubleshoot.scopeHelp")}
        </Text>
        <SimpleGrid columns={{ base: 1, md: 2 }} gap={1.5}>
          {SCOPE_KEYS.map((key: ScopeKey) => {
            const allowed = !!options.allowed[key];
            return (
              <Checkbox.Root
                key={key}
                size="sm"
                checked={!!scope[key] && allowed}
                disabled={!allowed}
                onCheckedChange={(e) => onScope({ ...scope, [key]: !!e.checked })}
                data-testid={`scope-${key}`}
              >
                <Checkbox.HiddenInput />
                <Checkbox.Control />
                <Checkbox.Label>
                  <Text as="span" fontSize="sm">
                    {t(`troubleshoot.scopes.${key}`)}
                  </Text>
                  {!allowed ? (
                    <Text as="span" fontSize="2xs" color="fg.subtle">
                      {" "}
                      ({t("troubleshoot.notAllowed")})
                    </Text>
                  ) : null}
                </Checkbox.Label>
              </Checkbox.Root>
            );
          })}
        </SimpleGrid>
        {scope.filesystem && options.filesystem_roots?.length ? (
          <Text fontSize="2xs" color="fg.muted" mt={1}>
            {t("troubleshoot.roots")}: {options.filesystem_roots.join(", ")}
          </Text>
        ) : null}
      </Box>
      {options.mcp_servers?.length ? (
        <Box>
          <Text fontSize="sm" fontWeight="semibold" mb={1}>
            {t("troubleshoot.mcp")}
          </Text>
          <Stack gap={1.5}>
            {options.mcp_servers.map((s) => (
              <McpServerPick
                key={s.name}
                server={s}
                value={mcp[s.name]}
                onChange={(v) => onMcp({ ...mcp, [s.name]: v })}
              />
            ))}
          </Stack>
        </Box>
      ) : null}
    </Stack>
  );
}

function McpServerPick({
  server,
  value,
  onChange,
}: {
  server: McpServerOption;
  value: string[] | null | undefined;
  onChange: (v: string[] | null | undefined) => void;
}) {
  const { t } = useTranslation("ai");
  const on = value !== undefined;
  const tools = useQuery({
    queryKey: ["ai", "mcp-tools", server.name],
    enabled: on,
    queryFn: async ({ signal }) =>
      (await aiMcpServerTools({ path: { name: server.name }, signal, throwOnError: true })).data,
    staleTime: 60_000,
    retry: false,
  });
  return (
    <Box borderWidth="1px" rounded="md" p={2} data-testid={`mcp-${server.name}`}>
      <Checkbox.Root size="sm" checked={on} onCheckedChange={(e) => onChange(e.checked ? null : undefined)}>
        <Checkbox.HiddenInput />
        <Checkbox.Control />
        <Checkbox.Label>
          <Text as="span" fontWeight="medium" fontSize="sm">
            {server.name}
          </Text>{" "}
          <Text as="span" fontSize="xs" color="fg.muted">
            {server.description}
          </Text>
        </Checkbox.Label>
      </Checkbox.Root>
      {on ? (
        <Box pl={6} pt={1}>
          {tools.isPending ? <Spinner size="xs" /> : null}
          {tools.isError ? (
            <Text fontSize="xs" color="fg.error">
              {errorMessage(tools.error)}
            </Text>
          ) : null}
          <HStack gap={1} wrap="wrap">
            {(tools.data ?? []).map((tool: McpToolInfo) => {
              const selected = value === null || (value ?? []).includes(tool.name);
              return (
                <Button
                  key={tool.name}
                  size="2xs"
                  variant={selected ? "subtle" : "outline"}
                  title={tool.description}
                  aria-pressed={selected}
                  onClick={() => {
                    const all = (tools.data ?? []).map((x) => x.name);
                    const current = value === null ? all : (value ?? []);
                    const next = selected ? current.filter((n) => n !== tool.name) : [...current, tool.name];
                    onChange(next.length === all.length ? null : next);
                  }}
                >
                  {tool.name}
                </Button>
              );
            })}
          </HStack>
          {tools.data ? (
            <Text fontSize="2xs" color="fg.subtle" mt={1}>
              {t("troubleshoot.mcpTools")}
            </Text>
          ) : null}
        </Box>
      ) : null}
    </Box>
  );
}

function Confidence({ value }: { value: number }) {
  const pct = Math.round(value * 100);
  const palette = value >= 0.75 ? "green" : value >= 0.4 ? "orange" : "gray";
  return (
    <HStack gap={2} minW="140px">
      <Progress.Root value={pct} size="xs" colorPalette={palette} flex="1" aria-label={`${pct}%`}>
        <Progress.Track>
          <Progress.Range />
        </Progress.Track>
      </Progress.Root>
      <Text fontSize="xs" color="fg.muted" w="36px" textAlign="end">
        {pct}%
      </Text>
    </HStack>
  );
}

/** The structured result of an analysis (hypotheses, remediation, proposals). */
export function AnalysisReport({
  analysis,
  selected,
  onSelected,
  onUpdate,
  onApproval,
}: {
  analysis: TroubleshootAnalysis;
  selected: string[];
  onSelected: (ids: string[]) => void;
  onUpdate: (a: TroubleshootAnalysis) => void;
  /** Decide the pending tool calls of a paused analysis (resumes it). */
  onApproval?: (approvalId: string, decisions: ToolDecision[]) => void;
}) {
  const { t } = useTranslation("ai");
  const patches = (analysis.findings ?? []).filter((f) => (f.patch ?? []).length > 0);
  const approvals = Array.isArray(analysis.approvals) ? analysis.approvals : [];
  return (
    <Stack gap={4} data-testid="troubleshoot-report">
      <Box borderWidth="1px" rounded="md" p={3} bg="bg.subtle">
        <HStack gap={2} mb={1} wrap="wrap">
          <Badge colorPalette={analysis.run_state === "failed" ? "red" : "gray"}>{analysis.run_state ?? "—"}</Badge>
          <Text fontSize="xs" color="fg.muted" fontFamily="mono">
            {analysis.dag_id} · {analysis.run_id}
            {analysis.task_id ? ` · ${analysis.task_id}` : ""}
            {analysis.dag_version != null ? ` · v${analysis.dag_version}` : ""}
          </Text>
          <Badge variant="outline">{t(`troubleshoot.status.${analysis.status ?? "no_change"}`)}</Badge>
        </HStack>
        <Text fontSize="sm" fontWeight="medium" data-testid="troubleshoot-summary">
          {analysis.summary || t("troubleshoot.noSummary")}
        </Text>
      </Box>
      {approvals.map((a, i) => (
        <ToolApprovalCard
          key={a.id ?? i}
          approval={a}
          onSubmit={onApproval && i === approvals.length - 1 && isOpen(a) ? (d) => onApproval(a.id, d) : undefined}
        />
      ))}
      {analysis.hypotheses?.length ? (
        <Box>
          <Text fontSize="sm" fontWeight="semibold" mb={1}>
            {t("troubleshoot.hypotheses")}
          </Text>
          <Stack gap={2}>
            {analysis.hypotheses.map((h, i) => (
              <Box key={i} borderWidth="1px" rounded="md" p={2} data-testid="troubleshoot-hypothesis">
                <HStack justify="space-between" gap={3} wrap="wrap">
                  <HStack gap={2} minW={0}>
                    <Badge size="xs" variant="surface">
                      {t(`troubleshoot.category.${h.category ?? "unknown"}`, { defaultValue: h.category ?? "" })}
                    </Badge>
                    <Text fontSize="sm" fontWeight="medium">
                      {h.title}
                    </Text>
                  </HStack>
                  <Confidence value={h.confidence ?? 0} />
                </HStack>
                {h.explanation ? (
                  <Text fontSize="xs" color="fg.muted" mt={1}>
                    {h.explanation}
                  </Text>
                ) : null}
                {h.evidence?.length ? (
                  <Stack gap={1} mt={1.5}>
                    {h.evidence.map((e, j) => (
                      <HStack key={j} gap={1.5} align="start" fontSize="xs">
                        <Code size="sm" flexShrink={0}>
                          {e.source}
                          {e.line != null ? `:${e.line}` : ""}
                        </Code>
                        <Text fontFamily="mono" fontSize="2xs" color="fg.muted" wordBreak="break-word">
                          {e.quote}
                        </Text>
                      </HStack>
                    ))}
                  </Stack>
                ) : null}
              </Box>
            ))}
          </Stack>
        </Box>
      ) : null}
      {analysis.remediation?.length ? (
        <Box>
          <Text fontSize="sm" fontWeight="semibold" mb={1}>
            {t("troubleshoot.remediation")}
          </Text>
          <Stack as="ol" gap={1} ps={5} listStyleType="decimal" fontSize="sm">
            {analysis.remediation.map((r, i) => (
              <Box as="li" key={i}>
                <Text as="span" fontWeight="medium">
                  {r.title}
                </Text>
                {r.detail ? (
                  <Text as="span" color="fg.muted">
                    {" "}
                    — {r.detail}
                  </Text>
                ) : null}
              </Box>
            ))}
          </Stack>
        </Box>
      ) : null}
      {patches.length ? (
        <Box>
          <Text fontSize="sm" fontWeight="semibold" mb={1}>
            {t("troubleshoot.specChanges")}
          </Text>
          <Stack gap={1} mb={2}>
            {patches.map((f) => (
              <Checkbox.Root
                key={f.id}
                size="sm"
                checked={selected.includes(f.id)}
                disabled={analysis.status !== "proposed"}
                onCheckedChange={(e) =>
                  onSelected(e.checked ? [...selected, f.id] : selected.filter((id) => id !== f.id))
                }
              >
                <Checkbox.HiddenInput />
                <Checkbox.Control />
                <Checkbox.Label>
                  <HStack gap={1.5}>
                    <Badge size="xs" colorPalette={severityPalette(f.severity)}>
                      {f.severity}
                    </Badge>
                    <Text fontSize="sm">{f.title}</Text>
                    {f.node_id ? (
                      <Text fontSize="xs" fontFamily="mono" color="fg.muted">
                        @{f.node_id}
                      </Text>
                    ) : null}
                  </HStack>
                </Checkbox.Label>
              </Checkbox.Root>
            ))}
          </Stack>
          <SpecDiffReview
            base={analysis.base_spec}
            proposed={analysis.proposed_spec}
            problems={analysis.problems}
            codeBefore={analysis.code_before}
            codeAfter={analysis.code_after}
            testId="troubleshoot-diff"
          />
        </Box>
      ) : null}
      {analysis.config_changes?.length ? (
        <Box>
          <Text fontSize="sm" fontWeight="semibold" mb={1}>
            {t("troubleshoot.configChanges")}
          </Text>
          <Stack gap={2}>
            {analysis.config_changes.map((c, i) => (
              <Box key={i}>
                <Text fontSize="xs" fontWeight="medium">
                  {c.target}
                </Text>
                {c.explanation ? (
                  <Text fontSize="xs" color="fg.muted">
                    {c.explanation}
                  </Text>
                ) : null}
                <CodeBlock code={c.snippet} language={c.format === "env" ? "bash" : c.format} />
              </Box>
            ))}
          </Stack>
        </Box>
      ) : null}
      <AirflowActions analysis={analysis} onUpdate={onUpdate} />
      {analysis.notes?.length ? (
        <Stack gap={0.5}>
          {analysis.notes.map((n, i) => (
            <Text key={i} fontSize="2xs" color="fg.muted">
              • {n}
            </Text>
          ))}
        </Stack>
      ) : null}
      <ToolChips
        tools={(analysis.steps ?? []).map((s) => ({
          id: String(s.index),
          name: s.tool,
          arguments: s.arguments,
          ok: s.ok,
          preview: s.output_preview,
          duration_ms: s.duration_ms,
        }))}
      />
      <Citations items={analysis.sources ?? []} />
      <UsageLine model={analysis.model} usage={analysis.usage} />
    </Stack>
  );
}

function initialScope(options: TroubleshootOptions | undefined): TroubleshootScope {
  return options ? { ...options.defaults } : {};
}

/** "Analyze with AI": scoped, streamed troubleshooting of a DAG run / task instance. */
export default function TroubleshootDialog({ target }: { target: TroubleshootTarget }) {
  const { t } = useTranslation("ai");
  const access = useAccess();
  const status = useAiStatus();
  const options = useQuery({ ...troubleshootOptionsOptions(), staleTime: 60_000 });
  const previous = useQuery({
    ...listTroubleshootAnalysesOptions({ query: { dag_id: target.dagId, limit: 10 } }),
    staleTime: 10_000,
  });
  const { state, start, cancel } = useAiStream();
  const [scope, setScope] = useState<TroubleshootScope | null>(null);
  const [mcp, setMcp] = useState<McpPick>({});
  const [question, setQuestion] = useState("");
  const [loaded, setLoaded] = useState<TroubleshootAnalysis | null>(null);
  const [rerunOf, setRerunOf] = useState<string | null>(null);
  const [selected, setSelected] = useState<string[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [phase, setPhase] = useState<"setup" | "run">("setup");
  const [actionsMode, setActionsMode] = useState<ActionsMode | null>(null);
  const close = () => useAiChat.getState().closeTroubleshoot();
  const effectiveScope = scope ?? initialScope(options.data);
  const streamed = state.result?.kind === "troubleshoot" ? (state.result.data as TroubleshootAnalysis) : null;
  const analysis = loaded ?? streamed;
  const patchIds = (analysis?.findings ?? []).filter((f) => (f.patch ?? []).length).map((f) => f.id);
  const chosen = selected ?? patchIds;

  const run = () => {
    if (!options.data) return;
    const spec = useBuilder.getState().savedDagId === target.dagId ? useBuilder.getState().toSpec() : null;
    setLoaded(null);
    setSelected(null);
    setPhase("run");
    void start("/ai/troubleshoot/stream", {
      dag_id: target.dagId,
      run_id: target.runId,
      ...(target.taskId ? { task_id: target.taskId } : {}),
      ...(target.mapIndex != null ? { map_index: target.mapIndex } : {}),
      ...(target.tryNumber ? { try_number: target.tryNumber } : {}),
      ...(spec ? { spec } : {}),
      ...(question.trim() ? { question: question.trim() } : {}),
      scope: clampScope(effectiveScope, options.data.allowed),
      mcp: Object.entries(mcp)
        .filter(([, v]) => v !== undefined)
        .map(([server, tools]) => ({ server, ...(tools ? { tools } : {}) })),
      ...(rerunOf ? { rerun_of: rerunOf } : {}),
      ...(requestModel(status) ? { model: requestModel(status) } : {}),
      airflow_actions: clampMode(actionsMode, options.data.actions_mode),
    });
  };
  const rerun = () => {
    if (analysis) {
      setScope({ ...analysis.scope });
      setRerunOf(analysis.id ?? null);
      if (analysis.question) setQuestion(analysis.question);
      if (analysis.actions_mode) setActionsMode(analysis.actions_mode);
    }
    setLoaded(null);
    setPhase("setup");
  };
  const decide = async (decision: "approve" | "reject", then?: "editor" | "deploy") => {
    if (!analysis?.id) return;
    setBusy(true);
    try {
      const { data } = await decideTroubleshootAnalysis({
        path: { analysis_id: analysis.id },
        body: { decision, ...(decision === "approve" ? { finding_ids: chosen } : {}) },
        throwOnError: true,
      });
      setLoaded(data.analysis);
      if (decision === "reject") {
        toaster.create({ type: "info", title: t("troubleshoot.rejected") });
        return;
      }
      const spec = data.applied?.spec;
      if (!spec) {
        toaster.create({ type: "info", title: t("troubleshoot.approvedNoSpec") });
        return;
      }
      loadAsUnsaved(spec);
      const id = analysis.id;
      watchNextDeploy(spec.dag_id, (info) => {
        void troubleshootAnalysisApplied({
          path: { analysis_id: id },
          body: { deployed: true, detail: { etag: info.etag, pr_url: info.prUrl } },
        });
      });
      toaster.create({ type: "success", title: t("troubleshoot.applied"), description: t("troubleshoot.appliedHelp") });
      if (then === "deploy") {
        close();
        useLifecycle.getState().openDialog({ kind: "deploy", focus: "message" });
      }
    } catch (err) {
      toaster.create({ type: "error", title: t("troubleshoot.decisionFailed"), description: errorMessage(err) });
    } finally {
      setBusy(false);
    }
  };
  const decideApproval = async (approvalId: string, decisions: ToolDecision[]) => {
    const pausedId = analysis?.id;
    setLoaded(null);
    setSelected(null);
    await start(`/ai/approvals/${encodeURIComponent(approvalId)}/stream`, { decisions });
    // Refused (expired, decided elsewhere) or failed: show the stored state of the analysis.
    if (pausedId) {
      const { data } = await getTroubleshootAnalysis({ path: { analysis_id: pausedId } });
      if (data && data.status === "awaiting_approval") setLoaded(data);
    }
  };
  const openPrevious = async (id: string) => {
    try {
      const { data } = await getTroubleshootAnalysis({ path: { analysis_id: id }, throwOnError: true });
      setLoaded(data);
      setSelected(null);
      setPhase("run");
    } catch (err) {
      toaster.create({ type: "error", title: errorMessage(err) });
    }
  };

  const showResult = phase === "run" && !!analysis && !state.running;
  const canApprove = access.can("spec:write") && analysis?.status === "proposed" && !!analysis.proposed_spec;
  const footer =
    phase === "setup" ? (
      <>
        <Button size="sm" variant="ghost" onClick={close}>
          {t("common.close")}
        </Button>
        <Button size="sm" colorPalette="blue" disabled={!options.data} onClick={run} data-testid="troubleshoot-start">
          <LuPlay /> {rerunOf ? t("troubleshoot.rerun") : t("troubleshoot.start")}
        </Button>
      </>
    ) : state.running ? (
      <Button size="sm" variant="outline" colorPalette="red" onClick={cancel} data-testid="troubleshoot-cancel">
        <LuCircleStop /> {t("troubleshoot.cancel")}
      </Button>
    ) : (
      <>
        <Button size="sm" variant="ghost" onClick={rerun} data-testid="troubleshoot-rerun">
          <LuRefreshCw /> {t("troubleshoot.rerunScope")}
        </Button>
        {analysis?.status === "proposed" ? (
          <Button
            size="sm"
            variant="ghost"
            loading={busy}
            onClick={() => void decide("reject")}
            data-testid="troubleshoot-reject"
          >
            <LuX /> {t("troubleshoot.reject")}
          </Button>
        ) : null}
        {analysis?.status === "proposed" && analysis.proposed_spec ? (
          <>
            <Button
              size="sm"
              variant="outline"
              disabled={!canApprove || !chosen.length}
              title={!access.can("spec:write") ? t("proposal.needsWrite") : undefined}
              loading={busy}
              onClick={() => void decide("approve", "editor")}
              data-testid="troubleshoot-approve"
            >
              <LuSquarePen /> {t("troubleshoot.approveEditor")}
            </Button>
            {access.can("deploy") ? (
              <Button
                size="sm"
                colorPalette="blue"
                disabled={!canApprove || !chosen.length}
                loading={busy}
                onClick={() => void decide("approve", "deploy")}
                data-testid="troubleshoot-approve-deploy"
              >
                <LuRocket /> {t("troubleshoot.approveDeploy")}
              </Button>
            ) : null}
          </>
        ) : null}
      </>
    );

  return (
    <DialogShell
      title={
        <HStack gap={2}>
          <LuFlaskConical />
          <span>{t("troubleshoot.title")}</span>
        </HStack>
      }
      description={
        <Text as="span" fontFamily="mono" fontSize="xs">
          {target.dagId} · {target.runId}
          {target.taskId ? ` · ${target.taskId}` : ""}
          {target.tryNumber ? ` · ${t("troubleshoot.try", { n: target.tryNumber })}` : ""}
        </Text>
      }
      size="xl"
      onClose={close}
      footer={footer}
    >
      <Box data-testid="troubleshoot-dialog">
        {phase === "setup" ? (
          options.isPending ? (
            <Spinner size="sm" />
          ) : options.isError ? (
            <Text color="fg.error">{errorMessage(options.error)}</Text>
          ) : options.data ? (
            <Stack gap={4}>
              <ScopeForm options={options.data} scope={effectiveScope} onScope={setScope} mcp={mcp} onMcp={setMcp} />
              {options.data.actions_mode && options.data.actions_mode !== "read_only" ? (
                <ActionsModePicker
                  max={options.data.actions_mode}
                  value={clampMode(actionsMode, options.data.actions_mode)}
                  onChange={setActionsMode}
                />
              ) : null}
              <Field.Root>
                <Field.Label>{t("troubleshoot.question")}</Field.Label>
                <Textarea
                  size="sm"
                  rows={2}
                  value={question}
                  placeholder={t("troubleshoot.questionPlaceholder")}
                  onChange={(e) => setQuestion(e.target.value)}
                />
              </Field.Root>
              {previous.data?.length ? (
                <Box>
                  <Text fontSize="sm" fontWeight="semibold" mb={1}>
                    {t("troubleshoot.previous")}
                  </Text>
                  <Stack gap={1}>
                    {previous.data.slice(0, 5).map((a) => (
                      <HStack key={a.id} gap={2} fontSize="xs">
                        <Badge size="xs" variant="outline">
                          {t(`troubleshoot.status.${a.status}`)}
                        </Badge>
                        <Text truncate flex="1" title={a.summary}>
                          {a.run_id} — {a.summary || "—"}
                        </Text>
                        <Button size="2xs" variant="ghost" onClick={() => void openPrevious(a.id)}>
                          {t("troubleshoot.open")}
                        </Button>
                      </HStack>
                    ))}
                  </Stack>
                </Box>
              ) : null}
            </Stack>
          ) : null
        ) : (
          <Stack gap={4}>
            {state.running || (!analysis && (state.error || state.cancelled || state.interrupted)) ? (
              <AiStreamProgress state={state} showText={false} />
            ) : null}
            {showResult && analysis ? (
              <AnalysisReport
                analysis={analysis}
                selected={chosen}
                onSelected={setSelected}
                onUpdate={setLoaded}
                onApproval={(id, d) => void decideApproval(id, d)}
              />
            ) : null}
          </Stack>
        )}
      </Box>
    </DialogShell>
  );
}
