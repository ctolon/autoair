import {
  Badge,
  Box,
  Button,
  ButtonGroup,
  HStack,
  IconButton,
  Link,
  Menu,
  NativeSelect,
  Portal,
  Separator,
  Spinner,
  Text,
} from "@chakra-ui/react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type ReactNode, type RefObject, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  LuBookOpen,
  LuBookText,
  LuClipboardCheck,
  LuCopy,
  LuChevronDown,
  LuChevronRight,
  LuCloud,
  LuDatabase,
  LuDownload,
  LuExternalLink,
  LuFileCode,
  LuFileInput,
  LuFilePlus,
  LuFlaskConical,
  LuFolderGit2,
  LuFolderOpen,
  LuGauge,
  LuGitPullRequestArrow,
  LuHardDrive,
  LuHistory,
  LuKeyRound,
  LuLayoutDashboard,
  LuLayoutTemplate,
  LuLibrary,
  LuMessageCircleQuestion,
  LuPause,
  LuPlay,
  LuRedo2,
  LuRocket,
  LuShieldAlert,
  LuShieldCheck,
  LuSettings2,
  LuSparkles,
  LuSquareCode,
  LuTrash2,
  LuUndo2,
  LuWandSparkles,
  LuWrench,
} from "react-icons/lu";
import { useStore } from "zustand";

import {
  deleteSpecMutation,
  getDagStatusOptions,
  getDagStatusQueryKey,
  getDeployTargetOptions,
  listSpecsQueryKey,
  setDagPausedMutation,
} from "../api/gen/@tanstack/react-query.gen";
import { errorMessage } from "../api/setup";
import { AiHost } from "../ai/AiHost";
import { AiAssistantMenuItems } from "../ai/AiMenuItems";
import { redo, undo } from "../canvas/actions";
import { CollabControl } from "../collab/CollabControl";
import { useCollab } from "../collab/session";
import { formatShortcut, runCommand } from "../lib/commands";
import { isTaskNode } from "../lib/types";
import { layoutCanvas, newDag, newRawDag } from "../lifecycle/actions";
import { useEnvironments, useMeta } from "../lifecycle/hooks";
import { aiUsable, needsUserModel, setAiModel, useAiModel, useAiStatus } from "../lifecycle/lib/ai";
import { hostDagId, syncDeepLink } from "../lifecycle/lib/deepLink";
import { deleteToast, deleteWentLive, ifMatch } from "../lifecycle/lib/deleteOutcome";
import { type LifecycleDialog, useLifecycle } from "../lifecycle/store";
import { EditorModeToggle } from "../codemode/EditorModeToggle";
import { RawBadge } from "../codemode/RawCodeView";
import { editorAvailability, effectiveMode, useCodeMode, useIsRaw } from "../codemode/store";
import { useLayout } from "../shell/layout";
import { ShellControls } from "../shell/ShellControls";
import { Tip } from "../shell/Tip";
import { type Permission, useAccess } from "../lib/access";
import { useBuilder } from "../store/builder";
import { toaster } from "../ui/toaster";

const openDialog = (d: LifecycleDialog) => useLifecycle.getState().openDialog(d);

/** Width of an element (0 until measured), following resizes. */
function useWidth(ref: RefObject<HTMLElement | null>): number {
  const [width, setWidth] = useState(0);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const measure = () => setWidth(Math.round(el.getBoundingClientRect().width));
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [ref]);
  return width;
}

function MenuButton({ label, icon, children }: { label: string; icon?: ReactNode; children: ReactNode }) {
  return (
    <Menu.Root positioning={{ placement: "bottom-start" }}>
      <Menu.Trigger asChild>
        <Button size="xs" variant="ghost">
          {icon} {label} <LuChevronDown />
        </Button>
      </Menu.Trigger>
      <Portal>
        <Menu.Positioner>
          <Menu.Content minW="240px">{children}</Menu.Content>
        </Menu.Positioner>
      </Portal>
    </Menu.Root>
  );
}

function SubMenu({
  value,
  label,
  icon,
  children,
}: {
  value: string;
  label: string;
  icon: ReactNode;
  children: ReactNode;
}) {
  return (
    <Menu.Root positioning={{ placement: "right-start", gutter: 2 }}>
      <Menu.TriggerItem data-value={value}>
        {icon} <Box flex="1">{label}</Box> <LuChevronRight />
      </Menu.TriggerItem>
      <Portal>
        <Menu.Positioner>
          <Menu.Content minW="220px">{children}</Menu.Content>
        </Menu.Positioner>
      </Portal>
    </Menu.Root>
  );
}

