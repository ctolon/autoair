import { useQueryClient } from "@tanstack/react-query";
import { useReactFlow } from "@xyflow/react";
import { Suspense, lazy, useEffect } from "react";
import { useTranslation } from "react-i18next";
import {
  LuBookOpen,
  LuBotMessageSquare,
  LuClipboardCheck,
  LuDatabase,
  LuDownload,
  LuFileCode,
  LuFileInput,
  LuFilePlus,
  LuFlaskConical,
  LuFolderOpen,
  LuGauge,
  LuGitPullRequestArrow,
  LuHistory,
  LuLayoutTemplate,
  LuMessageCircleQuestion,
  LuPlay,
  LuRocket,
  LuShieldAlert,
  LuShieldCheck,
  LuSparkles,
  LuSquareCode,
  LuWandSparkles,
  LuWrench,
} from "react-icons/lu";

import { getDagStatus } from "../api/gen/sdk.gen";
import { editorAvailability, isRawDag } from "../codemode/store";
import { accessOf } from "../lib/access";
import { type Command, registerCommands } from "../lib/commands";
import { isTaskNode } from "../lib/types";
import { useBuilder } from "../store/builder";
import {
  aiWriteDocs,
  deploy,
  exportAirflowYaml,
  exportDagFactoryYaml,
  exportPython,
  exportSpecJson,
  newDag,
  newRawDag,
  openDag,
  validateInAirflow,
} from "./actions";
import { hostDagId, initialDagId, syncDeepLink } from "./lib/deepLink";
import { aiUsable, getAiStatus } from "./lib/ai";
import { getMeta, runtime } from "./runtime";
import { type LifecycleDialog, useLifecycle } from "./store";
import { useRunOverlaySync } from "./useRunOverlaySync";
import { ErrorBoundary } from "../ui/ErrorBoundary";

const OpenDialog = lazy(() => import("./dialogs/OpenDialog"));
const DeployDialog = lazy(() => import("./dialogs/DeployDialog"));
const ConflictDialog = lazy(() => import("./dialogs/ConflictDialog"));
const TriggerDialog = lazy(() => import("./dialogs/TriggerDialog"));
const BackfillDialog = lazy(() => import("./dialogs/BackfillDialog"));
const ClearRunDialog = lazy(() => import("./dialogs/ClearRunDialog"));
const TestTaskDialog = lazy(() => import("./dialogs/TestTaskDialog"));
const TemplatesDialog = lazy(() => import("./dialogs/TemplatesDialog"));
const SaveTemplateDialog = lazy(() => import("./dialogs/SaveTemplateDialog"));
const SaveOperatorTemplateDialog = lazy(() => import("./dialogs/SaveOperatorTemplateDialog"));
const OperatorTemplateInputsDialog = lazy(() => import("./dialogs/OperatorTemplateInputsDialog"));
const OperatorTemplateEditor = lazy(() => import("./dialogs/OperatorTemplateEditor"));
const ImportDialog = lazy(() => import("./dialogs/ImportDialog"));
const BulkImportDialog = lazy(() => import("./dialogs/BulkImportDialog"));
const PromoteDialog = lazy(() => import("./dialogs/PromoteDialog"));
const DeleteDagDialog = lazy(() => import("./dialogs/DeleteDagDialog"));
const StorageDialog = lazy(() => import("./dialogs/StorageDialog"));
const DuplicateDialog = lazy(() => import("./dialogs/DuplicateDialog"));
const ConfigDialog = lazy(() => import("./dialogs/ConfigDialog"));
const UnmanagedDialog = lazy(() => import("./dialogs/UnmanagedDialog"));
const AiGenerateDialog = lazy(() => import("./dialogs/AiDialogs").then((m) => ({ default: m.AiGenerateDialog })));
const AiEditDialog = lazy(() => import("./dialogs/AiDialogs").then((m) => ({ default: m.AiEditDialog })));
const AiExplainDialog = lazy(() => import("./dialogs/AiDialogs").then((m) => ({ default: m.AiExplainDialog })));
const AiFindingsDialog = lazy(() =>
  import("./dialogs/AiFindingsDialog").then((m) => ({ default: m.AiFindingsDialog })),
);
const AiExplainDagDialog = lazy(() =>
  import("./dialogs/AiAssistDialogs").then((m) => ({ default: m.AiExplainDagDialog })),
);
const AiTaskCodeDialog = lazy(() => import("./dialogs/AiAssistDialogs").then((m) => ({ default: m.AiTaskCodeDialog })));
const AiAskDialog = lazy(() => import("./dialogs/AiAssistDialogs").then((m) => ({ default: m.AiAskDialog })));
const AiWriterDialog = lazy(() => import("./dialogs/AiWriterDialog").then((m) => ({ default: m.AiWriterDialog })));
const AiModelDialog = lazy(() => import("./dialogs/AiModelDialog").then((m) => ({ default: m.AiModelDialog })));
const AiKnowledgeDialog = lazy(() =>
  import("./dialogs/AiKnowledgeDialog").then((m) => ({ default: m.AiKnowledgeDialog })),
);

