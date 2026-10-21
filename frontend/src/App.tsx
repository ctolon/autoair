import "@xyflow/react/dist/style.css";

import { Box, Flex, Text } from "@chakra-ui/react";
import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { ReactFlowProvider } from "@xyflow/react";
import { useEffect, useMemo, useState } from "react";
import { I18nextProvider, useTranslation } from "react-i18next";

import { getMetaOptions } from "./api/gen/@tanstack/react-query.gen";
import { configureClient, errorMessage } from "./api/setup";
import { CodeModeView } from "./codemode/CodeModeView";
import { DraftKeptBanner, useEditorModeWatch } from "./codemode/modeWatch";
import { RawCodeView } from "./codemode/RawCodeView";
import { editorAvailability, effectiveMode, useCodeMode, useIsRaw } from "./codemode/store";
import { startDraftChecks } from "./codemode/sync";
import { useAccess } from "./lib/access";
import { i18n } from "./i18n";
import { undo } from "./canvas/actions";
import { FlowCanvas } from "./canvas/FlowCanvas";
import { useDraftProtection } from "./hooks/useDraft";
import { useLiveChecks } from "./hooks/useLiveChecks";
import { LifecycleHost } from "./lifecycle/LifecycleHost";
import { BottomPanel } from "./panels/BottomPanel";
import { Inspector } from "./panels/Inspector";
import { Palette } from "./panels/Palette";
import { Toolbar } from "./panels/Toolbar";
import { LoginGate } from "./shell/LoginGate";
import { PlatformShell } from "./shell/PlatformShell";
import { ShellOverlays } from "./shell/ShellOverlays";
import { Workbench } from "./shell/Workbench";
import { useBuilder } from "./store/builder";
import { Toaster, toaster } from "./ui/toaster";
import { AirflowDatalists } from "./widgets/AirflowPickers";
import { ErrorBoundary } from "./ui/ErrorBoundary";

configureClient();

const NATIVE_COLOR_SCHEME = { colorScheme: "light", _dark: { colorScheme: "dark" } } as const;

const undoLastChange = () => undo();

function Builder() {
  const live = useLiveChecks();
  useDraftProtection();
  const lastError = useBuilder((s) => s.lastError);
  const selectedId = useBuilder((s) => s.selectedId);
  const availability = editorAvailability(useAccess());
  const mode = effectiveMode(
    useCodeMode((s) => s.mode),
    availability,
  );
  const raw = useIsRaw();
  useEditorModeWatch(availability.visual, availability.code);
  useEffect(() => startDraftChecks(), []);
  // Stable panel elements: dragging a splitter re-renders Builder on every frame, but React
  // skips children whose element is unchanged.
  const paletteContent = useMemo(
    () => (
      <ErrorBoundary area="palette">
        <Palette />
      </ErrorBoundary>
    ),
    [],
  );
  const canvasContent = useMemo(
    () => (
      <ErrorBoundary area="canvas" onUndo={undoLastChange}>
        <FlowCanvas />
      </ErrorBoundary>
    ),
    [],
  );
  const bottomContent = useMemo(
    () => (
      <ErrorBoundary area="bottomPanel" onUndo={undoLastChange}>
        <BottomPanel live={live} />
      </ErrorBoundary>
    ),
    [live],
  );
  const inspectorContent = useMemo(
    () => (
      <ErrorBoundary area="inspector" resetKey={selectedId} onUndo={undoLastChange}>
        <Inspector />
      </ErrorBoundary>
    ),
    [selectedId],
  );
  useEffect(() => {
    if (!lastError) return;
    toaster.create({ type: "warning", title: lastError });
    useBuilder.getState().setError(null);
  }, [lastError]);

  return (
    // color-scheme makes native controls (date pickers, scrollbars) follow the theme.
    <Flex direction="column" h="100%" minH="480px" minW={0} overflow="hidden" css={NATIVE_COLOR_SCHEME}>
      <ShellOverlays />
      <PlatformShell />
      <ErrorBoundary area="toolbar">
        <Toolbar />
      </ErrorBoundary>
      <LifecycleHost />
      <DraftKeptBanner />
      <Workbench
        palette={paletteContent}
        canvas={
          <>
            {/* The canvas stays mounted in code mode (viewport, selection) but takes no space. */}
            {availability.visual ? (
              <Box h="100%" display={mode === "code" || raw ? "none" : undefined}>
                {canvasContent}
              </Box>
            ) : null}
            {raw ? (
              // A raw code DAG has no canvas: it is edited as a file in every mode.
              <ErrorBoundary area="codeMode">
                <RawCodeView />
              </ErrorBoundary>
            ) : mode === "code" ? (
              <ErrorBoundary area="codeMode">
                <CodeModeView live={live} />
              </ErrorBoundary>
            ) : null}
          </>
        }
        sidePanels={mode === "visual" && !raw}
        inspectorPanel={raw || mode === "visual"}
        bottom={bottomContent}
        inspector={inspectorContent}
      />
    </Flex>
  );
}

/** How often the UI re-reads `/api/meta` (runtime feature flags). */
const META_REFRESH_MS = 60_000;

function Gate() {
  const { t } = useTranslation();
  // Feature flags can change at runtime (admin panel): re-read /api/meta when the window regains
  // focus and every minute. A failed background re-read keeps the last good value.
  const meta = useQuery({
    ...getMetaOptions(),
    retry: false,
    staleTime: META_REFRESH_MS / 6,
    refetchOnWindowFocus: true,
    refetchInterval: META_REFRESH_MS,
  });
  if (meta.isPending) return null;
  if (meta.isError) {
    const msg = errorMessage(meta.error);
    if (/401|bearer|token|authenticated/i.test(msg)) return <LoginGate message={msg} />;
  }
  if (!meta.data) {
    const msg = errorMessage(meta.error);
    return (
      <Text p={6} color="red.500">
        {t("apiUnreachable", { message: msg })}
      </Text>
    );
  }
  return (
    <>
      <AirflowDatalists />
      <ErrorBoundary area="builder">
        <Builder />
      </ErrorBoundary>
    </>
  );
}

const queryClient = new QueryClient({
  defaultOptions: { queries: { staleTime: 30_000, refetchOnWindowFocus: false } },
});

/** Root component shared by the Airflow plugin and the standalone page. */
export function App() {
  const [lang, setLang] = useState(i18n.language);
  useEffect(() => {
    const onChange = (lng: string) => setLang(lng);
    i18n.on("languageChanged", onChange);
    return () => i18n.off("languageChanged", onChange);
  }, []);
  return (
    <I18nextProvider i18n={i18n}>
      <QueryClientProvider client={queryClient}>
        <ReactFlowProvider>
          <Box
            h="100%"
            w="100%"
            // Native date/time pickers and hyphenation follow the UI language.
            lang={lang}
            bg="bg"
            color="fg"
            // Chakra keeps lazily-mounted inactive tab panels in the DOM (data-state=closed):
            // never let them paint over the active panel.
            css={{ "& [data-scope=tabs][data-part=content][data-state=closed]": { display: "none" } }}
          >
            <Gate />
          </Box>
          <Toaster />
        </ReactFlowProvider>
      </QueryClientProvider>
    </I18nextProvider>
  );
}