function Shortcut({ keys }: { keys: string }) {
  return <Menu.ItemCommand>{formatShortcut(keys)}</Menu.ItemCommand>;
}

function FileMenu() {
  const { t } = useTranslation("lifecycle");
  const qc = useQueryClient();
  const raw = useIsRaw();
  const savedDagId = useBuilder((s) => s.savedDagId);
  const hasSelection = useBuilder((s) => s.nodes.some((n) => n.selected));
  const inDagTab = !!hostDagId();
  const access = useAccess();
  const denied = (p: Permission) => (access.can(p) ? undefined : t("access.denied", { permission: p }));
  const remove = useMutation({
    ...deleteSpecMutation(),
    onSuccess: () => qc.invalidateQueries({ queryKey: listSpecsQueryKey() }),
  });
  const meta = useMeta();
  const deleteDag = () => {
    if (!savedDagId) return;
    // Git targets taking the user's own token: a dialog with the token field.
    const mode = meta?.storage.kind === "git" ? meta.iam?.deploy?.git_token : null;
    if (mode === "optional" || mode === "required") {
      openDialog({ kind: "deleteDag", dagId: savedDagId });
      return;
    }
    if (!window.confirm(t("confirm.delete", { dagId: savedDagId }))) return;
    remove.mutate(
      // If-Match: the server refuses deleting a version this editor never loaded.
      { path: { dag_id: savedDagId }, headers: ifMatch(useBuilder.getState().baseEtag) },
      {
        onSuccess: (res) => {
          // A delete proposal (four-eyes) or a pull request leaves the DAG live: keep it open.
          if (deleteWentLive(res)) {
            useBuilder.getState().reset();
            useLifecycle.getState().setSelectedRunId(null);
            syncDeepLink(null);
          }
          toaster.create(deleteToast(res, savedDagId, t));
        },
        onError: (e) =>
          toaster.create({ type: "error", title: t("toolbar.deleteFailed"), description: errorMessage(e) }),
      },
    );
  };
  return (
    <MenuButton label={t("menu.file")}>
      <Menu.Item value="new" onClick={newDag}>
        <LuFilePlus /> {t("menu.new")}
      </Menu.Item>
      {editorAvailability(access).canRaw ? (
        <Menu.Item value="new-raw" onClick={newRawDag} data-testid="menu-new-raw">
          <LuFileCode /> {t("menu.newRaw")}
        </Menu.Item>
      ) : null}
      {access.feature("templates") ? (
        <Menu.Item value="template" onClick={() => openDialog({ kind: "templates" })}>
          <LuLayoutTemplate /> {t("menu.newFromTemplate")}
        </Menu.Item>
      ) : null}
      {!inDagTab ? (
        <Menu.Item value="open" onClick={() => openDialog({ kind: "open" })}>
          <LuFolderOpen /> {t("menu.open")}
          <Shortcut keys="mod+o" />
        </Menu.Item>
      ) : null}
      <Menu.Item value="duplicate" onClick={() => openDialog({ kind: "duplicate" })}>
        <LuCopy /> {t("menu.duplicate")}
      </Menu.Item>
      {access.feature("imports") ? (
        <SubMenu value="import" label={t("menu.importSub")} icon={<LuFileInput />}>
          <Menu.Item
            value="import-python"
            disabled={!access.can("code:execute") || !access.feature("python_code")}
            title={denied("code:execute")}
            onClick={() => openDialog({ kind: "import", source: "python" })}
          >
            {t("menu.importPython")}
          </Menu.Item>
          <Menu.Item
            value="import-airflow"
            hidden={!access.feature("airflow")}
            onClick={() => openDialog({ kind: "import", source: "airflow" })}
          >
            {t("menu.importAirflow")}
          </Menu.Item>
          <Menu.Item value="import-yaml" onClick={() => openDialog({ kind: "import", source: "yaml" })}>
            {t("menu.importYaml")}
          </Menu.Item>
          <Menu.Item value="import-spec" onClick={() => openDialog({ kind: "import", source: "spec" })}>
            {t("menu.importSpec")}
          </Menu.Item>
          <Menu.Item
            value="import-bulk"
            disabled={!access.can("code:execute") || !access.can("spec:write") || !access.feature("python_code")}
            title={denied(!access.can("code:execute") ? "code:execute" : "spec:write")}
            onClick={() => openDialog({ kind: "bulkImport" })}
          >
            {t("menu.importBulk")}
          </Menu.Item>
        </SubMenu>
      ) : null}
      <Menu.Separator />
      {access.feature("templates") ? (
        <>
          <Menu.Item
            value="save-template"
            disabled={!access.can("template:write")}
            title={denied("template:write")}
            onClick={() => openDialog({ kind: "saveTemplate", mode: "dag" })}
          >
            <LuLayoutTemplate /> {t("menu.saveTemplate")}
          </Menu.Item>
          <Menu.Item
            value="save-fragment"
            hidden={raw}
            disabled={!hasSelection || !access.can("template:write")}
            title={denied("template:write") ?? (!hasSelection ? t("toolbar.selectNodesFirst") : undefined)}
            onClick={() => openDialog({ kind: "saveTemplate", mode: "fragment" })}
          >
            <LuLayoutTemplate /> {t("menu.saveFragment")}
          </Menu.Item>
        </>
      ) : null}
      <SubMenu value="export" label={t("menu.exportSub")} icon={<LuDownload />}>
        <Menu.Item value="export-python" onClick={() => runCommand("dag.export")}>
          {t("menu.exportPython")}
        </Menu.Item>
        <Menu.Item value="export-yaml" hidden={raw} onClick={() => runCommand("dag.exportYaml")}>
          {t("menu.exportYaml")}
        </Menu.Item>
        <Menu.Item value="export-dagfactory" hidden={raw} onClick={() => runCommand("dag.exportDagFactory")}>
          {t("menu.exportDagFactory")}
        </Menu.Item>
        <Menu.Item value="export-spec" onClick={() => runCommand("dag.exportSpec")}>
          {t("menu.exportSpec")}
        </Menu.Item>
      </SubMenu>
      {savedDagId && access.can("spec:delete") ? (
        <>
          <Menu.Separator />
          <Menu.Item value="delete" color="fg.error" onClick={deleteDag}>
            <LuTrash2 /> {t("menu.delete")}
          </Menu.Item>
        </>
      ) : null}
    </MenuButton>
  );
}

