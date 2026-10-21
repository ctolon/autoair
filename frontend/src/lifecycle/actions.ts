import type { DagSpec, Fragment } from "../api/gen/types.gen";
import {
  getSpecAuditQueryKey,
  listDagVersionsQueryKey,
  listSpecsQueryKey,
  listVersionsQueryKey,
} from "../api/gen/@tanstack/react-query.gen";
import {
  deleteSpec,
  deploySpec,
  describeDag,
  exportDagFactory,
  exportYamlDag,
  generateCode,
  getSpec,
  validateSpec,
} from "../api/gen/sdk.gen";
import { errorMessage } from "../api/setup";
import { autoLayout } from "../canvas/layout";
import { guessDagId, rawTemplate } from "../codemode/rawDoc";
import { isRawDag } from "../codemode/store";
import { i18n } from "../i18n";
import { getCommands, runCommand } from "../lib/commands";
import type { BuilderNode } from "../lib/types";
import { type BuilderState, absolutePosition, useBuilder, clearHistory } from "../store/builder";
import { toaster } from "../ui/toaster";
import { syncDeepLink } from "./lib/deepLink";
import { gitTokenErrorCode, gitTokenHeaders } from "./lib/gitToken";
import { downloadText } from "./lib/download";
import { fitViewSoon, runtime } from "./runtime";
import { type DeployOptions, useLifecycle } from "./store";

const t = (key: string, opts?: Record<string, unknown>) => i18n.t(key, { ns: "lifecycle", ...opts });

/** Asks before throwing away unsaved edits. */
export function confirmDiscard(): boolean {
  return !useBuilder.getState().dirty || window.confirm(t("confirm.discard"));
}

const newDagId = () => `dag_${Date.now().toString(36)}`;

export function newDag(): void {
  if (!confirmDiscard()) return;
  useBuilder.getState().reset(newDagId());
  useLifecycle.getState().setSelectedRunId(null);
  syncDeepLink(null);
}

/** A new raw code DAG: an unsaved document whose DAG file is written by hand. */
export function newRawDag(): void {
  if (!confirmDiscard()) return;
  const dagId = newDagId();
  useBuilder.getState().reset(dagId);
  useBuilder.setState((s) => ({ dag: { ...s.dag, raw_code: rawTemplate(dagId) } }));
  useLifecycle.getState().setSelectedRunId(null);
  syncDeepLink(null);
}

/** Opens a hand-written DAG file as an unsaved raw code DAG (nothing is executed). */
export function openAsRawDag(source: string, fallbackName: string): void {
  if (!confirmDiscard()) return;
  const dagId = guessDagId(source, fallbackName);
  useBuilder.getState().reset(dagId);
  useBuilder.setState((s) => ({ dag: { ...s.dag, raw_code: source }, dirty: true }));
  useLifecycle.getState().setSelectedRunId(null);
  syncDeepLink(null);
}

/**
 * Loads a document as unsaved work (generated, imported, restored…). The current DAG's
 * storage identity is kept when the dag_id is unchanged, so a deploy updates it in place.
 */
export function loadAsUnsaved(spec: DagSpec, opts: { keepIdentity?: boolean } = {}): void {
  const st = useBuilder.getState();
  const keep = (opts.keepIdentity ?? true) && st.savedDagId === spec.dag_id;
  const identity = {
    savedDagId: st.savedDagId,
    baseEtag: st.baseEtag,
    baseSpec: st.baseSpec,
    pendingPrUrl: st.pendingPrUrl,
  };
  st.load(spec, false);
  useBuilder.setState({ dirty: true, ...(keep ? identity : {}) });
  fitViewSoon();
}

type OpenResult = "opened" | "not_found" | "error" | "cancelled";

/** Opens a stored spec by dag_id. */
export async function openDag(
  dagId: string,
  opts: { confirm?: boolean; quiet404?: boolean } = {},
): Promise<OpenResult> {
  if ((opts.confirm ?? true) && !confirmDiscard()) return "cancelled";
  const res = await getSpec({ path: { dag_id: dagId }, throwOnError: false });
  if (res.data) {
    useBuilder.getState().load(res.data.spec, true, {
      etag: res.data.etag,
      pendingPrUrl: res.data.pending_pr_url ?? null,
    });
    useLifecycle.getState().setSelectedRunId(null);
    useLifecycle.getState().setUnmanagedDagId(null);
    syncDeepLink(dagId);
    if (needsLayout(res.data.spec)) await layoutLoaded();
    else fitViewSoon();
    return "opened";
  }
  if (res.response?.status === 404) {
    if (!opts.quiet404) toaster.create({ type: "error", title: t("open.notFound", { dagId }) });
    return "not_found";
  }
  toaster.create({ type: "error", title: t("open.failed", { dagId }), description: errorMessage(res.error) });
  return "error";
}

