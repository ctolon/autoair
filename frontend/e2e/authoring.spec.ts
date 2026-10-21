import { type Page, expect, test } from "@playwright/test";

/**
 * Authoring ergonomics on the standalone server: problems jump to their field (validation and
 * schema errors alike), collapsible panels, type badges, rendered operator/decorator docs,
 * Markdown preview and shell completion.
 */
const STANDALONE = process.env.AUTOAIR_STANDALONE_URL ?? "http://127.0.0.1:8765";

async function add(page: Page, query: string, name?: string) {
  await page.getByPlaceholder(/Search operators/).fill(query);
  const options = page.locator('[role="option"]');
  await (name ? options.filter({ has: page.getByText(name, { exact: true }) }).first() : options.first()).dblclick();
  await page.locator(".react-flow__node").last().dispatchEvent("click");
}

test("problems, panels, types, docs and editors", async ({ page, request }) => {
  test.setTimeout(180_000);
  const health = await request.get(`${STANDALONE}/api/health`).catch(() => null);
  test.skip(!health?.ok(), "standalone server not running");
  const token = (await (await request.get("/auth/token")).json()).access_token as string;
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.addInitScript((t) => {
    try {
      sessionStorage.setItem("autoair.airflowToken", t);
      localStorage.setItem("autoair.lang", "en");
      localStorage.removeItem("autoair.layout.collapsed");
    } catch {
      /* about:blank */
    }
  }, token);
  await page.goto(`${STANDALONE}/ui/`);
  await expect(page.getByPlaceholder(/Search operators/)).toBeVisible();

  // Type badges and required markers on operator parameters.
  await add(page, "HITLOperator", "HITLOperator");
  const subject = page.locator('[data-autoair-field="subject"]');
  await expect(subject.locator("[data-dtype]").first()).toHaveText("str");
  await expect(page.locator('[data-autoair-field="options"] [data-dtype]').first()).toHaveText("list[str]");

  // A validation problem in the bottom panel jumps to its field, from another node.
  await add(page, "BashOperator", "BashOperator");
  await page.getByRole("tab", { name: /Problems/ }).click();
  await page
    .getByRole("button", { name: /subject:/ })
    .first()
    .click();
  await expect(subject.locator(".cm-content").first()).toBeFocused();

  // A schema (422) error inside a nested editor: invalid retry rule exception name.
  await page.getByRole("tab", { name: "Behaviour" }).click();
  await page.getByRole("button", { name: "Add rule" }).click();
  const exception = page.locator('[data-autoair-field="retry_policy.rules.0.exception"] input');
  await exception.fill("not a dotted name");
  await page.getByRole("tab", { name: "Parameters" }).click();
  await page.getByRole("tab", { name: /Problems/ }).click();
  await page
    .getByRole("button", { name: /retry_policy\.rules\.0\.exception:/, description: "Show where this problem is" })
    .click();
  await expect(exception).toBeFocused();
  await exception.fill("requests.exceptions.Timeout");

  // Collapsible panels (toolbar buttons + shortcuts) restore their previous size.
  const palette = page.locator('[data-part="panel"][data-id="palette"], [id*="palette"][data-part="panel"]').first();
  const width = (await palette.boundingBox())?.width ?? 0;
  await page.getByTestId("toggle-palette").click();
  await expect(page.getByPlaceholder(/Search operators/)).toBeHidden();
  await page.keyboard.press("ControlOrMeta+b");
  await expect(page.getByPlaceholder(/Search operators/)).toBeVisible();
  expect(Math.abs(((await palette.boundingBox())?.width ?? 0) - width)).toBeLessThan(4);
  await page.getByTestId("toggle-inspector").click();
  await expect(page.locator('[data-autoair-field="subject"]')).toBeHidden();
  await page.getByTestId("toggle-inspector").click();

  // Operator docs: reST rendered (See also callout, parameter list), raw source on demand.
  await add(page, "PythonOperator", "PythonOperator");
  await page.getByRole("tab", { name: "Docs" }).click();
  const docs = page.getByTestId("markdown-preview").last();
  await expect(docs).toContainText("See also");
  await expect(docs.locator("code", { hasText: "python_callable" }).first()).toBeVisible();
  await expect(docs).not.toContainText(":param");
  // Task documentation is Markdown with a preview.
  await page.locator('[data-autoair-field="doc_md"] .cm-content').click();
  await page.keyboard.type("# Runbook\n\n- restart the job\n");
  await page.getByRole("button", { name: "Preview" }).first().click();
  await expect(page.locator('[data-autoair-field="doc_md"]').getByRole("heading", { name: "Runbook" })).toBeVisible();

  // Decorator docs for @task.* nodes.
  await add(page, "@task.bash");
  await page.getByRole("tab", { name: "Docs" }).click();
  await expect(page.getByTestId("markdown-preview").last()).toContainText("BashOperator");

  // Shell completion: Airflow's task environment variables.
  await add(page, "BashOperator", "BashOperator");
  await page.locator('[data-autoair-field*="bash_command"] .cm-content').first().click();
  await page.keyboard.type("echo $AIRFLOW_CTX_D");
  await expect(page.locator(".cm-tooltip-autocomplete")).toContainText("AIRFLOW_CTX_DAG_ID");

  expect(errors).toEqual([]);
});