function RunMenu() {
  const { t } = useTranslation("lifecycle");
  const deployed = useBuilder((s) => !!s.savedDagId);
  const selectedTask = useBuilder((s) => {
    const n = s.nodes.find((x) => x.id === s.selectedId);
    return n && isTaskNode(n) ? n.id : null;
  });
  const notDeployed = !deployed ? t("common.deployFirst") : undefined;
  const access = useAccess();
  const denied = (p: Permission) => (access.can(p) ? undefined : t("access.denied", { permission: p }));
  // A test run executes the DAG's Python on the server: it needs code:execute as well.
  const canTest = access.allows("test_runs", "run:test") && access.can("code:execute");
  const testDenied = denied("run:test") ?? denied("code:execute");
  const aiFeature = access.feature("ai");
  // Review and secret scan read the visual spec (gated like the palette's ai.review / ai.leaks).
  const raw = useIsRaw();
  return (
    <MenuButton label={t("menu.run")}>
      <Menu.Item value="validate" onClick={() => runCommand("dag.validate")}>
        <LuShieldCheck /> {t("menu.validate")}
      </Menu.Item>
      {/* Without the AI menu the deterministic review and secret scan live here. */}
      <Menu.Item value="review-rules" hidden={aiFeature || raw} onClick={() => openDialog({ kind: "aiReview" })}>
        <LuClipboardCheck /> {t("menu.reviewRules")}
      </Menu.Item>
      <Menu.Item value="scan-secrets" hidden={aiFeature || raw} onClick={() => openDialog({ kind: "aiLeaks" })}>
        <LuShieldAlert /> {t("menu.scanSecrets")}
      </Menu.Item>
      <Menu.Item
        value="test-task"
        disabled={!selectedTask || !canTest}
        title={testDenied ?? (!selectedTask ? t("toolbar.selectTaskFirst") : undefined)}
        hidden={!access.feature("test_runs")}
        onClick={() => openDialog({ kind: "testTask", nodeId: selectedTask })}
      >
        <LuFlaskConical /> {t("menu.testTask")}
      </Menu.Item>
      <Menu.Item
        value="test-dag"
        disabled={!canTest}
        title={testDenied}
        hidden={!access.feature("test_runs")}
        onClick={() => openDialog({ kind: "testTask", nodeId: selectedTask, whole: true })}
      >
        <LuFlaskConical /> {t("menu.testDag")}
      </Menu.Item>
      <Menu.Separator />
      <Menu.Item
        value="trigger"
        disabled={!deployed || !access.can("run:trigger")}
        title={denied("run:trigger") ?? notDeployed}
        hidden={!access.feature("runs") || !access.feature("airflow")}
        onClick={() => openDialog({ kind: "trigger" })}
      >
        <LuPlay /> {t("menu.trigger")}
      </Menu.Item>
      <Menu.Item
        value="backfill"
        disabled={!deployed || !access.can("run:manage")}
        title={denied("run:manage") ?? notDeployed}
        hidden={!access.feature("runs") || !access.feature("airflow")}
        onClick={() => openDialog({ kind: "backfill" })}
      >
        <LuHistory /> {t("menu.backfill")}
      </Menu.Item>
    </MenuButton>
  );
}

