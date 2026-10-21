import { Alert, Badge, Box, Button, HStack, Spinner, Stack, Tabs, Text } from "@chakra-ui/react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { LuClipboard, LuDownload, LuRefreshCw, LuUpload } from "react-icons/lu";

import { exportDagFactory, exportYamlDag, importYamlDag } from "../../api/gen/sdk.gen";
import type { DagSpec } from "../../api/gen/types.gen";
import { errorMessage } from "../../api/setup";
import type { LiveState } from "../../hooks/useLiveChecks";
import { useBuilder } from "../../store/builder";
import { toSpec } from "../../store/convert";
import { toaster } from "../../ui/toaster";
import { layoutCanvas } from "../actions";
import CodeView from "../components/CodeView";
import { DraftActions } from "../../codemode/CodeModeView";
import {
  PreviewBadge,
  PreviewEnvSelect,
  PreviewHint,
  useEnvPreviewCode,
  usePreviewEnv,
} from "../../codemode/EnvPreview";
import { usePreservedFile } from "../../codemode/preserved";
import { editorAvailability, useCodeMode, useIsRaw } from "../../codemode/store";
import { useAccess } from "../../lib/access";
import { useMeta } from "../hooks";
import { downloadText } from "../lib/download";
import { keepPositions } from "../lib/positions";

function CopyDownload({ text, filename, mime }: { text: string; filename: string; mime: string }) {
  const { t } = useTranslation("lifecycle");
  return (
    <HStack gap={1}>
      <Button
        size="xs"
        variant="outline"
        onClick={() => {
          void navigator.clipboard.writeText(text);
          toaster.create({ type: "success", title: t("common.copied") });
        }}
      >
        <LuClipboard /> {t("common.copy")}
      </Button>
      <Button size="xs" variant="outline" onClick={() => downloadText(filename, text, mime)}>
        <LuDownload /> {t("common.download")}
      </Button>
    </HStack>
  );
}

function PythonView({ live }: { live: LiveState }) {
  const { t } = useTranslation("lifecycle");
  const dagId = useBuilder((s) => s.dag.dag_id);
  const access = useAccess();
  // AUTOAIR_FEATURES__CODE_EDIT_INLINE: the generated file is editable here too (same draft
  // and import-on-apply as the full-screen code mode).
  const raw = useIsRaw();
  // Raw code is edited in the main area; here it is shown as deployed.
  const editable = !raw && access.feature("code_edit_inline") && editorAvailability(access).canEditCode;
  const draft = useCodeMode((s) => s.draft);
  const check = useCodeMode((s) => s.check);
  const previewEnv = usePreviewEnv();
  const preview = useEnvPreviewCode(previewEnv);
  const previewing = previewEnv !== null;
  // A DAG that keeps its hand-written file shows that file, not generated code.
  const preserved = usePreservedFile();
  const fileCode = preserved.active ? preserved.code : live.code;
  const text = previewing ? preview.code : raw ? live.code : (draft ?? fileCode);
  const header = (
    <HStack gap={2}>
      {previewing ? <PreviewBadge env={previewEnv} /> : null}
      <PreviewEnvSelect />
    </HStack>
  );
  if ((live.checking || (previewing && preview.loading) || preserved.loading) && !text)
    return <Spinner m={3} size="sm" />;
  if (!text) {
    return (
      <Stack gap={2} p={3}>
        {header}
        <Text fontSize="sm" color="fg.muted">
          {(previewing ? preview.error : (preserved.error ?? live.error)) ?? t("code.fixProblems")}
        </Text>
      </Stack>
    );
  }
  const problems = editable && !previewing && check.source === draft ? (check.result?.problems ?? []) : [];
  const canEdit = editable && !previewing && !(preserved.active && fileCode === null);
  // The kept hand-written file is shown (not generated code), unless an environment preview is.
  const keptFile = !previewing && preserved.active && fileCode !== null;
  return (
    <Stack gap={2} h="100%" p={2}>
      <HStack justify="space-between" wrap="wrap">
        <HStack gap={2} wrap="wrap">
          {header}
          <Text fontSize="xs" color="fg.muted" data-testid="python-caption">
            {raw
              ? t("code.rawHelp")
              : keptFile
                ? t(canEdit ? "code.pythonKeptEditHelp" : "code.pythonKeptHelp")
                : t(canEdit ? "code.pythonEditHelp" : "code.pythonHelp")}
          </Text>
        </HStack>
        <HStack gap={2}>
          {canEdit ? <DraftActions generated={fileCode} compact handWritten={preserved.active} /> : null}
          <CopyDownload text={text} filename={`${dagId}.py`} mime="text/x-python" />
        </HStack>
      </HStack>
      {previewing ? <PreviewHint /> : null}
      {problems.length ? (
        <Text fontSize="xs" color="fg.error" whiteSpace="pre-wrap" data-testid="inline-code-problems">
          {problems
            .map((p) => (p.line ? `${t("codeMode.line", { line: p.line })}: ${p.message}` : p.message))
            .join("\n")}
        </Text>
      ) : null}
      <Box flex="1" minH="200px">
        <CodeView
          value={text}
          language="python"
          readOnly={!canEdit}
          {...(canEdit ? { onChange: (v: string) => useCodeMode.getState().edit(v, fileCode) } : {})}
          label={t("code.python")}
        />
      </Box>
    </Stack>
  );
}

