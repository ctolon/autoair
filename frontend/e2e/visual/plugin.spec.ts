import { mkdirSync, writeFileSync } from "node:fs";

import { type APIRequestContext, type Locator, type Page, expect, test } from "@playwright/test";

import { DeployedDags } from "../cleanup";
import { BULK_RESULT, DAG_ID, PLUGIN_SPEC, prepare, preparePlugin } from "./fixtures";
import { routePluginBundle } from "./pluginBundle";
import { settle, snap as snapPage } from "./shots";

/**
 * Visual regression inside the Airflow UI (Airflow >= 3.1 `react_apps`, Browse -> DAG Builder),
 * light and dark: Airflow's navigation around the plugin, the plugin following Airflow's theme,
 * and the dialogs that only make sense against Airflow (connection form, clear, backfill, asset
 * actions). Config: playwright.visual-plugin.config.ts; run with `npm run test:visual:plugin`
 * (see e2e/visual/README.md).
 */

test.describe.configure({ mode: "serial" });

const API = "/autoair/api";
const ROUTE = "/plugin/dag-builder";

async function authHeaders(request: APIRequestContext): Promise<Record<string, string>> {
  // Simple auth manager with all admins: any caller gets an admin token.
  const token = (await (await request.get("/auth/token")).json()).access_token as string;
  return { Authorization: `Bearer ${token}` };
}

const dags = new DeployedDags("visual-plugin");
test.beforeAll(async ({ request }) => {
  const headers = await authHeaders(request);
  dags.track(API, DAG_ID, headers);
  const put = await request.put(`${API}/specs/${DAG_ID}`, { data: { spec: PLUGIN_SPEC, force: true }, headers });
  expect(put.ok(), await put.text()).toBeTruthy();
  const body = await put.json();
  expect(body.status, JSON.stringify(body.report?.problems ?? body)).toBe("deployed");
});
test.afterAll(async ({ request }) => {
  await dags.cleanup(request);
});
test.beforeEach(async ({ context }) => {
  await routePluginBundle(context);
});

/** Airflow's own theme (next-themes, key `theme`), pinned to the project's colour scheme. */
async function airflowTheme(page: Page): Promise<void> {
  const scheme = test.info().project.use.colorScheme === "dark" ? "dark" : "light";
  await page.addInitScript((s) => {
    try {
      localStorage.setItem("theme", s);
    } catch {
      /* storage unavailable: next-themes follows prefers-color-scheme, emulated per project */
    }
  }, scheme);
}

/**
 * Versions of the installed providers ("apache-airflow-providers-standard 1.20.1", "Airflow
 * >=2.11.0") change with every lock-file bump: masked, the layout around them stays compared.
 */
const versionMasks = (page: Page) => [
  page.getByText(/^apache-airflow-providers-[a-z0-9-]+ \d/),
  page.getByText(/^Airflow >=/),
];

/**
 * The private Airflow listens on a free port picked per run; URLs that show it (trigger
 * endpoint, snippets) get a fixed host before the screenshot so line wrapping stays the same.
 */
async function fixAirflowHost(page: Page): Promise<void> {
  await page.evaluate(() => {
    const host = /\b(?:localhost|127\.0\.0\.1):\d+/g;
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    for (let n = walker.nextNode(); n; n = walker.nextNode()) {
      const fixed = n.nodeValue?.replace(host, "airflow.example:8080");
      if (fixed !== undefined && fixed !== n.nodeValue) n.nodeValue = fixed;
    }
  });
}

async function snap(page: Page, name: string, target?: Locator): Promise<void> {
  await fixAirflowHost(page);
  await snapPage(page, name, target, versionMasks(page));
}

async function openBuilder(page: Page, hash = ""): Promise<void> {
  await airflowTheme(page);
  await prepare(page, { apiBase: null });
  await preparePlugin(page);
  await page.goto(`${ROUTE}${hash}`);
}

async function openDag(page: Page): Promise<void> {
  await openBuilder(page, `#dag=${DAG_ID}`);
  await expect(page.locator(".react-flow__node")).toHaveCount(4, { timeout: 60_000 });
  await page.getByRole("button", { name: "Fit view" }).click();
  await settle(page);
}

async function selectTask(page: Page, taskId: string): Promise<void> {
  await page.locator(".react-flow__node").filter({ hasText: taskId }).click();
  await expect(page.getByRole("textbox", { name: /task id/i }).first()).toHaveValue(taskId);
}

test("empty builder", async ({ page }) => {
  await openBuilder(page);
  await expect(page.getByText("Start your DAG")).toBeVisible({ timeout: 60_000 });
  await settle(page);
  await snap(page, "builder-empty");
});

test("canvas and task inspector", async ({ page }) => {
  await openDag(page);
  await selectTask(page, "extract_orders");
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
  await page.route("**/autoair/api/interchange/bulk/scan", (r) =>
    r.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(BULK_RESULT) }),
  );
  await page.getByRole("button", { name: "File", exact: true }).click();
  await page.getByRole("menuitem", { name: /^Import/ }).click();
  await page.getByRole("menuitem", { name: "From a DAG repository…" }).click();
  const dialog = page.getByRole("dialog");
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

test("new connection form", async ({ page }) => {
  await openDag(page);
  await selectTask(page, "publish_report");
  await page.getByRole("button", { name: "New connection" }).first().click();
  const dialog = page.getByRole("dialog");
  // The SQL operator accepts any conn_type: pick one so the typed (hook-meta) form shows.
  const type = dialog.getByRole("combobox", { name: /Connection type/ });
  await type.fill("postgres");
  await type.blur();
  await expect(dialog.getByText("SSL mode")).toBeVisible({ timeout: 15_000 });
  await settle(page);
  await snap(page, "connection-form", dialog);
});

async function openRuns(page: Page): Promise<void> {
  await openDag(page);
  await page.getByRole("tab", { name: "Runs" }).click();
  await page.getByText("scheduled__2026-03-01T00:00:00+00:00").click();
}

test("clear run dialog", async ({ page }) => {
  await openRuns(page);
  await page.getByRole("button", { name: "Clear run…" }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  await settle(page);
  await snap(page, "clear-dialog", dialog);
});

test("backfill dialog", async ({ page }) => {
  await openRuns(page);
  await page.getByRole("button", { name: "Backfill…" }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  await settle(page);
  await snap(page, "backfill-dialog", dialog);
});

test("asset actions", async ({ page }) => {
  await openDag(page);
  await selectTask(page, "extract_orders");
  await page.getByRole("tab", { name: "Data" }).click();
  await page.getByRole("button", { name: "Edit orders", exact: true }).click();
  const actions = page.getByTestId("asset-actions");
  await expect(actions.getByRole("button", { name: /Materialize/ })).toBeVisible({ timeout: 15_000 });
  await actions.getByRole("button", { name: /Send test event/ }).click();
  await actions.scrollIntoViewIfNeeded();
  await settle(page);
  await snap(page, "asset-actions");
});
