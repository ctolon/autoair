import { mkdirSync, writeFileSync } from "node:fs";

import { type Page, expect, test } from "@playwright/test";

import { BULK_RESULT, DAG_ID, SPEC, prepare } from "./fixtures";
import { settle, snap } from "./shots";

/**
 * Visual regression of the main screens, light and dark (one Playwright project each).
 * Update baselines after an intended UI change: `npm run test:visual -- --update-snapshots`
 * (see e2e/visual/README.md).
 */

test.describe.configure({ mode: "serial" });

test.beforeAll(async ({ request }) => {
  // beforeAll runs once per theme project: the second run overwrites the DAG the first one
  // deployed, which needs its etag (428 without it).
  const current = await request.get(`/api/specs/${DAG_ID}`);
  const base_etag = current.ok() ? ((await current.json()) as { etag?: string }).etag : undefined;
  const put = await request.put(`/api/specs/${DAG_ID}`, {
    data: { spec: SPEC, force: true, ...(base_etag ? { base_etag } : {}) },
  });
  expect(put.ok(), await put.text()).toBeTruthy();
  const body = await put.json();
  expect(body.status, JSON.stringify(body.report?.problems ?? body)).toBe("deployed");
});

async function openDag(page: Page): Promise<void> {
  await prepare(page, { dagId: DAG_ID });
  await page.goto("/");
  await expect(page.locator(".react-flow__node")).toHaveCount(4, { timeout: 30_000 });
  await page.getByRole("button", { name: "Fit view" }).click();
  await settle(page);
}

test("empty builder", async ({ page }) => {
  await prepare(page);
  await page.goto("/");
  await expect(page.getByText("Start your DAG")).toBeVisible({ timeout: 30_000 });
  await settle(page);
  await snap(page, "builder-empty");
});

test("canvas and task inspector", async ({ page }) => {
  await openDag(page);
  await page.locator(".react-flow__node").filter({ hasText: "extract_orders" }).click();
  await expect(page.getByRole("textbox", { name: /task id/i }).first()).toHaveValue("extract_orders");
  await settle(page);
  await snap(page, "canvas-inspector");
});

test("problems tab", async ({ page }) => {
  await openDag(page);
  await page.getByRole("tab", { name: /^Problems/ }).click();
  await expect(page.getByText(/policy rule owner-required/).first()).toBeVisible({ timeout: 15_000 });
  await settle(page);
  await snap(page, "problems");
});

test("generated code", async ({ page }) => {
  await openDag(page);
  await page.getByRole("tab", { name: "Code" }).click();
  await expect(page.getByText("BashOperator(").first()).toBeVisible({ timeout: 15_000 });
  await settle(page);
  await snap(page, "code");
});

test("environment mapping", async ({ page }) => {
  await openDag(page);
  await page.getByRole("tab", { name: "Environments" }).click();
  await page.getByRole("button", { name: "Expand prod" }).click();
  await expect(page.getByText("Connection & Variable mapping")).toBeVisible();
  await expect(page.getByTestId("env-mapping-status")).toBeVisible({ timeout: 15_000 });
  await page.getByText("Connection & Variable mapping").scrollIntoViewIfNeeded();
  await settle(page);
  await snap(page, "environments");
});

test("deploy dialog", async ({ page }) => {
  await openDag(page);
  await page.getByRole("button", { name: "More deploy options" }).click();
  await page.getByRole("menuitem", { name: "Deploy…" }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  await settle(page);
  await snap(page, "deploy-dialog", dialog);
});

test("bulk import result", async ({ page }) => {
  await openDag(page);
  await page.route("**/api/interchange/bulk/scan", (r) =>
    r.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(BULK_RESULT) }),
  );
  await page.getByRole("button", { name: "File", exact: true }).click();
  await page.getByRole("menuitem", { name: /^Import/ }).click();
  await page.getByRole("menuitem", { name: "From a DAG repository…" }).click();
  const dialog = page.getByRole("dialog");
  // A folder pick (webkitdirectory) needs a real directory.
  const repo = test.info().outputPath("repo");
  mkdirSync(`${repo}/dags`, { recursive: true });
  writeFileSync(`${repo}/dags/orders.py`, "import airflow  # dag\n");
  await dialog.getByTestId("bulk-import-folder").setInputFiles(repo);
  await expect(dialog.getByText("1 Python file")).toBeVisible();
  await dialog.getByTestId("bulk-import-scan").click();
  await expect(dialog.getByTestId("bulk-import-result")).toBeVisible();
  await settle(page);
  await snap(page, "bulk-import", dialog);
});

test("airflow tab with the trigger endpoint card", async ({ page }) => {
  await openDag(page);
  await page.getByRole("tab", { name: "Airflow" }).click();
  const card = page.getByText("Trigger endpoint").first();
  await expect(card).toBeVisible({ timeout: 15_000 });
  await card.scrollIntoViewIfNeeded();
  await settle(page);
  await snap(page, "airflow-endpoint");
});
