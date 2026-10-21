import { type Locator, type Page, expect, test } from "@playwright/test";

/**
 * Typing half-written code (unfinished attribute access, open brackets, Jinja) into every
 * kind of code field must never break the inspector: lint/LSP results arrive while typing.
 * Runs against the Vite dev server so React's development checks are errors too.
 */
const DEV = process.env.AUTOAIR_DEV_URL ?? "http://localhost:5173";
const SNIPPETS = [
  "import os\nos.en",
  "import os\nos.environ[",
  "def f(:\n  x = {",
  "from airflow.sdk import \n",
  "x = 'abc\n",
  "os.path.jo",
  'print("{{ ds',
];

async function open(page: Page, token: string, errors: string[]) {
  page.on("pageerror", (e) => errors.push(`pageerror: ${e.message}`));
  page.on("console", (m) => {
    if (m.type() === "error") errors.push(`console: ${m.text().slice(0, 500)}`);
  });
  await page.addInitScript((t) => {
    try {
      sessionStorage.setItem("autoair.airflowToken", t);
      localStorage.setItem("autoair.lang", "en");
    } catch {
      /* about:blank */
    }
  }, token);
  await page.goto(`${DEV}/`);
  await expect(page.getByPlaceholder(/Search operators/)).toBeVisible({ timeout: 30_000 });
}

async function add(page: Page, query: string, name?: string) {
  await page.getByPlaceholder(/Search operators/).fill(query);
  const options = page.locator('[role="option"]');
  const option = name ? options.filter({ has: page.getByText(name, { exact: true }) }).first() : options.first();
  await option.dblclick();
  await page.locator(".react-flow__node").last().dispatchEvent("click");
}

async function typeAll(page: Page, editor: Locator) {
  for (const snippet of SNIPPETS) {
    await editor.click();
    await page.keyboard.press("ControlOrMeta+a");
    await page.keyboard.press("Delete");
    for (const ch of snippet) {
      await page.keyboard.type(ch);
      await page.waitForTimeout(30);
    }
    // Lint, completion and hover results come back while the code is still broken.
    await page.waitForTimeout(1200);
    await page.keyboard.press("Escape");
  }
  await expect(page.getByTestId("error-boundary")).toHaveCount(0);
}

test.describe("typing unfinished code", () => {
  test.beforeEach(async ({ request }) => {
    const dev = await request.get(DEV).catch(() => null);
    test.skip(!dev?.ok(), "vite dev server not running");
  });

  test("@task body, function parameter and Jinja field", async ({ page, request }) => {
    test.setTimeout(240_000);
    const token = (await (await request.get("/auth/token")).json()).access_token as string;
    const errors: string[] = [];
    await open(page, token, errors);

    await add(page, "@task");
    await typeAll(page, page.locator('[data-autoair-field="code"] .cm-content').first());
    // Escape closes completion inside the editor; the node stays selected.
    await expect(page.locator('[data-autoair-field="code"]')).toBeVisible();

    await add(page, "PythonOperator", "PythonOperator");
    await page.getByRole("button", { name: /^Value source of python_callable/ }).click();
    await page.getByRole("menuitem", { name: "Python function (write it here)" }).click();
    await typeAll(page, page.locator('[data-autoair-field*="python_callable"] .cm-content').first());

    await add(page, "BashOperator", "BashOperator");
    await typeAll(page, page.locator('[data-autoair-field*="bash_command"] .cm-content').first());

    expect(errors).toEqual([]);
  });
});
