import { describe, expect, it } from "vitest";

import type { DagSpec, TargetStatus } from "../../api/gen/types.gen";
import { fromSpec, toSpec } from "../../store/convert";
import { deployEffect, suggestCommitMessage } from "./deployEffect";

const git = (details: Record<string, unknown>): TargetStatus => ({
  kind: "git",
  location: "https://git/x.git",
  writable: true,
  details,
});

describe("deployEffect", () => {
  it("names the branch and folder of a push", () => {
    expect(deployEffect(git({ base_branch: "main", subdir: "dags", mode: "push" }), { git_mode: "push" })).toEqual({
      kind: "push",
      branch: "main",
      path: "dags",
    });
  });
  it("opens a pull request in pr mode unless pushing directly", () => {
    const storage = git({ base_branch: "dev", mode: "pr" });
    expect(deployEffect(storage, { git_mode: "pr", default_opens_pr: true })).toEqual({ kind: "pr", branch: "dev" });
    expect(deployEffect(storage, { git_mode: "pr", default_opens_pr: true }, true).kind).toBe("push");
  });
  it("writes files for local and object storage", () => {
    expect(deployEffect({ kind: "local", location: "/dags", writable: true }, null)).toEqual({
      kind: "write",
      location: "/dags",
    });
  });
});

const op = (id: string, task_id: string, cmd = "echo") =>
  ({
    id,
    kind: "operator",
    task_id,
    operator: "airflow.providers.standard.operators.bash.BashOperator",
    params: { bash_command: cmd },
    position: { x: 0, y: 0 },
  }) as NonNullable<DagSpec["nodes"]>[number];

describe("suggestCommitMessage", () => {
  const base = { dag_id: "etl", nodes: [op("a", "extract"), op("b", "tmp")] } as DagSpec;
  it("describes a new DAG", () => {
    expect(suggestCommitMessage(base, null)).toBe("Add DAG etl");
  });
  it("lists added, removed and changed tasks (positions ignored)", () => {
    const next = {
      dag_id: "etl",
      nodes: [{ ...op("a", "extract", "echo 2"), position: { x: 99, y: 1 } }, op("c", "load")],
    } as DagSpec;
    expect(suggestCommitMessage(next, base)).toBe("Update etl: add load, remove tmp, change extract");
  });
  it("mentions DAG settings and shortens long lists", () => {
    const many = {
      ...base,
      description: "x",
      nodes: [...(base.nodes ?? []), ...["p", "q", "r", "s"].map((k) => op(k, k))],
    };
    expect(suggestCommitMessage(many, base)).toBe("Update etl: add p, q, r (+1), update DAG settings");
    expect(suggestCommitMessage(base, base)).toBe("Update etl");
  });
});

describe("suggestCommitMessage after a load/edit round trip", () => {
  // The deployed spec as the server returns it: `id` first, no `parent_group`, `null`s.
  const server = {
    dag_id: "etl",
    description: null,
    tags: ["a"],
    nodes: [
      {
        id: "a",
        kind: "operator",
        task_id: "extract",
        operator: "airflow.providers.standard.operators.bash.BashOperator",
        params: { env: { B: "2", A: "1" }, bash_command: "echo" },
        retries: null,
        position: { x: 10, y: 20 },
      },
      {
        id: "b",
        kind: "operator",
        task_id: "load",
        operator: "airflow.providers.standard.operators.empty.EmptyOperator",
        position: { x: 200, y: 20 },
      },
    ],
    edges: [{ source: "a", target: "b" }],
  } as unknown as DagSpec;

  it("reports no task change for an untouched DAG (key order and nulls ignored)", () => {
    const { dag, nodes, edges } = fromSpec(server);
    expect(suggestCommitMessage(toSpec(dag, nodes, edges), server)).toBe("Update etl");
  });

  it("names only the edited task", () => {
    const { dag, nodes, edges } = fromSpec(server);
    const edited = nodes.map((n) =>
      n.id === "a" && n.type === "operator"
        ? { ...n, data: { spec: { ...n.data.spec, params: { bash_command: "echo 2", env: { A: "1", B: "2" } } } } }
        : n,
    );
    expect(suggestCommitMessage(toSpec({ ...dag, tags: ["a"] }, edited, edges), server)).toBe(
      "Update etl: change extract",
    );
  });

  it("still notices a real settings change", () => {
    const { dag, nodes, edges } = fromSpec(server);
    expect(suggestCommitMessage(toSpec({ ...dag, description: "new" }, nodes, edges), server)).toBe(
      "Update etl: update DAG settings",
    );
  });
});
