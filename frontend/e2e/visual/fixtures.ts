/**
 * Fixtures of the visual suite: one deterministic DAG and the Airflow REST answers the UI
 * asks for (mocked, so screenshots never depend on a live Airflow).
 */
import type { Page } from "@playwright/test";

export const DAG_ID = "visual_orders_etl";
const FIXED_NOW = new Date("2026-03-02T09:30:00Z");

export const SPEC = {
  dag_id: DAG_ID,
  description: "Load daily orders into the warehouse",
  schedule: { type: "preset", preset: "@daily" },
  start_date: "2026-01-01T00:00:00",
  tags: ["orders", "visual"],
  params: {
    region: { default: "eu", enum: ["eu", "us"], description: "Sales region" },
    limit: { default: 100, type: "integer", minimum: 1 },
  },
  environments: {
    prod: {
      max_active_runs: 1,
      connections: { warehouse: "warehouse_prod", api: "orders_api_prod" },
      variables: { bucket: "prod.orders.bucket" },
    },
  },
  nodes: [
    {
      kind: "operator",
      id: "start",
      task_id: "start",
      operator: "airflow.providers.standard.operators.empty.EmptyOperator",
      position: { x: 0, y: 120 },
    },
    {
      kind: "operator",
      id: "extract",
      task_id: "extract_orders",
      operator: "airflow.providers.standard.operators.bash.BashOperator",
      params: {
        bash_command: "curl -s {{ conn.api.host }}/orders?region={{ params.region }} > /tmp/orders.json",
      },
      retries: 2,
      position: { x: 260, y: 120 },
    },
    {
      kind: "python_task",
      id: "load",
      task_id: "load_orders",
      function_name: "load_orders",
      code: 'from airflow.sdk import Variable\n\nbucket = Variable.get("bucket")\nreturn {"bucket": bucket}\n',
      position: { x: 520, y: 120 },
    },
    {
      kind: "operator",
      id: "done",
      task_id: "done",
      operator: "airflow.providers.standard.operators.empty.EmptyOperator",
      position: { x: 780, y: 120 },
    },
  ],
  edges: [
    { source: "start", target: "extract" },
    { source: "extract", target: "load" },
    { source: "load", target: "done" },
  ],
};

const json = (body: unknown, status = 200) => ({
  status,
  contentType: "application/json",
  body: JSON.stringify(body),
});

/**
 * Fixed clock, API base, English UI; Airflow REST answers mocked. `apiBase: null` injects no
 * host configuration (the Airflow plugin finds its API itself and opens DAGs by `#dag=`).
 */
export async function prepare(
  page: Page,
  opts: { dagId?: string | null; apiBase?: string | null } = {},
): Promise<void> {
  await page.clock.setFixedTime(FIXED_NOW);
  await page.addInitScript(
    ([dagId, apiBase]) => {
      if (apiBase !== null)
        (window as unknown as { __AUTOAIR_CONFIG__: object }).__AUTOAIR_CONFIG__ = {
          apiBase,
          ...(dagId ? { initialDagId: dagId } : {}),
        };
      try {
        localStorage.setItem("autoair.lang", "en");
      } catch {
        /* storage unavailable: the default language is English */
      }
    },
    [opts.dagId ?? null, opts.apiBase === undefined ? "/api" : opts.apiBase] as const,
  );
  await page.route("**/api/airflow/connections?*", (r) => r.fulfill(json(CONNECTIONS)));
  await page.route("**/api/airflow/connections", (r) =>
    r.request().method() === "GET" ? r.fulfill(json(CONNECTIONS)) : r.fallback(),
  );
  await page.route("**/api/airflow/variables*", (r) => r.fulfill(json(VARIABLES)));
  await page.route("**/api/airflow/pools*", (r) => r.fulfill(json([{ name: "default_pool", slots: 128 }])));
  await page.route("**/api/airflow/capabilities", (r) => r.fulfill(json(CAPABILITIES)));
  await page.route("**/api/airflow/mapping-check", (r) => r.fulfill(json(MAPPING_CHECK)));
  await page.route("**/api/airflow/import-errors*", (r) => r.fulfill(json([])));
  await page.route("**/api/airflow/dags/*/status", (r) => r.fulfill(json(DAG_STATUS)));
  await page.route("**/api/airflow/dags/*/runs?*", (r) => r.fulfill(json(RUNS)));
  await page.route("**/api/airflow/dags/*/versions", (r) => r.fulfill(json([])));
}