function renderDialog(d: LifecycleDialog) {
  switch (d.kind) {
    case "open":
      return <OpenDialog />;
    case "deploy":
      return <DeployDialog focus={d.focus} />;
    case "conflict":
      return <ConflictDialog conflict={d.conflict} request={d.request} spec={d.spec} />;
    case "trigger":
      return <TriggerDialog />;
    case "backfill":
      return <BackfillDialog />;
    case "clear":
      return <ClearRunDialog runId={d.runId} />;
    case "testTask":
      return <TestTaskDialog nodeId={d.nodeId} whole={d.whole ?? false} />;
    case "templates":
      return <TemplatesDialog filter={d.filter} />;
    case "saveTemplate":
      return <SaveTemplateDialog mode={d.mode} />;
    case "saveOperatorTemplate":
      return <SaveOperatorTemplateDialog nodeId={d.nodeId} />;
    case "operatorTemplateInputs":
      return <OperatorTemplateInputsDialog template={d.template} at={d.at} />;
    case "operatorTemplateEditor":
      return <OperatorTemplateEditor mode={d.mode} templateId={d.templateId} />;
    case "aiGenerate":
      return <AiGenerateDialog />;
    case "aiEdit":
      return <AiEditDialog />;
    case "aiExplain":
      return <AiExplainDialog />;
    case "aiExplainDag":
      return <AiExplainDagDialog />;
    case "aiReview":
      return <AiFindingsDialog mode="review" />;
    case "aiLeaks":
      return <AiFindingsDialog mode="leaks" />;
    case "aiOptimize":
      return <AiFindingsDialog mode="optimize" />;
    case "aiFix":
      return <AiFindingsDialog mode="fix" />;
    case "aiTaskCode":
      return <AiTaskCodeDialog nodeId={d.nodeId} />;
    case "aiAsk":
      return <AiAskDialog />;
    case "aiWriter":
      return <AiWriterDialog />;
    case "aiModel":
      return <AiModelDialog />;
    case "aiKnowledge":
      return <AiKnowledgeDialog />;
    case "import":
      return <ImportDialog source={d.source} dagId={d.dagId} />;
    case "bulkImport":
      return <BulkImportDialog />;
    case "promote":
      return <PromoteDialog />;
    case "deleteDag":
      return <DeleteDagDialog dagId={d.dagId} />;
    case "storage":
      return <StorageDialog />;
    case "duplicate":
      return <DuplicateDialog />;
    case "config":
      return <ConfigDialog />;
  }
}

const open = (d: LifecycleDialog) => useLifecycle.getState().openDialog(d);
const aiEnabled = () => aiUsable(getAiStatus(), !!getMeta()?.ai_enabled);
/** The one selected task when the user may save operator templates (`template:write`). */
const operatorTemplateSource = (): string | null => {
  const meta = getMeta();
  if (meta?.features?.templates === false) return null;
  if (meta?.iam && !(meta.iam.permissions ?? []).includes("template:write")) return null;
  const selected = useBuilder.getState().nodes.filter((n) => n.selected);
  const only = selected.length === 1 ? selected[0] : undefined;
  return only && (only.type === "operator" || only.type === "python_task" || only.type === "custom") ? only.id : null;
};
/** The user may create and edit operator templates (feature on, `template:write`). */
const canWriteTemplates = (): boolean => accessOf(getMeta()).allows("templates", "template:write");
const deployed = () => !!useBuilder.getState().savedDagId;
/** The open document is a visual (spec-backed) DAG: spec-only commands apply. */
const visualDoc = () => !isRawDag(useBuilder.getState().dag);
const selectedTask = () => {
  const { nodes, selectedId } = useBuilder.getState();
  const n = nodes.find((x) => x.id === selectedId);
  return n && isTaskNode(n) ? n.id : null;
};