function VersionsMenu() {
  const { t } = useTranslation("lifecycle");
  const deployed = useBuilder((s) => !!s.savedDagId);
  const prUrl = useBuilder((s) => s.pendingPrUrl);
  const access = useAccess();
  return (
    <MenuButton label={t("menu.versions")}>
      <Menu.Item value="history" onClick={() => useLifecycle.getState().setBottomTab("versions")}>
        <LuHistory /> {t("menu.history")}
      </Menu.Item>
      <Menu.Item value="storage" onClick={() => openDialog({ kind: "storage" })}>
        <LuDatabase /> {t("menu.storage")}
      </Menu.Item>
      <Menu.Item value="pr" disabled={!prUrl} asChild={!!prUrl}>
        {prUrl ? (
          <a href={prUrl} target="_blank" rel="noreferrer">
            <LuExternalLink /> {t("menu.openPr")}
          </a>
        ) : (
          <>
            <LuExternalLink /> {t("menu.openPr")}
          </>
        )}
      </Menu.Item>
      <Menu.Item
        value="promote"
        disabled={!deployed || !access.can("promote")}
        title={
          !access.can("promote")
            ? t("access.denied", { permission: "promote" })
            : !deployed
              ? t("common.deployFirst")
              : undefined
        }
        onClick={() => openDialog({ kind: "promote" })}
      >
        <LuGitPullRequestArrow /> {t("menu.promote")}
      </Menu.Item>
    </MenuButton>
  );
}

/** Menu item with a tooltip (also on disabled items, where the native title never shows). */
function TipItem({ tip, ...props }: { tip?: string | undefined } & Menu.ItemProps) {
  const item = <Menu.Item {...props} />;
  return tip ? (
    <Tip content={tip} placement="right">
      {item}
    </Tip>
  ) : (
    item
  );
}