const CONNECTIONS = [
  { connection_id: "warehouse_prod", conn_type: "postgres", host: "db.prod.internal" },
  { connection_id: "warehouse_dev", conn_type: "postgres", host: "localhost" },
];
const VARIABLES = [{ key: "prod.orders.bucket" }, { key: "dev.orders.bucket" }];
const CAPABILITIES = {
  version: "3.3.2",
  source: "server",
  materialize: true,
  clear_run_on_latest_version: true,
  clear_only_new: true,
  keep_task_state: false,
  backfill_run_on_latest_version: true,
  scheduling_state: false,
};
const MAPPING_CHECK = {
  environment: "prod",
  connections: [
    { logical: "api", actual: "orders_api_prod", found: false },
    { logical: "warehouse", actual: "warehouse_prod", found: true },
  ],
  variables: [{ logical: "bucket", actual: "prod.orders.bucket", found: true }],
  secrets_backend: false,
};
const DAG_STATUS = {
  dag_id: DAG_ID,
  exists: true,
  is_paused: false,
  file_token: "token",
  fileloc: `/opt/airflow/dags/autoair/${DAG_ID}.py`,
  last_parsed_time: "2026-03-02T09:00:00Z",
  scheduling_state: null,
  latest_runs: [],
};
const RUNS = [
  {
    dag_run_id: "scheduled__2026-03-01T00:00:00+00:00",
    state: "success",
    run_type: "scheduled",
    logical_date: "2026-03-01T00:00:00Z",
    run_after: "2026-03-02T00:00:00Z",
    start_date: "2026-03-02T00:00:05Z",
    end_date: "2026-03-02T00:01:10Z",
  },
];

export const BULK_RESULT = {
  source: "upload",
  summary: { files: 6, imported_files: 3, dags: 3, convertible: 2, existing: 1, errors: 1 },
  files: [
    {
      path: "dags/customers.py",
      status: "imported",
      dags: [{ dag_id: "customers", spec: { dag_id: "customers", nodes: [], edges: [] }, warnings: [], exists: false }],
      import_errors: [],
    },
    {
      path: "dags/orders.py",
      status: "imported",
      dags: [
        {
          dag_id: DAG_ID,
          spec: { dag_id: DAG_ID, nodes: [], edges: [] },
          warnings: ["on_failure_callback is not representable; kept as a reference"],
          exists: true,
          stored_etag: "etag-1",
        },
      ],
      import_errors: [],
    },
    {
      path: "dags/legacy.py",
      status: "imported",
      dags: [{ dag_id: "legacy", spec: null, warnings: [], error: "SubDagOperator is not supported" }],
      import_errors: [],
    },
    {
      path: "dags/broken.py",
      status: "error",
      dags: [],
      import_errors: ["ModuleNotFoundError: No module named 'acme_internal'"],
    },
    { path: "common/helpers.py", status: "no_dag", dags: [], import_errors: [] },
    { path: "tests/test_dags.py", status: "excluded", dags: [], import_errors: [] },
  ],
};

/**
 * The plugin suite's DAG: the same pipeline, with an asset outlet on `extract_orders` (asset
 * actions) and a SQL task whose `conn_id` offers "New connection" (connection form).
 */
export const PLUGIN_SPEC = {
  ...SPEC,
  nodes: SPEC.nodes.map((n) => {
    if (n.id === "extract") return { ...n, outlets: [{ name: "orders" }] };
    if (n.id === "done")
      return {
        kind: "operator",
        id: "done",
        task_id: "publish_report",
        operator: "airflow.providers.common.sql.operators.sql.SQLExecuteQueryOperator",
        params: { conn_id: "warehouse_prod", sql: "SELECT count(*) FROM orders" },
        position: n.position,
      };
    return n;
  }),
};

/** Mocks the plugin screens need on top of `prepare`: assets, connection types, run overlay. */
export async function preparePlugin(page: Page): Promise<void> {
  await page.route("**/autoair/api/airflow/assets", (r) =>
    r.request().method() === "GET" ? r.fulfill(json(ASSETS)) : r.fallback(),
  );
  await page.route("**/autoair/api/airflow/assets?*", (r) => r.fulfill(json(ASSETS)));
  await page.route("**/autoair/api/catalog/connection-types", (r) => r.fulfill(json(CONNECTION_TYPES)));
  await page.route("**/autoair/api/airflow/connections/hook-meta", (r) => r.fulfill(json(HOOK_META)));
  await page.route("**/autoair/api/airflow/dags/*/runs/*/overlay", (r) => r.fulfill(json(RUN_OVERLAY)));
}

const ASSETS = [{ id: 7, name: "orders", uri: "s3://warehouse/orders", group: "asset" }];
const CONNECTION_TYPES = [
  {
    conn_type: "postgres",
    hook_class: "airflow.providers.postgres.hooks.postgres.PostgresHook",
    hook_name: "Postgres",
    provider: "apache-airflow-providers-postgres",
    default_conn_name: "postgres_default",
  },
  { conn_type: "generic", hook_class: null, hook_name: "Generic", provider: "apache-airflow", default_conn_name: null },
];
const HIDDEN = { hidden: true, title: null, placeholder: null };
const HOOK_META = [
  {
    connection_type: "postgres",
    hook_name: "Postgres",
    hook_class_name: "airflow.providers.postgres.hooks.postgres.PostgresHook",
    default_conn_name: "postgres_default",
    standard_fields: {
      description: null,
      url_schema: HIDDEN,
      host: { hidden: false, title: "Host", placeholder: "db.example.com" },
      port: { hidden: false, title: "Port", placeholder: "5432" },
      login: { hidden: false, title: "Login", placeholder: null },
      password: { hidden: false, title: "Password", placeholder: null },
    },
    extra_fields: {
      sslmode: {
        schema: { type: ["string", "null"], title: "SSL mode", enum: ["disable", "require", "verify-full"] },
        description: "libpq sslmode",
        default: "require",
      },
    },
  },
];
const RUN_OVERLAY = { run: RUNS[0], task_instances: [] };
