import { type Page, expect, test } from "@playwright/test";

/**
 * Astronomer Blueprint steps: insert a blueprint from the palette's Blueprints section, fill
 * its config in the schema form, see the task preview in the inspector and on the node,
 * collapse the node, switch the version and undo.
 *
 * Needs a stack whose blueprint catalog holds the test templates, e.g. the private stack started
 * with AUTOAIR_BLUEPRINTS__MODULES='["tests.blueprint_templates.etl"]' PYTHONPATH=<repo root>,
 * and AUTOAIR_DEV_URL pointing at its vite preview.
 */
const DEV = process.env.AUTOAIR_DEV_URL ?? "http://localhost:5173";
/** The stack's API for the test's own requests (default: the UI origin's /api). */
const API_BASE = process.env.AUTOAIR_E2E_API_BASE ?? "/api";
// The page always talks to its own origin's /api (vite preview proxies it): the private
// stack's absolute API address is another origin, which the API does not allow (CORS).
const PAGE_API_BASE = "/api";

const nodes = (page: Page) => page.locator(".react-flow__node");
const blueprintNode = (page: Page) => page.getByTestId("blueprint-node");
const field = (page: Page, name: string) => page.locator(`[data-autoair-field="${name}"]`).first();

async function open(page: Page, token: string) {
  await page.addInitScript(
    ([t, api]) => {
      if (api) (window as unknown as { __AUTOAIR_CONFIG__: object }).__AUTOAIR_CONFIG__ = { apiBase: api };
      try {
        sessionStorage.setItem("autoair.airflowToken", t);
        localStorage.setItem("autoair.lang", "en");
      } catch {
        /* about:blank */
      }
    },
    [token, PAGE_API_BASE] as const,
  );
  await page.goto(`${DEV}/`);
  await expect(page.getByPlaceholder(/Search operators/)).toBeVisible({ timeout: 30_000 });
}

test("insert, configure, preview and undo a blueprint step", async ({ page, request }, testInfo) => {
  const dev = await request.get(DEV).catch(() => null);
  test.skip(!dev?.ok(), "UI server not running");
  const api = API_BASE.startsWith("http") ? API_BASE : `${DEV}${API_BASE}`;
  const listed = await request.get(`${api}/blueprints`).catch(() => null);
  const catalog = listed?.ok() ? ((await listed.json()) as { name: string }[]) : [];
  test.skip(!catalog.some((b) => b.name === "extract"), "the test blueprint templates are not in the catalog");
  const tokenResponse = await request.get("/auth/token").catch(() => null);
  const token = tokenResponse?.ok() ? ((await tokenResponse.json()).access_token as string) : "";
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await open(page, token);

  // The palette groups the templates under "Blueprints (Astronomer)" (newest version only).
  await page.getByPlaceholder(/Search operators/).fill("extract");
  await expect(page.getByText("Blueprints (Astronomer)", { exact: true })).toBeVisible();
  const row = page.locator('[role="option"]').filter({ has: page.getByText("extract", { exact: true }) });
  await expect(row).toHaveCount(1);
  await expect(row).toContainText("v2");
  await row.dblclick();
  await expect(blueprintNode(page)).toHaveCount(1);
  await expect(nodes(page)).toHaveCount(1);

  // Version 1 has a single required field; the form follows the version.
  const inspector = page.getByTestId("blueprint-inspector");
  await expect(inspector).toBeVisible();
  await field(page, "version").locator("select").selectOption("1");
  const source = page.locator("#root_source_table");
  await expect(source).toBeVisible({ timeout: 20_000 });
  await source.fill("raw.customers");

  // The preview renders the step's tasks with this config (DagBag worker), in the inspector and on the node.
  const preview = page.getByTestId("blueprint-preview");
  await expect(preview).toContainText("extract_1.validate", { timeout: 60_000 });
  await expect(preview).toContainText("extract_1.extract");
  await expect(blueprintNode(page)).toContainText("extract_1.validate");
  await page.screenshot({ path: testInfo.outputPath("blueprint-step.png") });

  // Collapsing hides the task list; the badge counts the tasks.
  await blueprintNode(page)
    .getByRole("button", { name: /Hide the tasks/ })
    .click();
  await expect(blueprintNode(page)).not.toContainText("extract_1.validate");
  await expect(blueprintNode(page)).toContainText("2 tasks");

  // Undo walks back the collapse, the config edit, the version switch and the insert.
  await page.locator(".react-flow__pane").click({ position: { x: 5, y: 5 } });
  for (let i = 0; i < 6 && (await blueprintNode(page).count()) > 0; i++) {
    await page.keyboard.press("ControlOrMeta+z");
    await page.waitForTimeout(150);
  }
  await expect(nodes(page)).toHaveCount(0);
  expect(errors).toEqual([]);
});