/** Snapshot of the document used as the source of a generated view (refreshable). */
function useDocSnapshot(): [DagSpec, () => void] {
  const [spec, setSpec] = useState<DagSpec>(() => useBuilder.getState().toSpec());
  return [spec, () => setSpec(useBuilder.getState().toSpec())];
}

function YamlView() {
  const { t } = useTranslation("lifecycle");
  const meta = useMeta();
  const [spec, refresh] = useDocSnapshot();
  const exp = useQuery({
    queryKey: ["lifecycle", "yaml-export", spec],
    queryFn: async ({ signal }) => (await exportYamlDag({ body: spec, signal, throwOnError: true })).data,
    staleTime: Infinity,
  });
  const [draft, setDraft] = useState<string | null>(null);
  const text = draft ?? exp.data?.yaml ?? "";
  const apply = useMutation({
    mutationFn: async (source: string) => (await importYamlDag({ body: { source }, throwOnError: true })).data,
    onSuccess: (res) => {
      const { spec: next, matched } = keepPositions(res.spec, useBuilder.getState().toSpec());
      // One undoable step (the canvas keeps its saved identity).
      useBuilder.getState().replaceSpec(next);
      setDraft(null);
      refresh();
      if (matched < (next.nodes?.length ?? 0)) requestAnimationFrame(() => void layoutCanvas());
      toaster.create({
        type: res.issues.length ? "warning" : "success",
        title: t("code.yamlApplied"),
        description: res.issues.map((i) => `• ${i.message}`).join("\n") || undefined,
      });
    },
    onError: (e) => toaster.create({ type: "error", title: t("code.yamlApplyFailed"), description: errorMessage(e) }),
  });
  return (
    <Stack gap={2} h="100%" p={2}>
      <HStack justify="space-between" wrap="wrap">
        <HStack gap={2}>
          <Badge variant="outline">{t("code.yamlTarget", { version: meta?.target_airflow_version ?? "3.4" })}</Badge>
          {draft !== null ? <Badge colorPalette="orange">{t("code.edited")}</Badge> : null}
        </HStack>
        <HStack gap={1}>
          <Button
            size="xs"
            variant="ghost"
            onClick={() => {
              setDraft(null);
              refresh();
            }}
          >
            <LuRefreshCw /> {t("code.refresh")}
          </Button>
          <Button
            size="xs"
            colorPalette="blue"
            disabled={draft === null || !draft.trim()}
            loading={apply.isPending}
            onClick={() => draft && apply.mutate(draft)}
          >
            <LuUpload /> {t("code.applyYaml")}
          </Button>
          <CopyDownload text={text} filename={`${spec.dag_id}.yaml`} mime="application/yaml" />
        </HStack>
      </HStack>
      {exp.isError ? (
        <Text fontSize="sm" color="red.500">
          {errorMessage(exp.error)}
        </Text>
      ) : null}
      {exp.data?.issues.length ? (
        <Alert.Root status="warning" size="sm">
          <Alert.Indicator />
          <Alert.Content>
            <Alert.Title fontSize="xs">{t("export.yamlIssues", { count: exp.data.issues.length })}</Alert.Title>
            <Alert.Description fontSize="xs">
              {exp.data.issues.map((i, n) => (
                <Text key={n}>• {i.message}</Text>
              ))}
            </Alert.Description>
          </Alert.Content>
        </Alert.Root>
      ) : null}
      {exp.isPending ? (
        <Spinner size="sm" />
      ) : (
        <Box flex="1" minH="200px">
          <CodeView value={text} language="yaml" onChange={setDraft} label={t("code.yaml")} />
        </Box>
      )}
    </Stack>
  );
}