function AiMenu() {
  const { t } = useTranslation("lifecycle");
  const meta = useMeta();
  const status = useAiStatus();
  const model = useAiModel();
  const access = useAccess();
  // /ai/status knows about per-user models (meta.ai_enabled only reflects a central model).
  const enabled = aiUsable(status, !!meta?.ai_enabled) && access.can("ai:use");
  const ownModel = !!status && status.enabled !== false && (status.llm_mode ?? "central") !== "central";
  const mustConfigure = needsUserModel(status);
  const admin = status?.role === "admin";
  // Admins with the configuration page can see how AI is set up (AUTOAIR_AI__*).
  const canConfigure = access.can("admin:config") && !!meta?.expose_config && meta.expose_config !== "false";
  const hasProblems = useBuilder((s) => s.problems.length > 0);
  const pythonSelected = useBuilder((s) => s.nodes.some((n) => n.id === s.selectedId && n.type === "python_task"));
  // Spec-only dialogs (SPEC_ONLY_DIALOGS) are hidden for raw code DAGs, as in the command palette.
  const raw = useIsRaw();
  const off = !enabled
    ? access.can("ai:use")
      ? t("toolbar.aiDisabled")
      : t("access.denied", { permission: "ai:use" })
    : undefined;
  const models = status?.models ?? [];
  return (
    <>
      {/* The chat panel and AI dialogs (portalled), with their Ctrl/⌘+Shift+A command. */}
      <AiHost />
      <MenuButton label={t("menu.ai")} icon={<LuSparkles />}>
        {!enabled ? (
          <Text px={2} py={1} color="fg.muted" fontSize="xs">
            {off}
          </Text>
        ) : null}
        {!enabled && access.can("ai:use") && !mustConfigure && !ownModel ? (
          <TipItem
            value="ai-setup"
            tip={canConfigure ? t("toolbar.aiSetupAdmin") : t("toolbar.aiSetupHelp")}
            onClick={() =>
              canConfigure
                ? openDialog({ kind: "config" })
                : toaster.create({
                    type: "info",
                    title: t("toolbar.aiSetupTitle"),
                    description: t("toolbar.aiSetupHelp"),
                  })
            }
          >
            <LuSettings2 /> {t("menu.aiSetup")}
          </TipItem>
        ) : null}
        {mustConfigure && access.can("ai:use") ? (
          <TipItem value="ai-my-model-hint" onClick={() => openDialog({ kind: "aiModel" })}>
            <Text color="orange.fg" fontSize="xs">
              {t("toolbar.aiConfigureModel")}
            </Text>
          </TipItem>
        ) : null}
        <TipItem value="ai-generate" disabled={!enabled} tip={off} onClick={() => openDialog({ kind: "aiGenerate" })}>
          <LuSparkles /> {t("menu.aiGenerate")}
        </TipItem>
        <TipItem
          value="ai-edit"
          hidden={raw}
          disabled={!enabled}
          tip={off}
          onClick={() => openDialog({ kind: "aiEdit" })}
        >
          <LuWandSparkles /> {t("menu.aiEdit")}
        </TipItem>
        <TipItem
          value="ai-task-code"
          hidden={raw}
          disabled={!enabled || !pythonSelected}
          tip={off ?? (!pythonSelected ? t("toolbar.selectPythonTask") : undefined)}
          onClick={() => runCommand("ai.taskCode")}
        >
          <LuSquareCode /> {t("menu.aiTaskCode")}
        </TipItem>
        <Menu.Separator />
        <TipItem
          value="ai-explain-dag"
          disabled={!enabled}
          tip={off}
          onClick={() => openDialog({ kind: "aiExplainDag" })}
        >
          <LuBookText /> {t("menu.aiExplainDag")}
        </TipItem>
        <TipItem
          value="ai-explain"
          disabled={!enabled || !hasProblems}
          tip={off ?? (!hasProblems ? t("toolbar.noProblems") : undefined)}
          onClick={() => openDialog({ kind: "aiExplain" })}
        >
          <LuMessageCircleQuestion /> {t("menu.aiExplain")}
        </TipItem>
        <TipItem
          value="ai-fix"
          hidden={raw}
          disabled={!enabled}
          tip={off}
          onClick={() => openDialog({ kind: "aiFix" })}
        >
          <LuWrench /> {t("menu.aiFix")}
        </TipItem>
        {/* Review / leak scan / optimise also run their rules without an LLM. */}
        {raw ? null : <Menu.Separator />}
        <TipItem value="ai-review" hidden={raw} onClick={() => openDialog({ kind: "aiReview" })}>
          <LuClipboardCheck /> {t("menu.aiReview")}
        </TipItem>
        <TipItem value="ai-leaks" hidden={raw} onClick={() => openDialog({ kind: "aiLeaks" })}>
          <LuShieldAlert /> {t("menu.aiLeaks")}
        </TipItem>
        <TipItem value="ai-optimize" hidden={raw} onClick={() => openDialog({ kind: "aiOptimize" })}>
          <LuGauge /> {t("menu.aiOptimize")}
        </TipItem>
        <TipItem value="ai-docs" hidden={raw} disabled={!enabled} tip={off} onClick={() => runCommand("ai.writeDocs")}>
          <LuBookOpen /> {t("menu.aiDocs")}
        </TipItem>
        <Menu.Separator />
        <AiAssistantMenuItems enabled={enabled} off={off} />
        <TipItem value="ai-writer" disabled={!enabled} tip={off} onClick={() => openDialog({ kind: "aiWriter" })}>
          <LuSquareCode /> {t("menu.aiWriter")}
        </TipItem>
        {ownModel || admin ? <Menu.Separator /> : null}
        {ownModel ? (
          <TipItem value="ai-my-model" disabled={!access.can("ai:use")} onClick={() => openDialog({ kind: "aiModel" })}>
            <LuKeyRound /> {status?.configured ? t("menu.aiMyModel") : t("menu.aiUseOwnModel")}
          </TipItem>
        ) : null}
        {admin ? (
          <TipItem value="ai-knowledge" onClick={() => openDialog({ kind: "aiKnowledge" })}>
            <LuLibrary /> {t("menu.aiKnowledge")}
          </TipItem>
        ) : null}
        {models.length > 1 ? (
          <>
            <Menu.Separator />
            <Menu.ItemGroup>
              <Menu.ItemGroupLabel fontSize="xs">{t("menu.aiModel")}</Menu.ItemGroupLabel>
              <Menu.RadioItemGroup
                value={model && models.includes(model) ? model : (status?.model ?? "")}
                onValueChange={(e) => setAiModel(e.value === status?.model ? null : e.value)}
              >
                {models.map((m) => (
                  <Menu.RadioItem key={m} value={m} fontSize="xs" fontFamily="mono">
                    {m}
                    <Menu.ItemIndicator />
                  </Menu.RadioItem>
                ))}
              </Menu.RadioItemGroup>
            </Menu.ItemGroup>
          </>
        ) : meta?.ai_model ? (
          <Text px={2} py={1} color="fg.muted" fontSize="xs">
            {meta.ai_model}
          </Text>
        ) : null}
      </MenuButton>
    </>
  );
}

