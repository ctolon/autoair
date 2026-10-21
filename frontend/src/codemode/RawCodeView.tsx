import { Alert, Badge, Box, Button, Flex, HStack, Spinner, Stack, Text } from "@chakra-ui/react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { LuClipboard, LuDownload, LuFileCode, LuTriangleAlert, LuWorkflow } from "react-icons/lu";

import type { CodeSyncResult, Problem } from "../api/gen/types.gen";
import { errorMessage } from "../api/setup";
import { undo } from "../canvas/actions";
import { DialogShell } from "../lifecycle/components/DialogShell";
import { DiffSummary } from "../lifecycle/components/DiffSummary";
import { downloadText } from "../lifecycle/lib/download";
import { useAccess } from "../lib/access";
import { problemText } from "../lib/problemMessages";
import { useBuilder } from "../store/builder";
import { toaster } from "../ui/toaster";
import { CodeEditor, type CodeDiagnostic, type CodeEditorHandle } from "../widgets/code";
import { ejectedSpec } from "./rawDoc";
import { editorAvailability, useCodeMode } from "./store";
import { diffCounts, syncCode } from "./sync";

const MODULE_LSP = { functionName: "", arguments: [], usesLoopContext: false, mode: "module" as const };

/** Located problems of the raw file as editor diagnostics. */
function rawDiagnostics(problems: readonly Problem[]): CodeDiagnostic[] {
  return problems
    .filter((p) => !p.node_id && (p.field === "raw_code" || !p.field))
    .map((p) => ({
      line: p.line ?? 1,
      column: p.column ? p.column - 1 : null,
      message: problemText(p),
      severity: p.severity,
    }));
}

/** Badge marking a raw code DAG (toolbar, lists). */
export function RawBadge({ size = "sm" }: { size?: "xs" | "sm" }) {
  const { t } = useTranslation("lifecycle");
  return (
    <Badge size={size} colorPalette="teal" variant="subtle" title={t("raw.badgeHelp")} data-testid="raw-badge">
      <LuFileCode /> {t("raw.badge")}
    </Badge>
  );
}

/**
 * "Convert to raw code": the visual DAG becomes a raw code DAG whose file is the current
 * generated code (or the unapplied draft). One-way: the canvas and the code sync are gone
 * (only undo brings them back before the next save).
 */
export function EjectDialog({ code, onClose }: { code: string; onClose: () => void }) {
  const { t } = useTranslation("lifecycle");
  const [confirmed, setConfirmed] = useState(false);
  const eject = () => {
    const spec = useBuilder.getState().toSpec();
    useBuilder.getState().replaceSpec(ejectedSpec(spec, code));
    useCodeMode.getState().discard();
    useCodeMode.getState().setPreviewEnv(null);
    onClose();
    toaster.create({
      type: "success",
      title: t("raw.ejected"),
      description: t("raw.ejectedHelp"),
      action: { label: t("codeMode.undo"), onClick: () => undo() },
    });
  };
  return (
    <DialogShell
      title={t("raw.ejectTitle")}
      description={t("raw.ejectDescription")}
      size="md"
      onClose={onClose}
      footer={
        <HStack gap={2}>
          <Button size="sm" variant="ghost" onClick={onClose}>
            {t("common.cancel")}
          </Button>
          <Button size="sm" colorPalette="orange" disabled={!confirmed} onClick={eject} data-testid="eject-confirm">
            <LuFileCode /> {t("raw.ejectAction")}
          </Button>
        </HStack>
      }
    >
      <Stack gap={3}>
        <Alert.Root status="warning" size="sm">
          <Alert.Indicator />
          <Alert.Content>
            <Alert.Title fontSize="sm">{t("raw.oneWay")}</Alert.Title>
            <Alert.Description fontSize="xs">{t("raw.oneWayHelp")}</Alert.Description>
          </Alert.Content>
        </Alert.Root>
        <Box as="label" display="flex" gap={2} alignItems="center" fontSize="sm">
          <input
            type="checkbox"
            checked={confirmed}
            onChange={(e) => setConfirmed(e.target.checked)}
            data-testid="eject-understood"
          />
          {t("raw.ejectUnderstood")}
        </Box>
      </Stack>
    </DialogShell>
  );
}

