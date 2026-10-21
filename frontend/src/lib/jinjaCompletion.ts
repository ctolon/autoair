/**
 * Airflow-aware completion for Jinja templates (pure; wrapped by the CodeMirror editor).
 */
import type { JinjaCompletionData } from "../widgets/code/types";
import { FILTER_NAMES } from "./jinja";

type JinjaCompletion = {
  label: string;
  type: "variable" | "function" | "property" | "method" | "constant" | "keyword" | "text";
  detail?: string;
  info?: string;
  /** Text inserted instead of the label. */
  apply?: string;
};

type JinjaCompletionResult = { from: number; options: JinjaCompletion[] };

const v = (label: string, detail: string, type: JinjaCompletion["type"] = "variable"): JinjaCompletion => ({
  label,
  type,
  detail,
});

/** Top-level names of the Airflow 3 task context. */
const CONTEXT_COMPLETIONS: JinjaCompletion[] = [
  v("ds", "logical date YYYY-MM-DD"),
  v("ds_nodash", "logical date YYYYMMDD"),
  v("ts", "logical date ISO 8601"),
  v("ts_nodash", "YYYYMMDDTHHMMSS"),
  v("ts_nodash_with_tz", "YYYYMMDDTHHMMSS+0000"),
  v("logical_date", "pendulum DateTime"),
  v("data_interval_start", "pendulum DateTime"),
  v("data_interval_end", "pendulum DateTime"),
  v("run_id", "DAG run id"),
  v("dag", "the DAG object"),
  v("dag_run", "the DagRun"),
  v("params", "DAG / task params"),
  v("var", "Airflow Variables"),
  v("conn", "Airflow connections"),
  v("ti", "the TaskInstance"),
  v("task_instance", "the TaskInstance"),
  v("task", "the operator"),
  v("macros", "airflow.macros"),
  v("conf", "Airflow configuration"),
  v("task_instance_key_str", "dag__task__YYYYMMDD"),
  v("partition_key", "partitioned runs (Airflow ≥ 3.2)"),
  v("triggering_asset_events", "asset events that triggered the run"),
  v("outlet_events", "attach extra / aliases to outlet events"),
  v("inlet_events", "events of the task inlets"),
  v("expanded_ti_count", "mapped task count"),
  v("test_mode", "airflow tasks test"),
];

const MACROS: JinjaCompletion[] = [
  { label: "ds_add", type: "function", detail: "(ds, days)", apply: "ds_add(ds, 1)" },
  {
    label: "ds_format",
    type: "function",
    detail: "(ds, input_format, output_format)",
    apply: "ds_format(ds, '%Y-%m-%d', '%d/%m/%Y')",
  },
  { label: "datetime", type: "function", detail: "datetime.datetime" },
  { label: "timedelta", type: "function", detail: "datetime.timedelta", apply: "timedelta(days=1)" },
  { label: "time", type: "property", detail: "time module" },
  { label: "uuid", type: "property", detail: "uuid module" },
  { label: "random", type: "property", detail: "random module" },
  { label: "dateutil", type: "property", detail: "dateutil module" },
  { label: "json", type: "property", detail: "json module" },
];

const DAG_RUN: JinjaCompletion[] = [
  v("conf", "trigger configuration", "property"),
  v("run_id", "run id", "property"),
  v("run_type", "scheduled | manual | asset_triggered | backfill", "property"),
  v("logical_date", "pendulum DateTime", "property"),
  v("data_interval_start", "pendulum DateTime", "property"),
  v("data_interval_end", "pendulum DateTime", "property"),
  v("start_date", "run start", "property"),
  v("dag_id", "DAG id", "property"),
];

const DATE_MEMBERS: JinjaCompletion[] = [
  { label: "strftime", type: "method", detail: "(format)", apply: "strftime('%Y-%m-%d')" },
  { label: "isoformat", type: "method", detail: "()", apply: "isoformat()" },
  { label: "add", type: "method", detail: "(days=…, hours=…)", apply: "add(days=1)" },
  { label: "subtract", type: "method", detail: "(days=…, hours=…)", apply: "subtract(days=1)" },
  { label: "start_of", type: "method", detail: "('day'|'month'|…)", apply: "start_of('day')" },
  { label: "end_of", type: "method", detail: "('day'|'month'|…)", apply: "end_of('day')" },
  v("year", "int", "property"),
  v("month", "int", "property"),
  v("day", "int", "property"),
  v("hour", "int", "property"),
  v("minute", "int", "property"),
  { label: "timestamp", type: "method", detail: "()", apply: "timestamp()" },
];

const CONN_MEMBERS: JinjaCompletion[] = [
  "host",
  "port",
  "login",
  "password",
  "schema",
  "conn_type",
  "extra",
  "extra_dejson",
].map((m) => v(m, "connection attribute", "property"));

const TASK_MEMBERS: JinjaCompletion[] = ["task_id", "owner", "retries", "queue", "pool", "dag_id"].map((m) =>
  v(m, "operator attribute", "property"),
);

