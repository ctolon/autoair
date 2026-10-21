import { describe, expect, it } from "vitest";

import type { BulkImportResult } from "../api/gen/types.gen";
import { defaultSelection, duplicateDagIds, planDeploys, relativePath, wantedFile } from "./bulkImport";

const spec = (dag_id: string) => ({ dag_id }) as never;

const result: BulkImportResult = {
  source: "upload",
  summary: { files: 3, imported_files: 2, dags: 3, convertible: 2, existing: 1, errors: 0 },
  files: [
    {
      path: "dags/a.py",
      status: "imported",
      dags: [
        { dag_id: "a", spec: spec("a"), warnings: [], exists: false },
        { dag_id: "b", spec: spec("b"), warnings: [], exists: true, stored_etag: "e1" },
      ],
    },
    { path: "dags/c.py", status: "imported", dags: [{ dag_id: "c", spec: null, warnings: [], error: "x" }] },
    { path: "dags/a2.py", status: "imported", dags: [{ dag_id: "a", spec: spec("a"), warnings: [] }] },
  ],
};

describe("bulk import planning", () => {
  it("strips the picked folder and filters files", () => {
    expect(relativePath("repo/dags/a.py", "a.py")).toBe("dags/a.py");
    expect(relativePath("", "a.py")).toBe("a.py");
    expect(wantedFile("dags/a.py")).toBe(true);
    expect(wantedFile(".venv/lib/x.py")).toBe(false);
    expect(wantedFile("dags/readme.md")).toBe(false);
  });

  it("never overwrites an existing DAG without confirmation", () => {
    const all = new Set(["a", "b", "c"]);
    expect(planDeploys(result, all, new Set())).toEqual({
      deploys: [{ dagId: "a", path: "dags/a.py", overwrite: false, baseEtag: null }],
      skipped: ["b"],
    });
    expect(planDeploys(result, all, new Set(["b"])).deploys).toEqual([
      { dagId: "a", path: "dags/a.py", overwrite: false, baseEtag: null },
      { dagId: "b", path: "dags/a.py", overwrite: true, baseEtag: "e1" },
    ]);
  });

  it("selects new convertible DAGs and reports duplicates", () => {
    expect([...defaultSelection(result)]).toEqual(["a"]);
    expect([...duplicateDagIds(result)]).toEqual(["a"]);
  });
});
