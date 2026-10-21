import { Alert, Badge, Box, Button, Field, HStack, Spinner, Stack, Text, Textarea } from "@chakra-ui/react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { LuCheck, LuSparkles, LuWandSparkles } from "react-icons/lu";

import { generateDagMutation } from "../../api/gen/@tanstack/react-query.gen";
import { diffVersions, explainProblems } from "../../api/gen/sdk.gen";
import type { DagSpec, GenerationResult } from "../../api/gen/types.gen";
import { errorMessage } from "../../api/setup";
import { useBuilder } from "../../store/builder";
import { toaster } from "../../ui/toaster";
import { confirmDiscard, layoutCanvas, loadAsUnsaved } from "../actions";
import { DialogShell } from "../components/DialogShell";
import { DiffSummary } from "../components/DiffSummary";
import { useMeta } from "../hooks";
import { syncDeepLink } from "../lib/deepLink";
import { useLifecycle } from "../store";

const EXAMPLE_KEYS = ["etl", "sensor", "mapped", "dbt"] as const;
const MAX_REPAIRS = 3;

function GenerationReport({ result }: { result: GenerationResult }) {
  const { t } = useTranslation("lifecycle");
  const tasks = (result.spec?.nodes ?? []).filter((n) => n.kind !== "task_group" && n.kind !== "note").length;
  return (
    <Stack gap={3} aria-live="polite">
      <HStack gap={2} wrap="wrap">
        <Badge colorPalette={result.ok ? "green" : "orange"}>{result.ok ? t("ai.valid") : t("ai.invalid")}</Badge>
        <Badge variant="outline">{t("ai.attempts", { count: result.attempts })}</Badge>
        {result.spec ? (
          <Badge variant="outline">{t("ai.graphSummary", { tasks, edges: result.spec.edges?.length ?? 0 })}</Badge>
        ) : null}
        {result.model ? (
          <Text fontSize="xs" color="fg.muted">
            {result.model}
          </Text>
        ) : null}
      </HStack>
      {result.explanation ? (
        <Text fontSize="sm" whiteSpace="pre-wrap">
          {result.explanation}
        </Text>
      ) : null}
      {result.selected_operators?.length ? (
        <Text fontSize="xs" color="fg.muted">
          {t("ai.operators", { list: result.selected_operators.map((o) => o.split(".").pop()).join(", ") })}
        </Text>
      ) : null}
      {result.problems?.length ? (
        <Stack gap={1}>
          {result.problems.map((p, i) => (
            <Text key={i} fontSize="xs" color={p.severity === "error" ? "red.500" : "orange.500"}>
              {p.severity}: {p.message}
            </Text>
          ))}
        </Stack>
      ) : null}
    </Stack>
  );
}

function Pending() {
  const { t } = useTranslation("lifecycle");
  return (
    <HStack role="status" gap={2}>
      <Spinner size="sm" />
      <Text fontSize="sm">{t("ai.working", { max: MAX_REPAIRS + 1 })}</Text>
    </HStack>
  );
}

/** Natural language → new DAG. */
export function AiGenerateDialog() {
  const { t } = useTranslation("lifecycle");
  const close = useLifecycle((s) => s.closeDialog);
  const [prompt, setPrompt] = useState("");
  const gen = useMutation({
    ...generateDagMutation(),
    onError: (e) => toaster.create({ type: "error", title: t("ai.failed"), description: errorMessage(e) }),
  });
  const spec = gen.data?.spec ?? null;
  return (
    <DialogShell
      title={t("ai.generateTitle")}
      description={t("ai.generateDescription")}
      size="lg"
      footer={
        <>
          <Button
            size="sm"
            variant="outline"
            disabled={!prompt.trim()}
            loading={gen.isPending}
            onClick={() => gen.mutate({ body: { prompt: prompt.trim(), max_repairs: MAX_REPAIRS } })}
          >
            <LuSparkles /> {gen.data ? t("ai.regenerate") : t("ai.generate")}
          </Button>
          <Button
            size="sm"
            colorPalette="blue"
            disabled={!spec}
            onClick={() => {
              if (!spec || !confirmDiscard()) return;
              loadAsUnsaved(spec, { keepIdentity: false });
              useLifecycle.getState().setSelectedRunId(null);
              syncDeepLink(null);
              close();
              requestAnimationFrame(() => void layoutCanvas());
            }}
          >
            <LuCheck /> {t("ai.openOnCanvas")}
          </Button>
        </>
      }
    >
      <Stack gap={3}>
        <Field.Root>
          <Field.Label>{t("ai.prompt")}</Field.Label>
          <Textarea
            autoFocus
            rows={5}
            value={prompt}
            placeholder={t("ai.promptPlaceholder")}
            onChange={(e) => setPrompt(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && prompt.trim()) {
                gen.mutate({ body: { prompt: prompt.trim(), max_repairs: MAX_REPAIRS } });
              }
            }}
          />
        </Field.Root>
        <HStack gap={1} wrap="wrap">
          <Text fontSize="xs" color="fg.muted">
            {t("ai.examples")}
          </Text>
          {EXAMPLE_KEYS.map((k) => (
            <Button key={k} size="2xs" variant="subtle" onClick={() => setPrompt(t(`ai.example.${k}`))}>
              {t(`ai.exampleLabel.${k}`)}
            </Button>
          ))}
        </HStack>
        {gen.isPending ? <Pending /> : null}
        {gen.data ? <GenerationReport result={gen.data} /> : null}
      </Stack>
    </DialogShell>
  );
}