const DATE_VARS = new Set(["logical_date", "data_interval_start", "data_interval_end"]);

const quote = (s: string) => `'${s.replace(/\\/g, "\\\\").replace(/'/g, "\\'")}'`;

function tiMembers(data: JinjaCompletionData): JinjaCompletion[] {
  const out: JinjaCompletion[] = [];
  for (const u of data.upstream) {
    const call = `xcom_pull(task_ids=${quote(u.taskId)})`;
    out.push({ label: call, type: "method", detail: "return value", apply: call });
    for (const key of u.keys) {
      const keyed = `xcom_pull(task_ids=${quote(u.taskId)}, key=${quote(key)})`;
      out.push({ label: keyed, type: "method", detail: `output “${key}”`, apply: keyed });
    }
  }
  out.push({ label: "xcom_pull", type: "method", detail: "(task_ids=…, key=…)", apply: "xcom_pull(task_ids='')" });
  for (const m of ["task_id", "dag_id", "run_id", "try_number", "map_index"])
    out.push(v(m, "task instance", "property"));
  return out;
}

function membersOf(path: string, data: JinjaCompletionData): JinjaCompletion[] | null {
  if (path === "") return CONTEXT_COMPLETIONS;
  if (path === "params" || path === "dag_run.conf") {
    return path === "params"
      ? data.params.map((p) => ({ label: p.name, type: "property" as const, detail: p.detail ?? "param" }))
      : [];
  }
  if (path === "var")
    return [v("value", "Variable as string", "property"), v("json", "Variable parsed as JSON", "property")];
  if (path === "var.value" || path === "var.json") {
    return data.variables.map((k) => ({ label: k, type: "constant" as const, detail: "Variable" }));
  }
  if (path === "conn") {
    return data.connections.map((c) => ({ label: c.id, type: "constant" as const, detail: c.type ?? "connection" }));
  }
  if (/^conn\.[A-Za-z_]\w*$/.test(path)) return CONN_MEMBERS;
  if (path === "macros") return MACROS;
  if (path === "dag_run") return DAG_RUN;
  if (path === "dag")
    return [v("dag_id", "DAG id", "property"), v("owner", "owner", "property"), v("tags", "tags", "property")];
  if (path === "task") return TASK_MEMBERS;
  if (path === "ti" || path === "task_instance") return tiMembers(data);
  const last = path.split(".").pop() ?? "";
  if (DATE_VARS.has(last) && (path === last || path === `dag_run.${last}`)) return DATE_MEMBERS;
  return null;
}

/** Index of the opening `{{` / `{%` of an unclosed tag ending at the cursor, or -1. */
function openTag(before: string): number {
  const open = Math.max(before.lastIndexOf("{{"), before.lastIndexOf("{%"));
  if (open < 0) return -1;
  const tail = before.slice(open + 2);
  if (tail.includes("}}") || tail.includes("%}")) return -1;
  return open;
}

/** True when the cursor sits inside an (unterminated) string literal of `inner`. */
function inString(inner: string): boolean {
  let quoteChar: string | null = null;
  for (let i = 0; i < inner.length; i++) {
    const c = inner[i];
    if (quoteChar) {
      if (c === "\\") i++;
      else if (c === quoteChar) quoteChar = null;
    } else if (c === '"' || c === "'") quoteChar = c;
  }
  return quoteChar !== null;
}

/**
 * Completions for the text before the cursor. `from` is an index into `before`.
 * Returns null outside of `{{ … }}` / `{% … %}` tags.
 */
export function jinjaCompletions(
  before: string,
  data: JinjaCompletionData,
  explicit = false,
): JinjaCompletionResult | null {
  const open = openTag(before);
  if (open < 0) return null;
  const inner = before.slice(open + 2).replace(/^-/, "");
  if (inString(inner)) return null;

  const filter = /\|\s*([A-Za-z_]\w*)?$/.exec(inner);
  if (filter) {
    const word = filter[1] ?? "";
    const options = FILTER_NAMES.filter((f) => f.startsWith(word)).map((f) => ({
      label: f,
      type: "function" as const,
      detail: "filter",
    }));
    return options.length ? { from: before.length - word.length, options } : null;
  }

  const m = /((?:[A-Za-z_]\w*\.)*)([A-Za-z_]\w*)?$/.exec(inner);
  const prefix = (m?.[1] ?? "").replace(/\.$/, "");
  const word = m?.[2] ?? "";
  if (!prefix && !word && !explicit) {
    // Only offer the context right after the tag opens or after an operator / separator.
    const head = inner.slice(0, inner.length - (m?.[0].length ?? 0)).trimEnd();
    if (head && !/[(,[+\-~=:*/%<>]$|\b(?:and|or|not|if|else|in)$/.test(head)) return null;
  }
  const members = membersOf(prefix, data);
  if (!members) return null;
  const lw = word.toLowerCase();
  const options = members.filter((o) => o.label.toLowerCase().startsWith(lw));
  return options.length ? { from: before.length - word.length, options } : null;
}