const STORAGE_ICON = { local: <LuHardDrive />, object: <LuCloud />, git: <LuFolderGit2 /> } as const;

/** The DAG's state in Airflow after a deploy: not parsed yet / paused / active (click toggles). */
function AirflowStateChip({ dagId }: { dagId: string }) {
  const { t } = useTranslation("lifecycle");
  const qc = useQueryClient();
  const access = useAccess();
  const canManage = access.allows("runs", "run:manage");
  const status = useQuery({
    ...getDagStatusOptions({ path: { dag_id: dagId } }),
    retry: false,
    refetchInterval: (q) => (q.state.data?.exists ? 30_000 : 5_000),
  });
  const toggle = useMutation({
    ...setDagPausedMutation(),
    onSuccess: (data) => qc.setQueryData(getDagStatusQueryKey({ path: { dag_id: dagId } }), data),
    onError: (e) => toaster.create({ type: "error", title: t("airflowState.failed"), description: errorMessage(e) }),
  });
  const s = status.data;
  if (!s) return null;
  if (!s.exists)
    return (
      <Badge variant="outline" title={t("airflowState.parsingHelp")} data-testid="airflow-state">
        <Spinner size="xs" /> {t("airflowState.parsing")}
      </Badge>
    );
  const paused = !!s.is_paused;
  return (
    <Badge
      asChild
      cursor="pointer"
      colorPalette={paused ? "orange" : "green"}
      variant="subtle"
      title={paused ? t("airflowState.pausedHelp") : t("airflowState.activeHelp")}
      data-testid="airflow-state"
      data-paused={paused}
    >
      <button
        type="button"
        disabled={toggle.isPending || !canManage}
        onClick={() => {
          // Unpausing makes the scheduler start creating runs: ask first.
          if (paused && !window.confirm(t("airflowState.confirmUnpause", { dagId }))) return;
          toggle.mutate({ path: { dag_id: dagId }, body: { paused: !paused } });
        }}
      >
        {paused ? <LuPause /> : <LuPlay />} {paused ? t("airflowState.paused") : t("airflowState.active")}
      </button>
    </Badge>
  );
}

function StatusChips({ compact, tight = false }: { compact: boolean; tight?: boolean }) {
  const airflowOn = useAccess().feature("airflow");
  const { t } = useTranslation("lifecycle");
  const meta = useMeta();
  // Airflow reads DAGs from git but AutoAir writes local files: deploys would never go live.
  const target = useQuery({ ...getDeployTargetOptions(), staleTime: 300_000, retry: false });
  // Only sources found in Airflow count: the hand-entered object storage option is always listed.
  const sources = target.data?.settings.mode === "local" ? target.data.candidates : [];
  const gitSource = sources.some((c) => c.kind === "airflow-bundle" || c.kind === "git-checkout");
  const objectSource = sources.some((c) => c.kind === "object-bundle");
  const gitMismatch = gitSource || objectSource;
  const dirty = useBuilder((s) => s.dirty);
  const savedDagId = useBuilder((s) => s.savedDagId);
  const prUrl = useBuilder((s) => s.pendingPrUrl);
  return (
    <HStack gap={1.5}>
      {dirty ? (
        <Badge colorPalette="orange">{t("status.unsaved")}</Badge>
      ) : savedDagId ? (
        <Badge colorPalette="green">{t("status.deployed")}</Badge>
      ) : (
        <Badge>{t("status.new")}</Badge>
      )}
      {savedDagId && airflowOn ? <AirflowStateChip dagId={savedDagId} /> : null}
      {prUrl ? (
        <Badge colorPalette="purple" asChild>
          <Link href={prUrl} target="_blank" rel="noreferrer" title={prUrl}>
            <LuGitPullRequestArrow /> {t("status.prOpen")}
          </Link>
        </Badge>
      ) : null}
      {meta ? (
        <Tip
          content={
            gitSource
              ? t("deployTarget.chipWarn")
              : objectSource
                ? t("deployTarget.chipWarnObject")
                : t("status.storageTitle", { kind: meta.storage.kind, location: meta.storage.location })
          }
        >
          <Badge
            // Amber, not alarm-red: deploys work, they just may not reach Airflow.
            variant={gitMismatch ? "surface" : "outline"}
            colorPalette={gitMismatch ? "yellow" : undefined}
            cursor="pointer"
            asChild
            data-testid="storage-chip"
          >
            <button type="button" onClick={() => openDialog({ kind: "storage" })}>
              {STORAGE_ICON[meta.storage.kind]}
              {tight ? (
                <Text as="span">{meta.storage.kind}</Text>
              ) : (
                <Text as="span" maxW={compact ? "90px" : "160px"} truncate>
                  {meta.storage.kind} · {meta.storage.location}
                </Text>
              )}
            </button>
          </Badge>
        </Tip>
      ) : null}
      {meta?.target_airflow_version ? (
        <Badge variant="subtle" title={t("status.targetTitle")}>
          {t("status.target", { version: meta.target_airflow_version })}
        </Badge>
      ) : null}
    </HStack>
  );
}