async function invalidateAfterSave(dagId: string): Promise<void> {
  const qc = runtime.queryClient;
  if (!qc) return;
  await Promise.all([
    qc.invalidateQueries({ queryKey: listSpecsQueryKey() }),
    qc.invalidateQueries({ queryKey: listVersionsQueryKey({ path: { dag_id: dagId } }) }),
    qc.invalidateQueries({ queryKey: getSpecAuditQueryKey({ path: { dag_id: dagId } }) }),
    qc.invalidateQueries({ queryKey: listDagVersionsQueryKey({ path: { dag_id: dagId } }) }),
  ]);
}

let deploying = false;

/**
 * Deploys the current document (or `opts.spec`). On an optimistic-locking conflict the
 * merge dialog opens; a hand-edited (ejected) file asks for a forced overwrite.
 */
/**
 * Deploy the current DAG. `secret.gitToken` (storage.git.request_token) only travels in the
 * `X-AutoAir-Git-Token` header of this request: it is deliberately not part of
 * `DeployOptions`, which the conflict dialog keeps in the lifecycle store.
 */
export async function deploy(opts: DeployOptions = {}, secret: { gitToken?: string | null } = {}): Promise<boolean> {
  if (deploying) return false;
  const headers = gitTokenHeaders(secret.gitToken);
  const st = useBuilder.getState();
  const spec = opts.spec ?? st.toSpec();
  if (st.problems.some((p) => p.severity === "error") && !opts.spec) {
    toaster.create({ type: "error", title: t("deploy.fixErrors") });
    return false;
  }
  const renamed = !!st.savedDagId && st.savedDagId !== spec.dag_id;
  const environment = opts.environment !== undefined ? opts.environment : st.environment;
  const baseEtag = opts.baseEtag !== undefined ? opts.baseEtag : renamed ? null : st.baseEtag;
  deploying = true;
  const pending = toaster.create({ type: "loading", title: t("deploy.running", { dagId: spec.dag_id }) });
  try {
    const { data: res } = await deploySpec({
      path: { dag_id: spec.dag_id },
      body: {
        spec,
        base_etag: baseEtag,
        environment,
        message: opts.message || null,
        force: opts.force ?? false,
        ...(opts.directPush != null ? { direct_push: opts.directPush } : {}),
        ...(opts.prDescription ? { pr_description: opts.prDescription } : {}),
        ...(opts.gitConnId ? { git_conn_id: opts.gitConnId } : {}),
        ...(opts.sourceResolution ? { source_resolution: opts.sourceResolution } : {}),
        ...(opts.sourceTargetSha != null ? { source_target_sha: opts.sourceTargetSha } : {}),
      },
      headers,
      throwOnError: true,
    });
    useBuilder.getState().setProblems(res.report.problems);
    if (!res.deployed) {
      // The source policy stopped it: a DAG file changed outside AutoAir, or a change the
      // existing (hand-written) file cannot take.
      if (res.source && (res.status === "source_refused" || res.status === "ejected")) {
        useLifecycle.getState().openDialog({
          kind: "source",
          plan: res.source,
          request: { ...opts, environment, sourceResolution: null, sourceTargetSha: null },
          spec,
        });
      } else if (res.conflict?.ejected_file && !res.conflict.current_spec && !res.conflict.force_allowed) {
        // A changed or kept DAG file the server would not overwrite for this user
        // (preserve.allow_force_overwrite, deploy:overwrite_ejected): say why, offer no force.
        toaster.create({
          type: "error",
          title: res.conflict.hand_written ? t("conflict.handWrittenTitle") : t("conflict.ejectedTitle"),
          description: res.message,
          duration: 12_000,
        });
      } else if (res.conflict?.current_spec || res.conflict?.ejected_file) {
        useLifecycle.getState().openDialog({
          kind: "conflict",
          conflict: res.conflict,
          request: { ...opts, environment },
          spec,
        });
      } else {
        toaster.create({ type: "error", title: t("deploy.notDeployed"), description: res.message });
      }
      return false;
    }
    const prev = useBuilder.getState().savedDagId;
    if (prev && prev !== spec.dag_id) {
      // Renamed DAG: remove the old files so Airflow does not keep both.
      await deleteSpec({ path: { dag_id: prev }, headers, throwOnError: true }).catch((e: unknown) =>
        toaster.create({
          type: "warning",
          title: t("deploy.oldNotRemoved", { dagId: prev }),
          description: errorMessage(e),
        }),
      );
    }
    // A preserve deploy merged changes made to the file outside AutoAir: the stored spec
    // carries them, so the document takes it over (one undoable step).
    if (res.stored_spec) useBuilder.getState().replaceSpec(res.stored_spec);
    useBuilder.getState().markSaved(spec.dag_id, { etag: res.etag ?? null, pendingPrUrl: res.pr_url ?? null });
    syncDeepLink(spec.dag_id);
    await invalidateAfterSave(spec.dag_id);
    toaster.create({
      type: "success",
      title: res.pr_url ? t("deploy.prOpened") : t("deploy.done"),
      description: res.pr_url ? `${res.message}\n${res.pr_url}` : res.message,
      duration: res.pr_url ? 12_000 : 5_000,
    });
    const warnings = (res.source?.warnings ?? []).filter((w) => typeof w === "string");
    if (warnings.length) {
      toaster.create({
        type: "warning",
        title: t("source.deployedWithWarnings"),
        description: warnings.slice(0, 5).join("\n"),
        duration: 10_000,
      });
    }
    return true;
  } catch (e) {
    toaster.create(gitTokenToast(e) ?? { type: "error", title: t("deploy.failed"), description: errorMessage(e) });
    return false;
  } finally {
    toaster.remove(pending);
    deploying = false;
  }
}