/**
 * "Open in visual builder": imports the raw file with the Python importer (best effort) and
 * shows what the visual DAG would be before replacing the document (undoable).
 */
function ConvertDialog({ onClose }: { onClose: () => void }) {
  const { t } = useTranslation("lifecycle");
  const code = useBuilder((s) => s.dag.raw_code ?? "");
  const [state, setState] = useState<{ result: CodeSyncResult | null; error: string | null; busy: boolean }>({
    result: null,
    error: null,
    busy: true,
  });
  useEffect(() => {
    const ctrl = new AbortController();
    syncCode(code, ctrl.signal)
      .then((result) => !ctrl.signal.aborted && setState({ result, error: null, busy: false }))
      .catch((e: unknown) => !ctrl.signal.aborted && setState({ result: null, error: errorMessage(e), busy: false }));
    return () => ctrl.abort();
  }, [code]);
  const result = state.result;
  const convert = () => {
    if (!result?.spec) return;
    useBuilder.getState().replaceSpec(result.spec);
    useCodeMode.getState().setMode("visual");
    onClose();
    toaster.create({
      type: "success",
      title: t("raw.converted"),
      description: t("codeMode.appliedSummary", diffCounts(result.diff)),
      action: { label: t("codeMode.undo"), onClick: () => undo() },
    });
  };
  return (
    <DialogShell
      title={t("raw.convertTitle")}
      description={t("raw.convertDescription")}
      size="lg"
      onClose={onClose}
      footer={
        <HStack gap={2}>
          <Button size="sm" variant="ghost" onClick={onClose}>
            {t("common.cancel")}
          </Button>
          <Button
            size="sm"
            colorPalette="blue"
            disabled={!result?.ok || !result.spec}
            onClick={convert}
            data-testid="convert-confirm"
          >
            <LuWorkflow /> {t("raw.convertAction")}
          </Button>
        </HStack>
      }
    >
      <Stack gap={3} data-testid="convert-review">
        {state.busy ? (
          <HStack gap={2}>
            <Spinner size="sm" /> <Text fontSize="sm">{t("raw.converting")}</Text>
          </HStack>
        ) : null}
        {state.error ? (
          <Text fontSize="sm" color="fg.error">
            {state.error}
          </Text>
        ) : null}
        {result?.problems?.length ? (
          <Stack gap={1}>
            {result.problems.map((p, i) => (
              <HStack key={i} align="start" gap={2} fontSize="xs">
                <Badge size="xs" colorPalette={p.severity === "error" ? "red" : "orange"} flexShrink={0}>
                  {p.line ? t("codeMode.line", { line: p.line }) : p.severity}
                </Badge>
                <Text whiteSpace="pre-wrap" wordBreak="break-word">
                  {p.message}
                </Text>
              </HStack>
            ))}
          </Stack>
        ) : null}
        {result?.diff ? (
          <Box>
            <Text fontSize="xs" fontWeight="semibold" mb={1}>
              {t("raw.convertWillChange")}
            </Text>
            <DiffSummary diff={result.diff} />
          </Box>
        ) : null}
        {result && !state.busy ? (
          <Text fontSize="xs" color="fg.muted">
            {t("raw.convertBestEffort")}
          </Text>
        ) : null}
      </Stack>
    </DialogShell>
  );
}

/**
 * Editor of a raw code DAG: the whole DAG file, deployed as written. Problems (syntax, lint,
 * secrets, policy, import) come from the live checks and show as diagnostics.
 */