function EnvironmentSelect() {
  const { t } = useTranslation("lifecycle");
  const meta = useMeta();
  const envs = useEnvironments();
  const env = useBuilder((s) => s.environment);
  const setEnv = useBuilder((s) => s.setEnvironment);
  if (!envs.length) return null;
  return (
    <NativeSelect.Root size="xs" w="auto" minW="120px">
      <NativeSelect.Field
        aria-label={t("deploy.environment")}
        title={t("deploy.environment")}
        value={env ?? ""}
        onChange={(e) => setEnv(e.target.value || null)}
      >
        <option value="">{t("deploy.defaultEnvironment", { env: meta?.default_environment ?? "—" })}</option>
        {envs.map((e) => (
          <option key={e} value={e}>
            {e}
          </option>
        ))}
      </NativeSelect.Field>
      <NativeSelect.Indicator />
    </NativeSelect.Root>
  );
}

/** Shows the Problems tab (bottom panel expanded). */
function openProblems(): void {
  useLifecycle.getState().setBottomTab("problems");
  useLayout.getState().set("bottom", false);
}

function DeployButton({ compact }: { compact: boolean }) {
  const { t } = useTranslation("lifecycle");
  const errors = useBuilder((s) => s.problems.filter((p) => p.severity === "error").length);
  // A raw code DAG is a file (no task nodes): deployable once it has code.
  const raw = useIsRaw();
  const empty = useBuilder((s) => (s.dag.raw_code != null ? !s.dag.raw_code.trim() : !s.nodes.some(isTaskNode)));
  const access = useAccess();
  const meta = useMeta();
  // Saving opens a PR for authors; going live needs the deploy permission.
  const rights = meta?.iam?.deploy;
  const allowed = access.feature("deploy") && (rights ? rights.can_save : access.can("spec:write"));
  // Raw code DAGs are saved only where code editing and code:raw allow it (the server says 404/403).
  const rawLocked = raw && !editorAvailability(access).canRaw;
  const blocked = !allowed
    ? t("access.denied", { permission: "deploy" })
    : rawLocked
      ? t(access.feature("code_editor") ? "raw.readOnlyHelp" : "raw.codeOffHelp")
      : empty
        ? t(raw ? "deploy.addCode" : "deploy.addTask")
        : errors > 0
          ? t("deploy.fixProblems", { count: errors })
          : undefined;
  if (!access.feature("deploy")) return null;
  // Blocked by problems, the button stays clickable: it opens the Problems tab.
  const disabled = !allowed || empty || rawLocked;
  return (
    <ButtonGroup size="xs" colorPalette="blue" attached>
      <Tip
        content={
          blocked ?? `${rights?.default_opens_pr ? t("deploy.opensPr") : t("cmd.deploy")} (${formatShortcut("mod+s")})`
        }
      >
        <Button
          disabled={disabled}
          aria-disabled={blocked ? true : undefined}
          data-testid="deploy-button"
          opacity={blocked && !disabled ? 0.55 : undefined}
          cursor={blocked && !disabled ? "help" : undefined}
          onClick={() => (errors > 0 ? openProblems() : runCommand("dag.deploy"))}
        >
          <LuRocket /> {t("deploy.button")}
          {compact ? null : (
            <Text as="span" fontSize="2xs" opacity={0.8}>
              {formatShortcut("mod+s")}
            </Text>
          )}
        </Button>
      </Tip>
      <Menu.Root positioning={{ placement: "bottom-end" }}>
        <Menu.Trigger asChild>
          <IconButton aria-label={t("toolbar.deployOptions")} disabled={!!blocked} borderLeftWidth="1px">
            <LuChevronDown />
          </IconButton>
        </Menu.Trigger>
        <Portal>
          <Menu.Positioner>
            <Menu.Content>
              <Menu.Item value="deploy-dialog" onClick={() => openDialog({ kind: "deploy", focus: "message" })}>
                <LuRocket /> {t("menu.deployDialog")}
              </Menu.Item>
            </Menu.Content>
          </Menu.Positioner>
        </Portal>
      </Menu.Root>
    </ButtonGroup>
  );
}