/** A friendly toast for a refused git token (401/403 from the host) or a missing one. */
export function gitTokenToast(err: unknown): { type: "error"; title: string; description: string } | null {
  const code = gitTokenErrorCode(err);
  if (code === "git_token_rejected") {
    return { type: "error", title: t("gitToken.rejected"), description: t("gitToken.rejectedHelp") };
  }
  if (code === "git_token_required") {
    return { type: "error", title: t("gitToken.required"), description: t("gitToken.requiredHelp") };
  }
  return null;
}

export async function validateInAirflow(): Promise<void> {
  const pending = toaster.create({ type: "loading", title: t("validate.running") });
  try {
    const { data } = await validateSpec({
      body: useBuilder.getState().toSpec(),
      query: { import_check: true },
      throwOnError: true,
    });
    useBuilder.getState().setProblems(data.problems);
    if (!data.ok) useLifecycle.getState().setBottomTab("problems");
    toaster.create(
      data.ok
        ? { type: "success", title: t("validate.ok"), description: t("validate.okHelp") }
        : {
            type: "error",
            title: t("validate.failed"),
            description: t("validate.count", { count: data.problems.length }),
          },
    );
  } catch (e) {
    toaster.create({ type: "error", title: t("validate.error"), description: errorMessage(e) });
  } finally {
    toaster.remove(pending);
  }
}

// ------------------------------------------------------------------------- exports

export async function exportPython(): Promise<void> {
  const spec = useBuilder.getState().toSpec();
  try {
    const { data } = await generateCode({ body: spec, throwOnError: true });
    if (!data.code) {
      useBuilder.getState().setProblems(data.problems);
      toaster.create({ type: "error", title: t("export.noCode") });
      return;
    }
    downloadText(`${spec.dag_id}.py`, data.code, "text/x-python");
  } catch (e) {
    toaster.create({ type: "error", title: t("export.failed"), description: errorMessage(e) });
  }
}

export async function exportAirflowYaml(): Promise<void> {
  const spec = useBuilder.getState().toSpec();
  try {
    const { data } = await exportYamlDag({ body: spec, throwOnError: true });
    downloadText(`${spec.dag_id}.yaml`, data.yaml, "application/yaml");
    if (data.issues.length) {
      toaster.create({
        type: "warning",
        title: t("export.yamlIssues", { count: data.issues.length }),
        description: data.issues.map((i) => `• ${i.message}`).join("\n"),
        duration: 10_000,
      });
    }
  } catch (e) {
    toaster.create({ type: "error", title: t("export.failed"), description: errorMessage(e) });
  }
}

export async function exportDagFactoryYaml(): Promise<void> {
  const spec = useBuilder.getState().toSpec();
  try {
    const { data } = await exportDagFactory({ body: spec, throwOnError: true });
    downloadText(`${spec.dag_id}.dag-factory.yaml`, data.yaml, "application/yaml");
    if (data.warnings.length) {
      toaster.create({
        type: "warning",
        title: t("export.warnings", { count: data.warnings.length }),
        description: data.warnings.map((w) => `• ${w}`).join("\n"),
        duration: 10_000,
      });
    }
  } catch (e) {
    toaster.create({ type: "error", title: t("export.failed"), description: errorMessage(e) });
  }
}

export function exportSpecJson(): void {
  const spec = useBuilder.getState().toSpec();
  downloadText(`${spec.dag_id}.autoair.json`, `${JSON.stringify(spec, null, 2)}\n`, "application/json");
}

// ------------------------------------------------------------------------- canvas helpers

