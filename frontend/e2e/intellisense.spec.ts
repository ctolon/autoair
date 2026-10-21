import { type APIRequestContext, type Page, expect, test } from "@playwright/test";

/**
 * Python IntelliSense in a @task body: the built-in (jedi) services rank keywords and prefix
 * matches first and know the Airflow context; with `features.ide_language_server` and a
 * language server installed, hover, diagnostics and go-to-definition (peek) come from it.
 * Runs against a Vite dev server (AUTOAIR_DEV_URL) proxying to an AutoAir backend.
 */
const DEV = process.env.AUTOAIR_DEV_URL ?? "http://localhost:5173";

async function token(request: APIRequestContext): Promise<string> {
  return (await (await request.get("/auth/token")).json()).access_token as string;
}

async function open(page: Page, t: string, errors: string[]) {
  page.on("pageerror", (e) => errors.push(`pageerror: ${e.message}`));
  await page.addInitScript((value) => {
    try {
      sessionStorage.setItem("autoair.airflowToken", value);
      localStorage.setItem("autoair.lang", "en");
    } catch {
      /* about:blank */
    }
  }, t);
  await page.goto(`${DEV}/`);
  await expect(page.getByPlaceholder(/Search operators/)).toBeVisible({ timeout: 30_000 });
  await page.getByPlaceholder(/Search operators/).fill("@task");
  await page.locator('[role="option"]').first().dblclick();
  await page.locator(".react-flow__node").last().dispatchEvent("click");
}

async function typeBody(page: Page, text: string) {
  const editor = page.locator('[data-autoair-field="code"] .cm-content').first();
  await editor.click();
  await page.keyboard.press("ControlOrMeta+a");
  await page.keyboard.press("Delete");
  // Line by line: Enter must not accept a completion that is open at the end of a line.
  const lines = text.split("\n");
  for (const [i, line] of lines.entries()) {
    if (i > 0) {
      await page.keyboard.press("Escape");
      await page.keyboard.press("Enter");
      await page.keyboard.press("Home");
    }
    await page.keyboard.type(line, { delay: 25 });
  }
}

const completions = (page: Page) => page.locator(".cm-tooltip-autocomplete li");

/** The completion list offers `label` (asking explicitly once when a cold server missed the typing). */
async function expectCompletion(page: Page, label: string) {
  const item = completions(page).filter({ hasText: label }).first();
  try {
    await expect(item).toBeVisible({ timeout: 6_000 });
  } catch {
    await page.keyboard.press("Control+Space");
    await expect(item).toBeVisible({ timeout: 20_000 });
  }
}

async function ideEnabled(request: APIRequestContext, t: string): Promise<boolean> {
  const headers = { Authorization: `Bearer ${t}` };
  const meta = await request.get(`${DEV}/api/meta`, { headers });
  if (!meta.ok() || !(await meta.json()).features?.ide_language_server) return false;
  const servers = await request.get(`${DEV}/api/lsp/servers`, { headers });
  return servers.ok() && !!(await servers.json()).python?.available;
}

test.describe("python intellisense", () => {
  test.beforeEach(async ({ request }) => {
    const dev = await request.get(DEV).catch(() => null);
    test.skip(!dev?.ok(), "vite dev server not running");
  });

  test("keywords, stdlib and the Airflow context complete", async ({ page, request }) => {
    const errors: string[] = [];
    await open(page, await token(request), errors);
    await typeBody(page, "ret");
    await expectCompletion(page, "return");
    await expect(completions(page).first()).toContainText("return");
    await page.keyboard.press("Escape");

    await typeBody(page, "import js");
    await expectCompletion(page, "json");
    await page.keyboard.press("Escape");

    await typeBody(page, "import json\njson.du");
    await expectCompletion(page, "dumps");
    await page.keyboard.press("Escape");

    await typeBody(page, "ti.xcom_p");
    await expectCompletion(page, "xcom_pull");
    await page.keyboard.press("Escape");
    expect(errors).toEqual([]);
  });

  test("language server: hover, diagnostics and definition peek", async ({ page, request }) => {
    const t = await token(request);
    test.skip(!(await ideEnabled(request, t)), "ide_language_server off or no Python language server");
    const errors: string[] = [];
    await open(page, t, errors);
    await typeBody(page, "import json\nprint(undefined_name)\nvalue = json.dumps");
    // Diagnostics from the server (undefined name), mapped into the body.
    await expect(page.locator(".cm-lintRange-error, .cm-lintRange-warning").first()).toBeVisible({
      timeout: 30_000,
    });
    // Hover the middle of "dumps" (the editor splits a line into highlighted spans).
    const point = await page.evaluate(() => {
      const line = [...document.querySelectorAll('[data-autoair-field="code"] .cm-line')].find((l) =>
        l.textContent?.includes("value = json.dumps"),
      );
      const walker = line ? document.createTreeWalker(line, NodeFilter.SHOW_TEXT) : null;
      for (let node = walker?.nextNode(); node; node = walker?.nextNode()) {
        const at = node.textContent?.indexOf("dumps") ?? -1;
        if (at < 0) continue;
        const range = document.createRange();
        range.setStart(node, at + 1);
        range.setEnd(node, at + 3);
        const rect = range.getBoundingClientRect();
        return { x: rect.x + rect.width / 2, y: rect.y + rect.height / 2 };
      }
      return null;
    });
    if (!point) throw new Error("dumps not rendered");
    await page.mouse.move(point.x, point.y);
    await expect(page.locator(".cm-tooltip-hover").first()).toContainText("dumps", { timeout: 20_000 });
    await page.screenshot({ path: test.info().outputPath("hover.png") });
    // The cursor sits after "json.dumps": go inside the name and jump to its definition.
    await page.keyboard.press("ArrowLeft");
    await page.keyboard.press("ArrowLeft");
    await page.keyboard.press("F12");
    const peek = page.locator("#autoair-lsp-peek");
    await expect(peek).toBeVisible({ timeout: 20_000 });
    await expect(peek).toContainText("def dumps");
    await page.screenshot({ path: test.info().outputPath("peek.png") });
    await page.keyboard.press("Escape");
    await expect(peek).toHaveCount(0);
    expect(errors).toEqual([]);
  });
});