export function RawCodeView() {
  const { t } = useTranslation("lifecycle");
  const access = useAccess();
  const availability = editorAvailability(access);
  const dagId = useBuilder((s) => s.dag.dag_id);
  const code = useBuilder((s) => s.dag.raw_code ?? "");
  const problems = useBuilder((s) => s.problems);
  const reveal = useCodeMode((s) => s.reveal);
  const [convert, setConvert] = useState(false);
  const handle = useRef<CodeEditorHandle | null>(null);
  // Code editing switched off (visual-only deployment) or not allowed: read-only, kept.
  const codeOff = !access.feature("code_editor");
  const editable = availability.canRaw;
  const diagnostics = useMemo(() => rawDiagnostics(problems), [problems]);
  const errors = problems.filter((p) => p.severity === "error").length;

  useEffect(() => {
    if (!reveal || !handle.current) return;
    handle.current.edit((doc) => {
      let pos = 0;
      for (let line = 1; line < reveal.line && pos >= 0; line++) {
        const next = doc.indexOf("\n", pos);
        pos = next < 0 ? doc.length : next + 1;
      }
      pos = Math.min(doc.length, pos + Math.max(0, (reveal.column ?? 1) - 1));
      return { from: pos, to: pos, insert: "", selectionFrom: pos, selectionTo: pos };
    });
    handle.current.focus();
  }, [reveal]);

  return (
    <Flex direction="column" h="100%" minH={0} data-testid="raw-code-mode">
      <HStack px={3} py={1.5} gap={3} borderBottomWidth="1px" wrap="wrap" flexShrink={0}>
        <Text fontFamily="mono" fontSize="sm" truncate maxW="40%" title={`${dagId}.py`}>
          {dagId}.py
        </Text>
        <RawBadge />
        {!editable ? (
          <Badge variant="outline" title={codeOff ? t("raw.codeOffHelp") : t("raw.readOnlyHelp")}>
            {t("codeMode.readOnly")}
          </Badge>
        ) : null}
        {errors ? (
          <Badge colorPalette="red" data-testid="raw-errors">
            <LuTriangleAlert /> {t("raw.errors", { count: errors })}
          </Badge>
        ) : null}
        <Box flex="1" />
        <Text fontSize="xs" color="fg.muted" title={t("raw.noEnvPreviewHelp")}>
          {t("raw.noEnvPreview")}
        </Text>
        <Button
          size="xs"
          variant="ghost"
          onClick={() => {
            void navigator.clipboard.writeText(code);
            toaster.create({ type: "success", title: t("common.copied") });
          }}
        >
          <LuClipboard /> {t("common.copy")}
        </Button>
        <Button size="xs" variant="ghost" onClick={() => downloadText(`${dagId}.py`, code, "text/x-python")}>
          <LuDownload /> {t("common.download")}
        </Button>
        {availability.visual && availability.canEditCode && !codeOff ? (
          <Button
            size="xs"
            variant="outline"
            onClick={() => setConvert(true)}
            title={t("raw.convertHelp")}
            data-testid="raw-to-visual"
          >
            <LuWorkflow /> {t("raw.convert")}
          </Button>
        ) : null}
      </HStack>
      {codeOff ? (
        <Alert.Root status="warning" size="sm" borderRadius={0} flexShrink={0} data-testid="raw-code-off">
          <Alert.Indicator />
          <Alert.Content>
            <Alert.Description fontSize="xs">{t("raw.codeOffHelp")}</Alert.Description>
          </Alert.Content>
        </Alert.Root>
      ) : null}
      <Box flex="1" minH="240px" data-code-mode-editor="">
        <CodeEditor
          value={code}
          onChange={(v) => {
            if (editable) useBuilder.getState().setDag({ raw_code: v });
          }}
          onReady={(h) => {
            handle.current = h;
          }}
          language="python"
          ariaLabel={t("raw.editorLabel", { dagId })}
          readOnly={!editable}
          noExpand
          height="100%"
          diagnostics={diagnostics}
          pythonLsp={MODULE_LSP}
        />
      </Box>
      {convert ? <ConvertDialog onClose={() => setConvert(false)} /> : null}
    </Flex>
  );
}
