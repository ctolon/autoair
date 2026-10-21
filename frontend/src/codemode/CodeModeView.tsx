import { Alert, Badge, Box, Button, Flex, HStack, Spinner, Stack, Text } from "@chakra-ui/react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { LuCheck, LuClipboard, LuDownload, LuFileCode, LuRotateCcw, LuTriangleAlert, LuUpload } from "react-icons/lu";

import type { CodeProblem } from "../api/gen/types.gen";
import type { LiveState } from "../hooks/useLiveChecks";
import { newRawDag } from "../lifecycle/actions";
import { DiffSummary } from "../lifecycle/components/DiffSummary";
import { downloadText } from "../lifecycle/lib/download";
import { useLifecycle } from "../lifecycle/store";
import { useAccess } from "../lib/access";
import { formatShortcut } from "../lib/commands";
import { useBuilder } from "../store/builder";
import { toaster } from "../ui/toaster";
import { CodeEditor } from "../widgets/code";
import { PreviewBadge, PreviewEnvSelect, PreviewHint, useEnvPreviewCode, usePreviewEnv } from "./EnvPreview";
import { EjectDialog } from "./RawCodeView";
import { editorAvailability, isStale, useCodeMode } from "./store";
import { applyDraft, checkDraft, diffCounts, toDiagnostics } from "./sync";

const MODULE_LSP = { functionName: "", arguments: [], usesLoopContext: false, mode: "module" as const };

function ProblemList({ problems }: { problems: CodeProblem[] }) {
  const { t } = useTranslation("lifecycle");
  if (!problems.length) return null;
  return (
    <Stack gap={1} data-testid="code-problems">
      <Text fontSize="xs" fontWeight="semibold">
        {t("codeMode.problems", { count: problems.length })}
      </Text>
      {problems.map((p, i) => (
        <HStack key={i} align="start" gap={2} fontSize="xs">
          <Badge size="xs" colorPalette={p.severity === "error" ? "red" : "orange"} flexShrink={0}>
            {p.line ? t("codeMode.line", { line: p.line }) : p.severity}
          </Badge>
          <Text whiteSpace="pre-wrap" wordBreak="break-word" maxH="12em" overflowY="auto">
            {p.message}
          </Text>
        </HStack>
      ))}
    </Stack>
  );
}

/** Status + actions of a code draft (shared by the code mode and the inline Code tab). */
export function DraftActions({ generated, compact = false }: { generated: string | null; compact?: boolean }) {
  const { t } = useTranslation("lifecycle");
  const draft = useCodeMode((s) => s.draft);
  const base = useCodeMode((s) => s.base);
  const check = useCodeMode((s) => s.check);
  const applying = useCodeMode((s) => s.applying);
  const errors = (check.result?.problems ?? []).filter((p) => p.severity === "error").length;
  const fresh = check.source === draft;
  const c = diffCounts(check.result?.diff);
  return (
    <HStack gap={2} wrap="wrap" justify="flex-end">
      {draft === null ? (
        <Badge variant="outline">{t("codeMode.generated")}</Badge>
      ) : check.status === "checking" ? (
        <Badge variant="subtle">
          <Spinner size="xs" /> {t("codeMode.checking")}
        </Badge>
      ) : check.status === "error" && fresh ? (
        <Badge colorPalette="red" data-testid="code-status">
          <LuTriangleAlert /> {check.error ? t("codeMode.checkFailed") : t("codeMode.fixProblems", { count: errors })}
        </Badge>
      ) : check.status === "ok" && fresh ? (
        <Badge colorPalette="green" data-testid="code-status">
          <LuCheck /> {t("codeMode.ready", c)}
        </Badge>
      ) : (
        <Badge colorPalette="orange">{t("codeMode.edited")}</Badge>
      )}
      {isStale(draft, base, generated) && !compact ? (
        <Badge colorPalette="purple" title={t("codeMode.staleHelp")}>
          {t("codeMode.stale")}
        </Badge>
      ) : null}
      <Button
        size="xs"
        variant="ghost"
        disabled={draft === null}
        onClick={() => {
          if (draft !== null && !window.confirm(t("codeMode.confirmDiscard"))) return;
          useCodeMode.getState().discard();
        }}
      >
        <LuRotateCcw /> {t("codeMode.discard")}
      </Button>
      <Button
        size="xs"
        colorPalette="blue"
        disabled={draft === null || (fresh && check.status === "error" && !check.error)}
        loading={applying}
        title={`${t("codeMode.applyHelp")} (${formatShortcut("mod+enter")})`}
        data-testid="code-apply"
        onClick={() => void applyDraft()}
      >
        <LuUpload /> {t("codeMode.apply")}
      </Button>
    </HStack>
  );
}

