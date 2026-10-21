import { type APIRequestContext, expect, test } from "@playwright/test";

import { DeployedDags } from "./cleanup";

const DAG_ID = `ui_e2e_${Date.now().toString(36)}`;
// Deployed from the UI: deleted in afterAll even when the test fails half-way.
const dags = new DeployedDags("builder");
test.afterAll(async ({ request }) => {
  await dags.cleanup(request, async () => ({ Authorization: `Bearer ${await apiToken(request)}` }));
});

async function apiToken(request: APIRequestContext): Promise<string> {
  return (await (await request.get("/auth/token")).json()).access_token as string;
}

test("build, deploy, run and inspect a DAG from inside the Airflow UI", async ({ page, request }) => {
  page.on("pageerror", (e) => console.error("pageerror:", e.message));
  page.on("console", (m) => m.type() === "error" && console.error("console:", m.text().slice(0, 400)));

  // Airflow >= 3.1 lists the react_app under "Browse" and renders it natively.
  await page.goto("/");
  await page.getByRole("button", { name: "Browse" }).click();
  await page.getByText("DAG Builder").click();
  await expect(page).toHaveURL(/plugin\/dag-builder/);

  // DAG settings (nothing selected): rename the DAG.
  const dagIdInput = page.getByRole("textbox", { name: "DAG id" });
  await expect(dagIdInput).toBeVisible({ timeout: 30_000 });
  await dagIdInput.fill(DAG_ID);

  // Drag two operators from the palette.
  const canvas = page.locator(".react-flow__pane");
  const drop = async (label: string, x: number, y: number) => {
    await page
      .getByPlaceholder(/Search/)
      .first()
      .fill(label);
    await page.getByText(label, { exact: true }).first().dragTo(canvas, { targetPosition: { x, y } });
  };
  await drop("EmptyOperator", 120, 120);
  await drop("BashOperator", 420, 120);

  // bash_command is a templated field: a Jinja-aware CodeMirror editor.
  const editor = page.locator('[data-autoair-field*="bash_command"] .cm-content').first();
  await editor.click();
  await page.keyboard.type("echo hello from autoair {{ ds }}");

  // Connect empty_1 -> bash_1 by dragging between handles.
  const source = page.locator(".react-flow__node").filter({ hasText: "empty_1" }).locator(".react-flow__handle-right");
  const target = page.locator(".react-flow__node").filter({ hasText: "bash_1" }).locator(".react-flow__handle-left");
  await source.dragTo(target);
  await expect(page.locator(".react-flow__edge")).toHaveCount(1);

  // Command palette (Ctrl/⌘+K) lists registered commands.
  await canvas.click({ position: { x: 600, y: 400 } });
  await page.keyboard.press("ControlOrMeta+k");
  await expect(page.getByPlaceholder(/command|search/i).last()).toBeVisible();
  await page.keyboard.press("Escape");

  // Live validation + generated code come from the backend.
  await page.getByRole("tab", { name: "Code" }).click();
  await expect(page.getByText("BashOperator(", { exact: false }).first()).toBeVisible({ timeout: 20_000 });
  await page.screenshot({ path: "e2e/.results/builder.png" });

  // Deploy (Ctrl/⌘+S works too) and wait for Airflow to parse the DAG.
  dags.track("/autoair/api", DAG_ID);
  await page
    .getByRole("button", { name: /^Deploy/ })
    .first()
    .click();
  await expect(page.getByText("Deployed", { exact: true }).first()).toBeVisible({ timeout: 60_000 });

  const token = await apiToken(request);
  const headers = { Authorization: `Bearer ${token}` };
  await expect
    .poll(async () => (await request.get(`/api/v2/dags/${DAG_ID}`, { headers })).status(), { timeout: 60_000 })
    .toBe(200);

  // Trigger through AutoAir's API proxy and wait for success.
  const trigger = await request.post(`/autoair/api/airflow/dags/${DAG_ID}/trigger`, {
    headers,
    data: { unpause: true },
  });
  const runId = (await trigger.json()).dag_run_id as string;
  await expect
    .poll(
      async () =>
        (await (await request.get(`/api/v2/dags/${DAG_ID}/dagRuns/${encodeURIComponent(runId)}`, { headers })).json())
          .state,
      { timeout: 90_000 },
    )
    .toBe("success");

  // Runs tab: selecting the run paints task states on the canvas.
  await page.getByRole("tab", { name: "Runs" }).click();
  await page.getByText(runId).click();
  await expect(page.locator(".react-flow__node").filter({ hasText: "success" }).first()).toBeVisible({
    timeout: 20_000,
  });
  await page.screenshot({ path: "e2e/.results/deployed.png" });
});
