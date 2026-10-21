import { expect, test } from "@playwright/test";

import { DeployedDags } from "./cleanup";

/**
 * Operators that take Python objects (python_callable…) are configured with Python references
 * (with the @task.* node suggested as the inline alternative); custom operators from Airflow's
 * plugins folder appear in the palette; both deploy, import and run inside Airflow.
 * The custom operators (`demo_ops`) live in e2e/fixtures/airflow-plugins: copy that folder
 * into `$AIRFLOW_HOME/plugins` of the Airflow under test (CI's E2E job does).
 */
const dags = new DeployedDags("taskflow-equivalents");
test.afterAll(async ({ request }) => {
  await dags.cleanup(request);
});

const CALLABLE_OPERATORS: Record<string, string> = {
  PythonOperator: "@task",
  BranchPythonOperator: "@task.branch",
  ShortCircuitOperator: "@task.short_circuit",
  PythonVirtualenvOperator: "@task.virtualenv",
  BranchPythonVirtualenvOperator: "@task.branch_virtualenv",
  ExternalPythonOperator: "@task.external_python",
  BranchExternalPythonOperator: "@task.branch_external_python",
  PythonSensor: "@task.sensor",
};
const PYOP = "airflow.providers.standard.operators.python.PythonOperator";

test("callable operators insert themselves and take a Python reference", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto("/plugin/dag-builder");
  const search = page.getByPlaceholder(/Search operators/);
  await expect(search).toBeVisible({ timeout: 30_000 });
  const row = (name: string) =>
    page
      .locator('[role="option"]')
      .filter({ has: page.getByText(name, { exact: true }) })
      .first();
  for (const [operator, decorator] of Object.entries(CALLABLE_OPERATORS)) {
    await search.fill(operator);
    await expect(row(operator)).toBeVisible();
    await expect(row(operator)).not.toHaveAttribute("aria-disabled", "true");
    await expect(row(operator)).toContainText(`use a ${decorator} node`);
  }
  // Insert a PythonOperator: the operator itself, python_callable edited as a reference.
  await search.fill("PythonOperator");
  await row("PythonOperator").dblclick();
  const node = page.locator(".react-flow__node").filter({ hasText: "PythonOperator" }).last();
  await expect(node).toBeVisible();
  await node.click({ position: { x: 12, y: 8 } });
  const ref = page.getByRole("textbox", { name: "Python reference for python_callable" });
  await expect(ref).toBeVisible();
  await ref.fill("not a path");
  await expect(page.getByText(/Use an import path/)).toBeVisible();
  await ref.fill("demo_ops.funcs.greet");
  await expect(page.getByText(/Use an import path/)).toHaveCount(0);
  await page.screenshot({ path: "e2e/.results/python-ref.png" });
  expect(errors).toEqual([]);
});

test("custom operators from the plugins folder are in the palette", async ({ page }) => {
  await page.goto("/plugin/dag-builder");
  const search = page.getByPlaceholder(/Search operators/);
  await expect(search).toBeVisible({ timeout: 30_000 });
  await search.fill("Gitlab");
  await expect(page.locator('[role="option"]').filter({ hasText: "GitlabCreateIssueOperator" })).toBeVisible();
  await expect(page.locator('[role="option"]').filter({ hasText: "GitlabMergeRequestSensor" })).toBeVisible();
  await page.locator('[role="option"]').filter({ hasText: "GitlabCreateIssueOperator" }).dblclick();
  await page
    .locator(".react-flow__node")
    .last()
    .click({ position: { x: 12, y: 8 } });
  await expect(page.getByText("custom:plugins").first()).toBeVisible();
  // The custom operator's own parameter is in the form (docs may mention it too).
  await expect(page.locator('[data-autoair-field="project"]').first()).toBeVisible();
});

test("a DAG with a custom operator and a Python reference deploys, imports and runs", async ({ request }) => {
  const token = (await (await request.get("/auth/token")).json()).access_token as string;
  const headers = { Authorization: `Bearer ${token}` };
  const dagId = `custom_ref_${Date.now().toString(36)}`;
  const spec = {
    spec_version: 2,
    dag_id: dagId,
    nodes: [
      {
        kind: "operator",
        id: "issue",
        task_id: "issue",
        operator: "demo_ops.gitlab.GitlabCreateIssueOperator",
        params: { project: "data/etl", title: "Run {{ ds }}", labels: ["etl"] },
      },
      {
        kind: "operator",
        id: "greet",
        task_id: "greet",
        operator: PYOP,
        params: {
          python_callable: { $type: "import", path: "demo_ops.funcs.greet" },
          op_kwargs: { name: "airflow" },
        },
      },
    ],
    edges: [{ source: "issue", target: "greet" }],
  };
  const put = await request.put(`/autoair/api/specs/${dagId}`, { headers, data: { spec } });
  dags.track("/autoair/api", dagId, headers);
  const result = await put.json();
  expect(result.deployed, JSON.stringify(result.report)).toBe(true);
  await expect
    .poll(async () => (await request.get(`/api/v2/dags/${dagId}`, { headers })).status(), { timeout: 60_000 })
    .toBe(200);
  const tasks = (await (await request.get(`/api/v2/dags/${dagId}/tasks`, { headers })).json()).tasks;
  expect(tasks.map((t: { operator_name: string }) => t.operator_name).sort()).toEqual([
    "GitlabCreateIssueOperator",
    "PythonOperator",
  ]);
  const run = await request.post("/autoair/api/runner/test-task", {
    headers,
    data: { spec, task_node_id: "greet" },
  });
  const outcome = await run.json();
  expect(outcome.state, JSON.stringify(outcome)).toBe("success");
  expect(outcome.return_value).toBe("hello airflow");
});
