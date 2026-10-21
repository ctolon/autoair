import { mkdirSync, writeFileSync } from "node:fs";

import { type Locator, type Page, expect, test } from "@playwright/test";

import { BULK_RESULT, DAG_ID, SPEC, prepare } from "./fixtures";

/**
 * Visual regression of the main screens, light and dark (one Playwright project each).
 * Update baselines after an intended UI change: `npm run test:visual -- --update-snapshots`
 * (see e2e/visual/README.md).
 */

test.describe.configure({ mode: "serial" });

test.beforeAll(async ({ request }) => {
  const put = await request.put(`/api/specs/${DAG_ID}`, { data: { spec: SPEC, force: true } });
  expect(put.ok(), await put.text()).toBeTruthy();
  const body = await put.json();
  expect(body.status, JSON.stringify(body.report?.problems ?? body)).toBe("deployed");
});

/**
 * Machine-specific text: the local deploy folder lives in this run's folder (a different path
 * per run and machine). It is rewritten to a fixed placeholder before each screenshot, so the
 * layout (line wrapping) is the same everywhere, and masked.
 */
const RUN_DIR = (process.env.AUTOAIR_VISUAL_DIR ?? "").replaceAll("\\", "/");
const RUN_PLACEHOLDER = "/visual-run";
const masks = (page: Page): Locator[] => [page.getByText(/^local · /), page.getByText(RUN_PLACEHOLDER)];

async function hideRunDir(page: Page): Promise<void> {
  if (!RUN_DIR) return;
  await page.evaluate(
    ([dir, placeholder]) => {
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      for (let n = walker.nextNode(); n; n = walker.nextNode())
        if (n.nodeValue?.includes(dir)) n.nodeValue = n.nodeValue.split(dir).join(placeholder);
    },
    [RUN_DIR, RUN_PLACEHOLDER] as const,
  );
}

async function settle(page: Page): Promise<void> {
  await page.evaluate(() => document.fonts.ready);
  // Live validation / code preview are debounced (600 ms) and the layout runs in a worker.
  await page.waitForLoadState("networkidle");
  await page.waitForTimeout(800);
  await page.mouse.move(0, 0);
}

async function openDag(page: Page): Promise<void> {
  await prepare(page, { dagId: DAG_ID });
  await page.goto("/");
  await expect(page.locator(".react-flow__node")).toHaveCount(4, { timeout: 30_000 });
  await page.getByRole("button", { name: "Fit view" }).click();
  await settle(page);
}

async function snap(page: Page, name: string, target?: Locator): Promise<void> {
  await hideRunDir(page);
  const opts = { mask: masks(page), fullPage: false };
  if (target) await expect(target).toHaveScreenshot(`${name}.png`, opts);
  else await expect(page).toHaveScreenshot(`${name}.png`, opts);
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
