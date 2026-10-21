import { describe, expect, it } from "vitest";

import type { JinjaCompletionData } from "../widgets/code/types";
import { jinjaCompletions } from "./jinjaCompletion";

const data: JinjaCompletionData = {
  params: [{ name: "limit", detail: "integer" }, { name: "region" }],
  variables: ["api_key", "bucket"],
  connections: [{ id: "pg_main", type: "postgres" }, { id: "s3" }],
  upstream: [
    { taskId: "extract", keys: ["rows", "path"] },
    { taskId: "grp.clean", keys: [] },
  ],
};
const labels = (before: string, explicit = false) =>
  jinjaCompletions(before, data, explicit)?.options.map((o) => o.label);

describe("jinjaCompletions", () => {
  it("is inactive outside of tags", () => {
    expect(jinjaCompletions("echo ds", data)).toBeNull();
    expect(jinjaCompletions("{{ ds }} after", data)).toBeNull();
    expect(jinjaCompletions("{{ 'some str", data)).toBeNull();
  });

  it("offers the context right after {{", () => {
    const r = jinjaCompletions("run {{ ", data);
    expect(r?.from).toBe(7);
    expect(r?.options.map((o) => o.label)).toEqual(
      expect.arrayContaining([
        "ds",
        "ts",
        "logical_date",
        "params",
        "var",
        "ti",
        "macros",
        "partition_key",
        "triggering_asset_events",
        "outlet_events",
        "inlet_events",
      ]),
    );
  });

  it("filters top-level names by the typed word", () => {
    const r = jinjaCompletions("{{ data_", data);
    expect(r?.from).toBe(3);
    expect(r?.options.map((o) => o.label)).toEqual(["data_interval_start", "data_interval_end"]);
    expect(labels("{{ds_n")).toEqual(["ds_nodash"]);
  });

  it("does not pop up after a complete operand", () => {
    expect(jinjaCompletions("{{ ds ", data)).toBeNull();
    expect(labels("{{ ds ", true)).toContain("ts");
    expect(labels("{{ ds ~ ")).toContain("ts");
  });

  it("completes declared params", () => {
    expect(labels("{{ params.")).toEqual(["limit", "region"]);
    expect(labels("{{ params.re")).toEqual(["region"]);
    expect(jinjaCompletions("{{ params.re", data)?.from).toBe(10);
  });

  it("completes Variables and connections", () => {
    expect(labels("{{ var.")).toEqual(["value", "json"]);
    expect(labels("{{ var.value.")).toEqual(["api_key", "bucket"]);
    expect(labels("{{ var.json.b")).toEqual(["bucket"]);
    expect(jinjaCompletions("{{ conn.", data)?.options).toEqual([
      { label: "pg_main", type: "constant", detail: "postgres" },
      { label: "s3", type: "constant", detail: "connection" },
    ]);
    expect(labels("{{ conn.pg_main.ho")).toEqual(["host"]);
  });

  it("completes xcom_pull for upstream tasks and keys", () => {
    const l = labels("{{ ti.") ?? [];
    expect(l).toContain("xcom_pull(task_ids='extract')");
    expect(l).toContain("xcom_pull(task_ids='extract', key='rows')");
    expect(l).toContain("xcom_pull(task_ids='grp.clean')");
    expect(jinjaCompletions("{{ task_instance.xcom_pull(task_ids='g", data)).toBeNull(); // inside a string
  });

  it("completes macros, dag_run and date members", () => {
    expect(labels("{{ macros.ds")).toEqual(["ds_add", "ds_format"]);
    expect(labels("{{ dag_run.")).toEqual(expect.arrayContaining(["conf", "run_id", "logical_date"]));
    expect(labels("{{ logical_date.str")).toEqual(["strftime"]);
    expect(labels("{{ dag_run.logical_date.")).toContain("isoformat");
  });

  it("completes filters after a pipe", () => {
    expect(labels("{{ ds | up")).toEqual(["upper"]);
    expect(labels("{{ logical_date |")).toEqual(expect.arrayContaining(["ds", "ts_nodash", "default", "tojson"]));
  });

  it("works inside statements", () => {
    expect(labels("{% if params.")).toEqual(["limit", "region"]);
  });

  it("returns null for unknown paths", () => {
    expect(jinjaCompletions("{{ foo.", data)).toBeNull();
  });
});
