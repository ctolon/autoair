/**
 * Bulk import of a DAG repository (POST /interchange/bulk/scan): turning a browser folder
 * pick into repository-relative files, and planning which converted DAGs to deploy. A DAG
 * that already exists is only deployed when the user confirmed overwriting it, and then
 * against the stored version it was compared with (base_etag).
 */
import type { BulkImportResult } from "../api/gen/types.gen";

export type PickedFile = { path: string; source: string };

/** `webkitRelativePath` "repo/dags/a.py" → "dags/a.py" (the picked folder is the root). */
export function relativePath(webkitRelativePath: string, name: string): string {
  const p = (webkitRelativePath || name).replace(/\\/g, "/");
  const i = p.indexOf("/");
  return i >= 0 ? p.slice(i + 1) : p;
}

/** Only Python files are sent; hidden folders / virtualenvs are dropped client-side. */
export function wantedFile(path: string): boolean {
  if (!path.endsWith(".py")) return false;
  return !path.split("/").some((part) => part.startsWith(".") || part === "node_modules" || part === "__pycache__");
}

type PlannedDeploy = { dagId: string; path: string; overwrite: boolean; baseEtag: string | null };

/** Deploys for the selected DAG ids; existing ones only when also confirmed. */
export function planDeploys(
  result: BulkImportResult,
  selected: ReadonlySet<string>,
  confirmed: ReadonlySet<string>,
): { deploys: PlannedDeploy[]; skipped: string[] } {
  const deploys: PlannedDeploy[] = [];
  const skipped: string[] = [];
  const seen = new Set<string>();
  for (const file of result.files) {
    for (const dag of file.dags ?? []) {
      if (!dag.spec || !selected.has(dag.dag_id) || seen.has(dag.dag_id)) continue;
      seen.add(dag.dag_id);
      if (dag.exists && !confirmed.has(dag.dag_id)) {
        skipped.push(dag.dag_id);
        continue;
      }
      deploys.push({
        dagId: dag.dag_id,
        path: file.path,
        overwrite: !!dag.exists,
        baseEtag: dag.exists ? (dag.stored_etag ?? null) : null,
      });
    }
  }
  return { deploys, skipped };
}

/** DAG ids defined by more than one file (only the first can be imported). */
export function duplicateDagIds(result: BulkImportResult): Set<string> {
  const count = new Map<string, number>();
  for (const f of result.files) for (const d of f.dags ?? []) count.set(d.dag_id, (count.get(d.dag_id) ?? 0) + 1);
  return new Set([...count].filter(([, n]) => n > 1).map(([id]) => id));
}

/** Initially selected: every convertible DAG that does not exist yet. */
export function defaultSelection(result: BulkImportResult): Set<string> {
  const out = new Set<string>();
  for (const f of result.files) for (const d of f.dags ?? []) if (d.spec && !d.exists) out.add(d.dag_id);
  return out;
}