/** Registers the lifecycle commands (re-registered when the language changes). */
function useLifecycleCommands(): void {
  const { t, i18n } = useTranslation("lifecycle");
  useEffect(() => {
    const g = {
      file: t("group.file"),
      edit: t("group.edit"),
      run: t("group.run"),
      versions: t("group.versions"),
      ai: t("group.ai"),
    };
    const commands: Command[] = [
      // undo/redo are registered by the canvas (edit.undo / edit.redo)
      { id: "dag.new", title: t("menu.new"), group: g.file, icon: <LuFilePlus />, run: newDag },
      {
        id: "dag.newRaw",
        title: t("menu.newRaw"),
        group: g.file,
        icon: <LuFileCode />,
        when: () => editorAvailability(accessOf(getMeta())).canRaw,
        run: newRawDag,
      },
      {
        id: "dag.open",
        title: t("menu.open"),
        group: g.file,
        shortcut: "mod+o",
        icon: <LuFolderOpen />,
        run: () => open({ kind: "open" }),
      },
      { id: "dag.validate", title: t("menu.validate"), group: g.run, icon: <LuShieldCheck />, run: validateInAirflow },
      {
        id: "dag.deploy",
        title: t("cmd.deploy"),
        group: g.file,
        shortcut: "mod+s",
        icon: <LuRocket />,
        keywords: ["save", "kaydet"],
        run: async () => {
          // storage.git.request_token=required: every git write needs the user's token,
          // which only the deploy dialog asks for.
          if (getMeta()?.iam?.deploy?.git_token === "required") open({ kind: "deploy", focus: "message" });
          else await deploy();
        },
      },
      {
        id: "dag.deployWithMessage",
        title: t("menu.deployWithMessage"),
        group: g.file,
        icon: <LuRocket />,
        run: () => open({ kind: "deploy", focus: "message" }),
      },
      {
        id: "dag.trigger",
        title: t("menu.trigger"),
        group: g.run,
        icon: <LuPlay />,
        when: deployed,
        run: () => open({ kind: "trigger" }),
      },
      {
        id: "dag.backfill",
        title: t("menu.backfill"),
        group: g.run,
        icon: <LuHistory />,
        when: deployed,
        run: () => open({ kind: "backfill" }),
      },
      { id: "dag.export", title: t("menu.exportPython"), group: g.file, icon: <LuDownload />, run: exportPython },
      {
        id: "dag.exportYaml",
        title: t("menu.exportYaml"),
        group: g.file,
        when: visualDoc,
        icon: <LuDownload />,
        run: exportAirflowYaml,
      },
      {
        id: "dag.exportDagFactory",
        title: t("menu.exportDagFactory"),
        group: g.file,
        when: visualDoc,
        icon: <LuDownload />,
        run: exportDagFactoryYaml,
      },
      { id: "dag.exportSpec", title: t("menu.exportSpec"), group: g.file, icon: <LuDownload />, run: exportSpecJson },
      {
        id: "templates.open",
        title: t("menu.newFromTemplate"),
        group: g.file,
        icon: <LuLayoutTemplate />,
        run: () => open({ kind: "templates" }),
      },
      {
        id: "templates.saveAsTemplate",
        title: t("menu.saveTemplate"),
        group: g.file,
        icon: <LuLayoutTemplate />,
        run: () => open({ kind: "saveTemplate", mode: "dag" }),
      },
      {
        id: "templates.saveSelectionAsFragment",
        title: t("menu.saveFragment"),
        group: g.file,
        icon: <LuLayoutTemplate />,
        when: () => useBuilder.getState().nodes.some((n) => n.selected),
        run: () => open({ kind: "saveTemplate", mode: "fragment" }),
      },
      {
        id: "templates.saveAsOperatorTemplate",
        title: t("menu.saveOperatorTemplate"),
        group: g.file,
        icon: <LuLayoutTemplate />,
        when: () => operatorTemplateSource() !== null,
        run: () => {
          const nodeId = operatorTemplateSource();
          if (nodeId) open({ kind: "saveOperatorTemplate", nodeId });
        },
      },
      {
        id: "templates.newOperatorTemplate",
        title: t("menu.newOperatorTemplate"),
        group: g.file,
        icon: <LuLayoutTemplate />,
        keywords: ["operator template", "blueprint", "şablon"],
        when: canWriteTemplates,
        run: () => open({ kind: "operatorTemplateEditor", mode: "new" }),
      },
      {
        id: "templates.manageOperatorTemplates",
        title: t("menu.operatorTemplates"),
        group: g.file,
        icon: <LuLayoutTemplate />,
        keywords: ["operator template", "blueprint", "edit"],
        when: () => accessOf(getMeta()).feature("templates"),
        run: () => open({ kind: "templates", filter: "operator" }),
      },
      {
        id: "ai.generate",
        title: t("menu.aiGenerate"),
        group: g.ai,
        icon: <LuSparkles />,
        when: aiEnabled,
        run: () => open({ kind: "aiGenerate" }),
      },
      {
        id: "ai.edit",
        title: t("menu.aiEdit"),
        group: g.ai,
        icon: <LuWandSparkles />,
        when: () => aiEnabled() && visualDoc(),
        run: () => open({ kind: "aiEdit" }),
      },
      {
        id: "ai.explain",
        title: t("menu.aiExplain"),
        group: g.ai,
        icon: <LuMessageCircleQuestion />,
        when: () => aiEnabled() && useBuilder.getState().problems.length > 0,
        run: () => open({ kind: "aiExplain" }),
      },
      {
        id: "ai.explainDag",
        title: t("menu.aiExplainDag"),
        group: g.ai,
        icon: <LuMessageCircleQuestion />,
        when: aiEnabled,
        run: () => open({ kind: "aiExplainDag" }),
      },
      {
        id: "ai.review",
        title: t("menu.aiReview"),
        group: g.ai,
        icon: <LuClipboardCheck />,
        when: visualDoc,
        run: () => open({ kind: "aiReview" }),
      },
      {
        id: "ai.leaks",
        title: t("menu.aiLeaks"),
        group: g.ai,
        icon: <LuShieldAlert />,
        when: visualDoc,
        run: () => open({ kind: "aiLeaks" }),
      },
      {
        id: "ai.optimize",
        title: t("menu.aiOptimize"),
        group: g.ai,
        icon: <LuGauge />,
        when: visualDoc,
        run: () => open({ kind: "aiOptimize" }),
      },
      {
        id: "ai.fix",
        title: t("menu.aiFix"),
        group: g.ai,
        icon: <LuWrench />,
        when: () => aiEnabled() && visualDoc(),
        run: () => open({ kind: "aiFix" }),
      },
      {
        id: "ai.taskCode",
        title: t("menu.aiTaskCode"),
        group: g.ai,
        icon: <LuSquareCode />,
        when: () => {
          const st = useBuilder.getState();
          return aiEnabled() && st.nodes.some((n) => n.id === st.selectedId && n.type === "python_task");
        },
        run: () => {
          const id = useBuilder.getState().selectedId;
          if (id) open({ kind: "aiTaskCode", nodeId: id });
        },
      },
      {
        id: "ai.ask",
        title: t("menu.aiAsk"),
        group: g.ai,
        icon: <LuBotMessageSquare />,
        when: aiEnabled,
        run: () => open({ kind: "aiAsk" }),
      },
      {
        id: "ai.writer",
        title: t("menu.aiWriter"),
        group: g.ai,
        icon: <LuSquareCode />,
        when: aiEnabled,
        run: () => open({ kind: "aiWriter" }),
      },
      {
        id: "ai.myModel",
        title: t("menu.aiMyModel"),
        group: g.ai,
        icon: <LuBotMessageSquare />,
        when: () => (getAiStatus()?.llm_mode ?? "central") !== "central",
        run: () => open({ kind: "aiModel" }),
      },
      {
        id: "ai.knowledge",
        title: t("menu.aiKnowledge"),
        group: g.ai,
        icon: <LuBookOpen />,
        when: () => getAiStatus()?.role === "admin",
        run: () => open({ kind: "aiKnowledge" }),
      },
      {
        id: "ai.writeDocs",
        title: t("menu.aiDocs"),
        group: g.ai,
        icon: <LuBookOpen />,
        when: () => aiEnabled() && visualDoc(),
        run: aiWriteDocs,
      },
      {
        id: "import.open",
        title: t("menu.import"),
        group: g.file,
        icon: <LuFileInput />,
        run: () => open({ kind: "import", source: "python" }),
      },
      {
        id: "versions.open",
        title: t("menu.history"),
        group: g.versions,
        icon: <LuHistory />,
        run: () => useLifecycle.getState().setBottomTab("versions"),
      },
      {
        id: "git.status",
        title: t("menu.storage"),
        group: g.versions,
        icon: <LuDatabase />,
        run: () => open({ kind: "storage" }),
      },
      {
        id: "git.promote",
        title: t("menu.promote"),
        group: g.versions,
        icon: <LuGitPullRequestArrow />,
        when: deployed,
        run: () => open({ kind: "promote" }),
      },
      {
        id: "runner.testTask",
        title: t("menu.testTask"),
        group: g.run,
        icon: <LuFlaskConical />,
        when: () => selectedTask() !== null,
        run: () => open({ kind: "testTask", nodeId: selectedTask() }),
      },
      {
        id: "runner.testDag",
        title: t("menu.testDag"),
        group: g.run,
        icon: <LuFileCode />,
        run: () => open({ kind: "testTask", nodeId: selectedTask(), whole: true }),
      },
    ];
    return registerCommands(...commands);
  }, [t, i18n.language]);
}

