import { type Page, expect, test } from "@playwright/test";

/**
 * Operator templates (blueprints): save a configured task with a locked and a required field,
 * insert it from the palette (the required input is asked in a form), see the locked field
 * read-only, and get a validation problem when the locked value is changed anyway.
 *
 * Needs a dev server (AUTOAIR_DEV_URL, default http://localhost:5173) whose backend can
 * write templates, plus Airflow for the token.
 */
const DEV = process.env.AUTOAIR_DEV_URL ?? "http://localhost:5173";
const ID = `e2e-notify-${Date.now().toString(36)}`;
/**
 * A production build served by `vite preview` needs the API base (dev servers use /api). The
 * page uses its own origin's /api, which vite preview proxies: the private stack's absolute
 * AUTOAIR_E2E_API_BASE is another origin, which the API does not allow (CORS).
 */
const API_BASE = process.env.AUTOAIR_E2E_API_BASE ? "/api" : null;

const nodes = (page: Page) => page.locator(".react-flow__node");
/** Inspector input of a spec field (fields carry `data-autoair-field`). */
const field = (page: Page, name: string) => page.locator(`[data-autoair-field="${name}"] input`).first();

async function open(page: Page, token: string, theme: "light" | "dark" = "light") {
  await page.addInitScript(
    ([t, th, api]) => {
      if (api) (window as unknown as { __AUTOAIR_CONFIG__: object }).__AUTOAIR_CONFIG__ = { apiBase: api };
      try {
        sessionStorage.setItem("autoair.airflowToken", t);
        localStorage.setItem("autoair.lang", "en");
        localStorage.setItem("autoair.colorMode", th);
      } catch {
        /* about:blank */
      }
    },
    [token, theme, API_BASE] as const,
  );
  await page.goto(`${DEV}/`);
  await expect(page.getByPlaceholder(/Search operators/)).toBeVisible({ timeout: 30_000 });
}

test("save, insert and enforce an operator template", async ({ page, request }, testInfo) => {
  const dev = await request.get(DEV).catch(() => null);
  test.skip(!dev?.ok(), "vite dev server not running");
  const token = (await (await request.get("/auth/token")).json()).access_token as string;
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await open(page, token);

  // A configured BashOperator task.
  await page.getByPlaceholder(/Search operators/).fill("BashOperator");
  await page
    .locator('[role="option"]')
    .filter({ has: page.getByText("BashOperator", { exact: true }) })
    .first()
    .dblclick();
  await expect(nodes(page)).toHaveCount(1);
  await page.getByRole("tab", { name: "Execution" }).click();
  await field(page, "retries").fill("4");
  await field(page, "pool").fill("slack");

  // Right click → Save as operator template: retries locked, pool required.
  await nodes(page).first().click({ button: "right" });
  await page.getByRole("menuitem", { name: "Save as operator template…" }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByText("Save as operator template")).toBeVisible();
  await dialog.getByLabel("Title").fill(`Notify ${ID}`);
  await dialog.getByLabel("Id", { exact: false }).first().fill(ID);
  await dialog.getByLabel("Owner team").fill("platform");
  await dialog.getByLabel("Mode of retries").selectOption("locked");
  await dialog.getByLabel("Mode of pool").selectOption("required");
  await page.screenshot({ path: testInfo.outputPath("operator-template-save.png") });
  await dialog.getByRole("button", { name: "Save" }).click();
  await expect(dialog).toBeHidden();

  // The palette lists it under "Operator templates"; inserting asks for the required pool.
  await page.getByPlaceholder(/Search operators/).fill(ID);
  const row = page.locator('[role="option"]').filter({ hasText: `Notify ${ID}` });
  await expect(row).toBeVisible();
  await expect(page.getByText("Operator templates", { exact: true })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("operator-template-palette.png") });
  await row.dblclick();
  const form = page.getByRole("dialog");
  await expect(form.getByText(`Insert “Notify ${ID}”`)).toBeVisible();
  await expect(form.getByText("Fixed by the template")).toBeVisible();
  const insert = form.getByRole("button", { name: "Insert" });
  await expect(insert).toBeDisabled();
  await form.locator('[data-autoair-field="pool"] input').fill("etl");
  await page.screenshot({ path: testInfo.outputPath("operator-template-insert.png") });
  await insert.click();
  await expect(nodes(page)).toHaveCount(2);

  // The new task: banner, locked retries; editing them is refused.
  await expect(page.getByTestId("template-banner")).toContainText(`Notify ${ID}`);
  await page.getByRole("tab", { name: "Execution" }).click();
  const retries = field(page, "retries");
  await expect(retries).toHaveValue("4");
  await retries.fill("0");
  await expect(retries).toHaveValue("4");
  await expect(field(page, "pool")).toHaveValue("etl");

  // Validation (server side) flags a locked value changed outside the inspector.
  const spec = {
    dag_id: "e2e_tpl",
    nodes: [
      {
        id: "n1",
        kind: "operator",
        task_id: "notify",
        operator: "airflow.providers.standard.operators.bash.BashOperator",
        params: { bash_command: "echo" },
        retries: 0,
        pool: "etl",
        template_ref: { id: ID, version: "1" },
      },
    ],
    edges: [],
  };
  const report = await page.evaluate(
    async ([body, t]) => {
      const r = await fetch("/api/validate", {
        method: "POST",
        headers: { "content-type": "application/json", authorization: `Bearer ${t}` },
        body: JSON.stringify(body),
      });
      return (await r.json()) as { problems: { code: string; field?: string }[] };
    },
    [spec, token] as const,
  );
  expect(report.problems.filter((p) => p.code === "template_locked").map((p) => p.field)).toEqual(["retries"]);
  expect(errors).toEqual([]);
});