function DagFactoryView() {
  const { t } = useTranslation("lifecycle");
  const [spec, refresh] = useDocSnapshot();
  const exp = useQuery({
    queryKey: ["lifecycle", "dag-factory", spec],
    queryFn: async ({ signal }) => (await exportDagFactory({ body: spec, signal, throwOnError: true })).data,
    staleTime: Infinity,
  });
  return (
    <Stack gap={2} h="100%" p={2}>
      <HStack justify="space-between" wrap="wrap">
        <Text fontSize="xs" color="fg.muted">
          {t("code.dagFactoryHelp")}
        </Text>
        <HStack gap={1}>
          <Button size="xs" variant="ghost" onClick={refresh}>
            <LuRefreshCw /> {t("code.refresh")}
          </Button>
          {exp.data ? (
            <CopyDownload text={exp.data.yaml} filename={`${spec.dag_id}.dag-factory.yaml`} mime="application/yaml" />
          ) : null}
        </HStack>
      </HStack>
      {exp.isError ? (
        <Text fontSize="sm" color="red.500">
          {errorMessage(exp.error)}
        </Text>
      ) : null}
      {exp.data?.warnings.map((w) => (
        <Text key={w} fontSize="xs" color="orange.500">
          • {w}
        </Text>
      ))}
      {exp.isPending ? (
        <Spinner size="sm" />
      ) : exp.data ? (
        <Box flex="1" minH="200px">
          <CodeView value={exp.data.yaml} language="yaml" readOnly label={t("code.dagFactory")} />
        </Box>
      ) : null}
    </Stack>
  );
}

function SpecJsonView() {
  const { t } = useTranslation("lifecycle");
  const dag = useBuilder((s) => s.dag);
  const nodes = useBuilder((s) => s.nodes);
  const edges = useBuilder((s) => s.edges);
  const text = useMemo(() => JSON.stringify(toSpec(dag, nodes, edges), null, 2), [dag, nodes, edges]);
  return (
    <Stack gap={2} h="100%" p={2}>
      <HStack justify="space-between">
        <Text fontSize="xs" color="fg.muted">
          {t("code.specHelp")}
        </Text>
        <CopyDownload text={text} filename={`${dag.dag_id}.autoair.json`} mime="application/json" />
      </HStack>
      <Box flex="1" minH="200px">
        <CodeView value={text} language="json" readOnly label={t("code.spec")} />
      </Box>
    </Stack>
  );
}

/** Generated artefacts: Python, Airflow YAML (editable round-trip), dag-factory, spec JSON. */
export default function CodeTab({ live }: { live: LiveState }) {
  const { t } = useTranslation("lifecycle");
  // Raw code DAGs have no YAML / dag-factory form: they are a Python file.
  const raw = useIsRaw();
  return (
    <Tabs.Root
      key={raw ? "raw" : "spec"}
      defaultValue="python"
      size="sm"
      variant="subtle"
      lazyMount
      unmountOnExit
      h="100%"
      display="flex"
      flexDirection="column"
    >
      <Tabs.List px={2} pt={1}>
        <Tabs.Trigger value="python">{t("code.python")}</Tabs.Trigger>
        {raw ? null : <Tabs.Trigger value="yaml">{t("code.yaml")}</Tabs.Trigger>}
        {raw ? null : <Tabs.Trigger value="dagfactory">{t("code.dagFactory")}</Tabs.Trigger>}
        <Tabs.Trigger value="spec">{t("code.spec")}</Tabs.Trigger>
      </Tabs.List>
      <Box flex="1" minH={0}>
        <Tabs.Content value="python" h="100%" p={0}>
          <PythonView live={live} />
        </Tabs.Content>
        <Tabs.Content value="yaml" h="100%" p={0}>
          <YamlView />
        </Tabs.Content>
        <Tabs.Content value="dagfactory" h="100%" p={0}>
          <DagFactoryView />
        </Tabs.Content>
        <Tabs.Content value="spec" h="100%" p={0}>
          <SpecJsonView />
        </Tabs.Content>
      </Box>
    </Tabs.Root>
  );
}