/** Natural language edit of the current DAG, reviewed as a diff before applying. */
export function AiEditDialog() {
  const { t } = useTranslation("lifecycle");
  const close = useLifecycle((s) => s.closeDialog);
  const [prompt, setPrompt] = useState("");
  const [current, setCurrent] = useState<DagSpec>(() => useBuilder.getState().toSpec());
  const gen = useMutation({
    ...generateDagMutation(),
    onError: (e) => toaster.create({ type: "error", title: t("ai.failed"), description: errorMessage(e) }),
  });
  const proposed = gen.data?.spec ?? null;
  const diff = useQuery({
    queryKey: ["lifecycle", "ai-diff", current, proposed],
    enabled: !!proposed,
    queryFn: async ({ signal }) =>
      (await diffVersions({ body: { a: current, b: proposed as DagSpec }, signal, throwOnError: true })).data,
    staleTime: Infinity,
  });
  const run = () => {
    const spec = useBuilder.getState().toSpec();
    setCurrent(spec);
    gen.mutate({ body: { prompt: prompt.trim(), existing: spec, max_repairs: MAX_REPAIRS } });
  };
  return (
    <DialogShell
      title={t("ai.editTitle")}
      description={t("ai.editDescription")}
      size="lg"
      footer={
        <>
          <Button size="sm" variant="outline" disabled={!prompt.trim()} loading={gen.isPending} onClick={run}>
            <LuWandSparkles /> {t("ai.propose")}
          </Button>
          <Button
            size="sm"
            colorPalette="blue"
            disabled={!proposed || diff.data?.identical}
            onClick={() => {
              if (!proposed) return;
              loadAsUnsaved(proposed);
              const added = diff.data?.added_nodes?.length ?? 0;
              close();
              if (added) requestAnimationFrame(() => void layoutCanvas());
            }}
          >
            <LuCheck /> {t("ai.apply")}
          </Button>
        </>
      }
    >
      <Stack gap={3}>
        <Field.Root>
          <Field.Label>{t("ai.editPrompt")}</Field.Label>
          <Textarea
            autoFocus
            rows={4}
            value={prompt}
            placeholder={t("ai.editPlaceholder")}
            onChange={(e) => setPrompt(e.target.value)}
          />
        </Field.Root>
        {gen.isPending ? <Pending /> : null}
        {gen.data ? <GenerationReport result={gen.data} /> : null}
        {diff.isPending && proposed ? <Spinner size="sm" /> : null}
        {diff.data ? (
          <Box borderWidth="1px" rounded="md" p={3}>
            <Text fontWeight="semibold" fontSize="sm" mb={2}>
              {t("ai.proposedChanges")}
            </Text>
            <DiffSummary diff={diff.data} />
          </Box>
        ) : null}
      </Stack>
    </DialogShell>
  );
}

/** Plain-language explanation of the current problems. */
export function AiExplainDialog() {
  const { t } = useTranslation("lifecycle");
  const meta = useMeta();
  // Snapshot of what is explained (the document may keep changing underneath).
  const [input] = useState(() => ({ spec: useBuilder.getState().toSpec(), problems: useBuilder.getState().problems }));
  const problems = input.problems;
  const explain = useQuery({
    queryKey: ["lifecycle", "ai-explain", input],
    queryFn: async ({ signal }) => (await explainProblems({ body: input, signal, throwOnError: true })).data,
    staleTime: Infinity,
    retry: false,
  });
  return (
    <DialogShell title={t("ai.explainTitle")} description={meta?.ai_model ?? undefined} size="lg">
      {explain.isPending ? <Pending /> : null}
      {explain.isError ? (
        <Alert.Root status="error">
          <Alert.Indicator />
          <Alert.Description>{errorMessage(explain.error)}</Alert.Description>
        </Alert.Root>
      ) : null}
      {explain.data ? (
        <Text fontSize="sm" whiteSpace="pre-wrap" aria-live="polite">
          {explain.data.explanation}
        </Text>
      ) : null}
      {!problems.length ? (
        <Text fontSize="xs" color="fg.muted" mt={2}>
          {t("ai.noProblems")}
        </Text>
      ) : null}
    </DialogShell>
  );
}