/** Ctrl/⌘+Enter applies, while focus is in the code editor. */
function useApplyShortcut() {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Enter" || !(e.ctrlKey || e.metaKey)) return;
      if (!(e.target instanceof Element) || !e.target.closest("[data-code-mode-editor]")) return;
      e.preventDefault();
      void applyDraft();
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, []);
}

/**
 * Full-screen editor of the generated DAG file. Edits are imported back into the visual spec
 * (checked on the server after a pause, applied with "Apply"); what the importer cannot
 * convert is reported as problems and the draft is kept.
 */
export function CodeModeView({ live }: { live: LiveState }) {
  const { t } = useTranslation("lifecycle");
  const access = useAccess();
  const availability = editorAvailability(access);
  const { canEditCode } = availability;
  const dagId = useBuilder((s) => s.dag.dag_id);
  const draft = useCodeMode((s) => s.draft);
  const base = useCodeMode((s) => s.base);
  const check = useCodeMode((s) => s.check);
  const [eject, setEject] = useState(false);
  const empty = useBuilder((s) => s.nodes.length === 0);
  // The last generated code: the editor never goes blank while the DAG is momentarily invalid.
  const [lastGenerated, setLastGenerated] = useState(live.code);
  if (live.code && live.code !== lastGenerated) setLastGenerated(live.code);
  const generated = live.code ?? lastGenerated;
  // Read-only preview of an environment's code; editing always happens on the base.
  const previewEnv = usePreviewEnv();
  const preview = useEnvPreviewCode(previewEnv);
  const previewing = previewEnv !== null;
  const text = previewing ? (preview.code ?? "") : (draft ?? generated ?? "");
  const fresh = check.source === draft;
  const problems = fresh && !previewing ? (check.result?.problems ?? []) : [];
  useApplyShortcut();

  // Unapplied code is lost on reload: ask first.
  useEffect(() => {
    if (draft === null) return;
    const onUnload = (e: BeforeUnloadEvent) => e.preventDefault();
    window.addEventListener("beforeunload", onUnload);
    return () => window.removeEventListener("beforeunload", onUnload);
  }, [draft]);

  return (
    <Flex direction="column" h="100%" minH={0} data-testid="code-mode">
      <HStack px={3} py={1.5} gap={3} borderBottomWidth="1px" wrap="wrap" flexShrink={0}>
        <Text fontFamily="mono" fontSize="sm" truncate maxW="40%" title={`${dagId}.py`}>
          {dagId}.py
        </Text>
        {previewing ? (
          <PreviewBadge env={previewEnv} />
        ) : !canEditCode ? (
          <Badge variant="outline" title={t("codeMode.readOnlyHelp")}>
            {t("codeMode.readOnly")}
          </Badge>
        ) : null}
        <PreviewEnvSelect />
        <Box flex="1" />
        <Button
          size="xs"
          variant="ghost"
          onClick={() => {
            void navigator.clipboard.writeText(text);
            toaster.create({ type: "success", title: t("common.copied") });
          }}
        >
          <LuClipboard /> {t("common.copy")}
        </Button>
        <Button size="xs" variant="ghost" onClick={() => downloadText(`${dagId}.py`, text, "text/x-python")}>
          <LuDownload /> {t("common.download")}
        </Button>
        {availability.both && availability.canRaw && !previewing ? (
          <Button
            size="xs"
            variant="outline"
            title={t("raw.ejectHelp")}
            disabled={!(draft ?? generated)}
            onClick={() => setEject(true)}
            data-testid="eject-raw"
          >
            <LuFileCode /> {t("raw.eject")}
          </Button>
        ) : null}
        {canEditCode && !previewing ? <DraftActions generated={generated} /> : null}
      </HStack>
      {previewing ? (
        <Alert.Root status="info" size="sm" borderRadius={0} flexShrink={0}>
          <Alert.Indicator />
          <Alert.Content>
            <PreviewHint />
            {preview.error ? (
              <Text fontSize="xs" color="fg.error">
                {preview.error}
              </Text>
            ) : null}
          </Alert.Content>
          <Button size="2xs" variant="outline" onClick={() => useCodeMode.getState().setPreviewEnv(null)}>
            {t("codeMode.previewBack")}
          </Button>
        </Alert.Root>
      ) : null}
      {!previewing && isStale(draft, base, generated) ? (
        <Alert.Root status="info" size="sm" borderRadius={0} flexShrink={0}>
          <Alert.Indicator />
          <Alert.Content>
            <Alert.Description fontSize="xs">{t("codeMode.staleHelp")}</Alert.Description>
          </Alert.Content>
          <Button size="2xs" variant="outline" onClick={() => useCodeMode.getState().discard()}>
            {t("codeMode.reload")}
          </Button>
        </Alert.Root>
      ) : null}
      {empty && draft === null && !previewing ? (
        <Alert.Root status="info" size="sm" borderRadius={0} flexShrink={0} data-testid="code-empty">
          <Alert.Indicator />
          <Alert.Content>
            <Alert.Description fontSize="xs">
              {canEditCode ? t("codeMode.emptyHelp") : t("codeMode.emptyReadOnly")}
            </Alert.Description>
          </Alert.Content>
          <HStack gap={1}>
            {access.feature("templates") ? (
              <Button
                size="2xs"
                variant="outline"
                onClick={() => useLifecycle.getState().openDialog({ kind: "templates" })}
              >
                {t("menu.newFromTemplate")}
              </Button>
            ) : null}
            {availability.canRaw ? (
              <Button size="2xs" variant="outline" onClick={newRawDag}>
                <LuFileCode /> {t("menu.newRaw")}
              </Button>
            ) : null}
          </HStack>
        </Alert.Root>
      ) : null}
      {!previewing && !generated && !draft ? (
        <Text fontSize="sm" color="fg.muted" px={3} py={2}>
          {live.checking ? t("codeMode.generating") : (live.error ?? t("code.fixProblems"))}
        </Text>
      ) : null}
      {previewing && preview.loading && !preview.code ? (
        <Text fontSize="sm" color="fg.muted" px={3} py={2}>
          {t("codeMode.generating")}
        </Text>
      ) : null}
      <Flex flex="1" minH={0} direction={{ base: "column", lg: "row" }}>
        <Box flex="1" minW={0} minH="240px" data-code-mode-editor="">
          <CodeEditor
            value={text}
            onChange={(v) => canEditCode && !previewing && useCodeMode.getState().edit(v, generated)}
            language="python"
            ariaLabel={t("codeMode.editorLabel", { dagId })}
            readOnly={!canEditCode || previewing}
            noExpand
            height="100%"
            diagnostics={toDiagnostics(problems)}
            pythonLsp={MODULE_LSP}
          />
        </Box>
        {draft !== null && !previewing && (problems.length || check.result?.diff || check.error) ? (
          <Box
            w={{ base: "100%", lg: "360px" }}
            maxH={{ base: "40%", lg: "none" }}
            flexShrink={0}
            borderLeftWidth={{ base: 0, lg: "1px" }}
            borderTopWidth={{ base: "1px", lg: 0 }}
            overflowY="auto"
            p={3}
            data-testid="code-sync-panel"
          >
            <Stack gap={3}>
              {check.error && fresh ? (
                <Text fontSize="xs" color="fg.error">
                  {check.error}
                </Text>
              ) : null}
              <ProblemList problems={problems} />
              {fresh && check.result?.diff ? (
                <Box>
                  <Text fontSize="xs" fontWeight="semibold" mb={1}>
                    {t("codeMode.willChange")}
                  </Text>
                  <DiffSummary diff={check.result.diff} />
                </Box>
              ) : null}
              {!fresh && check.status !== "checking" ? (
                <Button size="xs" variant="outline" onClick={() => void checkDraft()}>
                  {t("codeMode.checkNow")}
                </Button>
              ) : null}
            </Stack>
          </Box>
        ) : null}
      </Flex>
      {eject ? <EjectDialog code={draft ?? generated ?? ""} onClose={() => setEject(false)} /> : null}
    </Flex>
  );
}
