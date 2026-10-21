// Deterministic demo data for the captures, written through AutoAir's REST API.
import { API, AIRFLOW } from "./env.mjs";

const BASH = "airflow.providers.standard.operators.bash.BashOperator";
const EMPTY = "airflow.providers.standard.operators.empty.EmptyOperator";
const SQL = "airflow.providers.common.sql.operators.sql.SQLExecuteQueryOperator";
const S3_SENSOR = "airflow.providers.amazon.aws.sensors.s3.S3KeySensor";
const SLACK = "airflow.providers.slack.operators.slack_webhook.SlackWebhookOperator";

export const SHOWCASE = "sales_reporting";
/** A real DAG of the shared Airflow with successful and failed runs (read-only use). */
export const AIRFLOW_DAG = "etl_full";

export async function airflowToken() {
  const r = await fetch(`${AIRFLOW}/auth/token`);
  if (!r.ok) throw new Error(`Airflow /auth/token: ${r.status}`);
  return (await r.json()).access_token;
}

function api(token) {
  const call = async (method, path, body) => {
    const r = await fetch(`${API}${path}`, {
      method,
      headers: { Authorization: `Bearer ${token}`, "content-type": "application/json", "x-autoair-csrf": "1" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const text = await r.text();
    if (!r.ok) throw new Error(`${method} ${path}: ${r.status} ${text.slice(0, 400)}`);
    return text ? JSON.parse(text) : null;
  };
  return {
    get: (p) => call("GET", p),
    put: (p, b) => call("PUT", p, b),
    post: (p, b) => call("POST", p, b),
    del: (p) => call("DELETE", p),
  };
}

/** The showcase DAG: sensor → task group of two extracts → Python transform → load → Slack. */
function showcaseSpec({ retries = 2, slackMessage = "Daily sales for {{ ds }} are loaded." } = {}) {
  return {
    spec_version: 2,
    dag_id: SHOWCASE,
    display_name: "Sales reporting",
    description: "Daily sales report: raw orders from S3 + CRM customers → warehouse",
    doc_md: "# Sales reporting\n\nBuilds `analytics.daily_sales` every morning.\n\n- **Owner:** data-platform\n- **Runbook:** re-run `load_warehouse` after fixing the source.",
    schedule: { type: "cron", expression: "0 6 * * *", timezone: "Europe/Istanbul" },
    start_date: "2026-01-01T00:00:00Z",
    catchup: false,
    tags: ["sales", "reporting", "autoair"],
    max_active_runs: 1,
    default_args: { owner: "data-platform", retries, retry_delay_seconds: 300 },
    environments: {
      dev: { schedule: { type: "none" }, is_paused_upon_creation: true, tags: ["dev"] },
      prod: {
        schedule: { type: "cron", expression: "0 5 * * *", timezone: "Europe/Istanbul" },
        default_args: { retries: 4 },
        params_defaults: { batch_size: 20000 },
      },
    },
    params: {
      region: {
        type: "string",
        default: "emea",
        enum: ["emea", "amer", "apac"],
        title: "Region",
        description: "Sales region to report on",
      },
      full_refresh: { type: "boolean", default: false, title: "Full refresh", description: "Rebuild the whole table" },
      batch_size: { type: "integer", default: 5000, minimum: 100, title: "Batch size" },
    },
    nodes: [
      {
        kind: "operator",
        id: "wait",
        task_id: "wait_for_orders",
        operator: S3_SENSOR,
        params: {
          bucket_key: "s3://acme-raw/orders/{{ ds }}/_SUCCESS",
          aws_conn_id: "aws_default",
          mode: "reschedule",
          poke_interval: 300,
        },
        position: { x: 0, y: 147 },
      },
      { kind: "task_group", id: "grp", group_id: "extract", position: { x: 290, y: 60, width: 270, height: 230 } },
      {
        kind: "operator",
        id: "orders",
        task_id: "extract_orders",
        operator: BASH,
        parent_group: "grp",
        params: { bash_command: "python -m etl.extract orders --date {{ ds }} --region {{ params.region }}" },
        position: { x: 35, y: 50 },
      },
      {
        kind: "operator",
        id: "customers",
        task_id: "extract_customers",
        operator: SQL,
        parent_group: "grp",
        params: {
          conn_id: "warehouse",
          sql: "SELECT id, segment, country\nFROM crm.customers\nWHERE updated_at >= '{{ data_interval_start }}'",
        },
        position: { x: 35, y: 140 },
      },
      {
        kind: "python_task",
        id: "transform",
        task_id: "build_daily_sales",
        function_name: "build_daily_sales",
        code:
          'from airflow.sdk import get_current_context\n\nctx = get_current_context()\norders = ctx["ti"].xcom_pull(task_ids="extract.extract_orders")\nreturn {"rows": len(orders or []), "region": ctx["params"]["region"]}',
        position: { x: 630, y: 147 },
      },
      {
        kind: "operator",
        id: "load",
        task_id: "load_warehouse",
        operator: SQL,
        params: {
          conn_id: "warehouse",
          sql: "INSERT INTO analytics.daily_sales\nSELECT * FROM staging.daily_sales WHERE ds = '{{ ds }}'",
        },
        retries: 3,
        retry_policy: {
          rules: [
            {
              exception: "psycopg2.OperationalError",
              action: "retry",
              retry_delay_seconds: 60,
              reason: "The warehouse dropped the connection",
            },
            { exception: "builtins.ValueError", action: "fail", reason: "Bad input: retrying will not help" },
          ],
          default: "default",
        },
        callbacks: {
          on_failure: [
            {
              notifier: "airflow.providers.slack.notifications.slack.SlackNotifier",
              params: { slack_conn_id: "slack_sales", text: "load_warehouse failed for {{ ds }}", channel: "#sales-oncall" },
            },
          ],
        },
        position: { x: 910, y: 147 },
      },
      {
        kind: "operator",
        id: "notify",
        task_id: "notify_team",
        operator: SLACK,
        params: { slack_webhook_conn_id: "slack_sales", message: slackMessage },
        position: { x: 1190, y: 147 },
      },
    ],
    edges: [
      { id: "e1", source: "wait", target: "grp" },
      { id: "e2", source: "grp", target: "transform" },
      { id: "e3", source: "transform", target: "load" },
      { id: "e4", source: "load", target: "notify" },
    ],
  };
}

/** A second, smaller DAG so the Open dialog / history have more than one entry. */
function inventorySpec() {
  return {
    spec_version: 2,
    dag_id: "inventory_sync",
    description: "Hourly inventory sync from the ERP",
    schedule: { type: "preset", preset: "@hourly" },
    start_date: "2026-01-01T00:00:00Z",
    tags: ["inventory"],
    default_args: { owner: "ops" },
    nodes: [
      { kind: "operator", id: "a", task_id: "start", operator: EMPTY, position: { x: 0, y: 0 } },
      {
        kind: "operator",
        id: "b",
        task_id: "pull_erp",
        operator: BASH,
        params: { bash_command: "erp-cli export --since {{ data_interval_start }}" },
        position: { x: 280, y: 0 },
      },
    ],
    edges: [{ id: "e1", source: "a", target: "b" }],
  };
}

async function deploy(a, spec, message, prDescription) {
  let base;
  try {
    base = (await a.get(`/specs/${spec.dag_id}`)).etag;
  } catch {
    base = null;
  }
  const res = await a.put(`/specs/${spec.dag_id}`, {
    spec,
    base_etag: base ?? null,
    message,
    pr_description: prDescription ?? null,
  });
  if (!res.deployed) throw new Error(`deploy ${spec.dag_id}: ${JSON.stringify(res).slice(0, 600)}`);
  return res;
}


/** Seeds specs: two versions of the showcase (a diff to show), a second DAG and a review.
 * Run it against the server in git *push* mode, so they land on main with a history. */
export async function seed(token) {
  const a = api(token);
  await deploy(a, showcaseSpec({ retries: 1, slackMessage: "Sales loaded." }), "Add the sales reporting DAG");
  await deploy(
    a,
    showcaseSpec(),
    "Retry twice and post a richer Slack message",
    "The Slack message now carries the run date; retries go from 1 to 2.",
  );
  await deploy(a, inventorySpec(), "Add the hourly inventory sync");
  // A DAG that already runs in the shared Airflow, imported (read-only there) so the Runs
  // tab, the run overlay, task logs and "Analyze with AI" have real runs to show.
  const imported = await a.post(`/interchange/airflow/${AIRFLOW_DAG}/import`);
  const spec = imported.dags[0].spec;
  // Importer quirk: a mapped task comes back as "<module>.MappedOperator" (reported).
  for (const n of spec.nodes) {
    if (n.operator?.endsWith(".bash.MappedOperator")) {
      n.operator = BASH;
      n.params = { bash_command: 'echo "process {{ ti.map_index }}"', ...n.params };
    }
  }
  await deploy(a, spec, `Import ${AIRFLOW_DAG} from Airflow`);
  const saved = await a.get(`/specs/${SHOWCASE}`);
  try {
    await a.post(`/specs/${SHOWCASE}/reviews`, {
      etag: saved.etag,
      verdict: "comment",
      comment: "Looks good. Can we alert #sales-oncall when load_warehouse fails?",
    });
  } catch (e) {
    console.warn("  (review seed skipped)", e.message.slice(0, 160));
  }
  return { etag: saved.etag };
}