/** Opens the DAG requested by the host page / URL on startup, and follows hash changes. */
function useDeepLink(): void {
  useEffect(() => {
    let cancelled = false;
    const openRequested = async (dagId: string, confirm: boolean) => {
      const res = await openDag(dagId, { confirm, quiet404: true });
      if (cancelled || res !== "not_found") return;
      // A DAG that exists only as Python in Airflow (not managed by AutoAir): offer to import it.
      const inAirflow =
        hostDagId() === dagId ||
        !!(await getDagStatus({ path: { dag_id: dagId }, throwOnError: false }).catch(() => null))?.data?.exists;
      if (cancelled) return;
      if (inAirflow) {
        useBuilder.getState().reset(dagId);
        useLifecycle.getState().setUnmanagedDagId(dagId);
      } else {
        syncDeepLink(null);
        open({ kind: "open" });
      }
    };
    const first = initialDagId();
    if (first) void openRequested(first, false);
    const onHash = () => {
      if (hostDagId()) return;
      const id = initialDagId();
      if (id && id !== useBuilder.getState().savedDagId) void openRequested(id, true);
    };
    window.addEventListener("hashchange", onHash);
    window.addEventListener("popstate", onHash);
    return () => {
      cancelled = true;
      window.removeEventListener("hashchange", onHash);
      window.removeEventListener("popstate", onHash);
    };
  }, []);
}

/** Mounts lifecycle commands, deep links and dialogs (inside the query + React Flow providers). */
export function LifecycleHost() {
  const queryClient = useQueryClient();
  const { fitView } = useReactFlow();
  useEffect(() => {
    runtime.queryClient = queryClient;
    runtime.fitView = () => void fitView({ padding: 0.2, duration: 200 });
  }, [queryClient, fitView]);
  useLifecycleCommands();
  useDeepLink();
  useRunOverlaySync();
  const dialog = useLifecycle((s) => s.dialog);
  const unmanaged = useLifecycle((s) => s.unmanagedDagId);
  return (
    <Suspense fallback={null}>
      {dialog ? (
        <ErrorBoundary key={dialog.kind} area="dialog" onReset={() => useLifecycle.getState().closeDialog()}>
          {renderDialog(dialog)}
        </ErrorBoundary>
      ) : null}
      {unmanaged && !dialog ? <UnmanagedDialog dagId={unmanaged} /> : null}
    </Suspense>
  );
}