/** Runs the canvas auto-layout (the canvas command when registered, else ELK directly). */
export async function layoutCanvas(): Promise<void> {
  if (getCommands().some((c) => c.id === "canvas.autoLayout")) {
    runCommand("canvas.autoLayout");
    return;
  }
  const { nodes, edges, applyLayout } = useBuilder.getState();
  applyLayout(await autoLayout(nodes, edges));
  fitViewSoon();
}

type WithInsert = BuilderState & { insertFragment?: (fragment: Fragment, at?: { x: number; y: number }) => void };

/** True when the canvas store can insert fragments (added by the canvas feature). */
export const canInsertFragment = (): boolean =>
  typeof (useBuilder.getState() as WithInsert).insertFragment === "function";

export function insertFragment(fragment: Fragment): boolean {
  const fn = (useBuilder.getState() as WithInsert).insertFragment;
  if (typeof fn !== "function") {
    toaster.create({ type: "error", title: t("templates.insertUnavailable") });
    return false;
  }
  fn(fragment);
  return true;
}

/** Selected canvas nodes as a fragment (internal edges only; groups pull in their members). */
export function selectionFragment(): Fragment | null {
  const { nodes, toSpec } = useBuilder.getState();
  const selected = new Set(nodes.filter((n: BuilderNode) => n.selected).map((n) => n.id));
  if (!selected.size) return null;
  // Members of selected groups come along.
  let grew = true;
  while (grew) {
    grew = false;
    for (const n of nodes) {
      if (n.parentId && selected.has(n.parentId) && !selected.has(n.id)) {
        selected.add(n.id);
        grew = true;
      }
    }
  }
  const spec = toSpec();
  const specNodes = (spec.nodes ?? [])
    .filter((n) => selected.has(n.id))
    .map((n) => {
      if (!n.parent_group || selected.has(n.parent_group)) return n;
      // Detached from an unselected group: positions become absolute.
      const abs = absolutePosition(nodes, n.id);
      return { ...n, parent_group: null, position: { ...n.position, x: Math.round(abs.x), y: Math.round(abs.y) } };
    });
  const specEdges = (spec.edges ?? []).filter((e) => selected.has(e.source) && selected.has(e.target));
  return { nodes: specNodes, edges: specEdges };
}

/** True when a spec carries no usable canvas layout (e.g. imported or generated). */
function needsLayout(spec: DagSpec): boolean {
  const nodes = spec.nodes ?? [];
  if (nodes.length < 2) return false;
  const seen = new Set(nodes.map((n) => `${n.parent_group ?? ""}:${n.position?.x ?? 0},${n.position?.y ?? 0}`));
  return seen.size < nodes.length;
}

/**
 * Lays out a document that was just loaded without a usable layout (written by the API, MCP,
 * AI or an importer). Positions are presentation only: the document stays clean and the
 * layout is not an undo step.
 */
async function layoutLoaded(): Promise<void> {
  const { nodes, edges } = useBuilder.getState();
  const layout = await autoLayout(nodes, edges);
  const st = useBuilder.getState();
  // React Flow replaces the array when it measures nodes; only bail on real edits.
  const ids = (list: { id: string }[]) => list.map((n) => n.id).join();
  if (st.dirty || ids(st.nodes) !== ids(nodes)) return;
  const dirty = st.dirty;
  st.applyLayout(layout);
  useBuilder.setState({ dirty });
  clearHistory();
  fitViewSoon();
}

/** Opens an imported/generated spec as a new unsaved document (laid out when needed). */
export function openImported(spec: DagSpec): boolean {
  if (!confirmDiscard()) return false;
  loadAsUnsaved(spec, { keepIdentity: false });
  useLifecycle.getState().setSelectedRunId(null);
  useLifecycle.getState().setUnmanagedDagId(null);
  syncDeepLink(null);
  if (needsLayout(spec)) requestAnimationFrame(() => void layoutCanvas());
  return true;
}

/** AI: writes the DAG documentation (doc_md) from the current graph. */
export async function aiWriteDocs(): Promise<void> {
  const st = useBuilder.getState();
  // doc_md lives in the visual spec; a raw code DAG documents itself in its source.
  if (isRawDag(st.dag)) return;
  if (st.dag.doc_md && !window.confirm(t("ai.confirmOverwriteDocs"))) return;
  const pending = toaster.create({ type: "loading", title: t("ai.writingDocs") });
  try {
    const { data } = await describeDag({ body: { spec: st.toSpec() }, throwOnError: true });
    useBuilder.getState().setDag({ doc_md: data.doc_md });
    toaster.create({ type: "success", title: t("ai.docsWritten") });
  } catch (e) {
    toaster.create({ type: "error", title: t("ai.failed"), description: errorMessage(e) });
  } finally {
    toaster.remove(pending);
  }
}