export function Toolbar() {
  const { t } = useTranslation("lifecycle");
  const aiFeature = useAccess().feature("ai");
  const dagId = useBuilder((s) => s.dag.dag_id);
  const displayName = useBuilder((s) => s.dag.display_name);
  const temporal = useStore(useBuilder.temporal);
  const collab = useCollab((s) => s.status === "connected");
  const collabCanUndo = useCollab((s) => s.canUndo);
  const collabCanRedo = useCollab((s) => s.canRedo);
  const canUndo = collab ? collabCanUndo : temporal.pastStates.length > 0;
  const canRedo = collab ? collabCanRedo : temporal.futureStates.length > 0;
  const availability = editorAvailability(useAccess());
  const raw = useIsRaw();
  const storedMode = useCodeMode((s) => s.mode);
  const codeMode = raw || effectiveMode(storedMode, availability) === "code";
  // Density follows the header's own width (inside Airflow it is narrower than the window).
  const header = useRef<HTMLDivElement>(null);
  const width = useWidth(header);
  const compact = width > 0 && width < 1500;
  // Inside Airflow a 1280px window leaves ~1215px: the storage chip shrinks to its icon.
  const tight = width > 0 && width < 1320;
  const wraps = width > 0 && width < 1080;

  return (
    // One row down to 1280px (en and tr): long values truncate (with tooltips), rarely used
    // shell buttons live in the "⋯" menu.
    <HStack
      ref={header}
      px={3}
      py={1.5}
      gap={2}
      borderBottomWidth="1px"
      wrap={wraps ? "wrap" : "nowrap"}
      minW={0}
      overflow="hidden"
      as="header"
      aria-label={t("toolbar.label")}
      css={{ "& > *": { flexShrink: 0 } }}
    >
      {compact ? null : (
        <Text fontWeight="bold" fontSize="sm" whiteSpace="nowrap">
          {t("toolbar.appName")}
        </Text>
      )}
      <Tip content={displayName && displayName !== dagId ? `${displayName}\n${dagId}` : dagId}>
        <Text
          fontFamily="mono"
          fontSize="sm"
          truncate
          maxW="220px"
          minW="48px"
          data-testid="toolbar-dag-id"
          css={{ flexShrink: "1 !important" }}
        >
          {dagId}
        </Text>
      </Tip>
      {raw ? <RawBadge size="xs" /> : null}
      <StatusChips compact={compact} tight={tight} />
      <Separator orientation="vertical" height="5" />
      <HStack as="nav" gap={0} aria-label={t("toolbar.menus")}>
        <FileMenu />
        <RunMenu />
        <VersionsMenu />
        {aiFeature ? <AiMenu /> : null}
      </HStack>
      <Separator orientation="vertical" height="5" />
      <Tip content={`${t("cmd.undo")} (${formatShortcut("mod+z")})`}>
        <IconButton aria-label={t("cmd.undo")} size="xs" variant="ghost" disabled={!canUndo} onClick={() => undo()}>
          <LuUndo2 />
        </IconButton>
      </Tip>
      <Tip content={`${t("cmd.redo")} (${formatShortcut("mod+shift+z")})`}>
        <IconButton aria-label={t("cmd.redo")} size="xs" variant="ghost" disabled={!canRedo} onClick={() => redo()}>
          <LuRedo2 />
        </IconButton>
      </Tip>
      {codeMode ? null : (
        <Tip content={`${t("toolbar.autoLayout")} (${formatShortcut("shift+l")})`}>
          <IconButton
            aria-label={t("toolbar.autoLayout")}
            size="xs"
            variant="ghost"
            onClick={() => void layoutCanvas()}
          >
            <LuLayoutDashboard />
          </IconButton>
        </Tip>
      )}
      {raw ? null : <EditorModeToggle iconOnly={compact} />}
      <Box flex="1" minW={0} />
      <CollabControl />
      <EnvironmentSelect />
      <DeployButton compact={compact} />
      <ShellControls />
    </HStack>
  );
}
