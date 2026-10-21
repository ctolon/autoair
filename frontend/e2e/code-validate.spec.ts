import { type Page, expect, test } from "@playwright/test";

/**
 * Code fields: the "Validate" button reports server-side diagnostics (ShellCheck / bash -n
 * here) in the editor, and documentation fields get the Markdown editor (formatting toolbar,
 * live side-by-side preview, full screen). Set SHOTS=<dir> to also save screenshots.
 */
const DEV = process.env.AUTOAIR_DEV_URL ?? "http://localhost:5173";
const SHOTS = process.env.SHOTS;

async function open(page: Page, token: string, theme: "light" | "dark") {
  await page.addInitScript(
    ([t, mode]) => {
      try {
        sessionStorage.setItem("autoair.airflowToken", t);
        localStorage.setItem("autoair.lang", "en");
        localStorage.setItem("autoair.colorMode", mode);
      } catch {
        /* about:blank */
      }
    },
    [token, theme] as const,
  );
  await page.goto(`${DEV}/`);
  await expect(page.getByPlaceholder(/Search operators/)).toBeVisible({ timeout: 30_000 });
}

async function addBash(page: Page) {
  await page.getByPlaceholder(/Search operators/).fill("BashOperator");
  await page
    .locator('[role="option"]')
    .filter({ has: page.getByText("BashOperator", { exact: true }) })
    .first()
    .dblclick();
  await expect(page.locator(".react-flow__node")).toHaveCount(1);
}

const VIEWPORT = process.env.VIEWPORT; // e.g. 1280x720
const shot = async (page: Page, name: string) => {
  if (!SHOTS) return;
  await page.waitForTimeout(400); // dialog animations
  await page.screenshot({ path: `${SHOTS}/${name}${VIEWPORT ? `-${VIEWPORT}` : ""}.png` });
};

for (const theme of ["light", "dark"] as const) {
  test(`validate a bash command and edit docs as markdown (${theme})`, async ({ page, request }) => {
    const dev = await request.get(DEV).catch(() => null);
    test.skip(!dev?.ok(), "vite dev server not running");
    const token = (await (await request.get("/auth/token")).json()).access_token as string;
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(e.message));
    if (VIEWPORT) {
      const [width, height] = VIEWPORT.split("x").map(Number);
      await page.setViewportSize({ width: width ?? 1600, height: height ?? 1000 });
    }
    await open(page, token, theme);
    await addBash(page);

    // --- Validate: bash -n finds the unterminated `if`.
    const field = page.locator('[data-autoair-field*="bash_command"] [data-code-editor]').first();
    await field.locator(".cm-content").click();
    await page.keyboard.type("if true; then echo $1", { delay: 40 });
    await expect(field.locator(".cm-content")).toHaveText("if true; then echo $1");
    await page.keyboard.press("Escape"); // completion popup
    await field.getByRole("button", { name: /^Validate/ }).click();
    const status = field.locator("[data-validation]");
    await expect(status).toHaveAttribute("data-validation", "errors");
    await expect(status).toContainText(/Checked with bash -n/);
    await expect(field.locator(".cm-lintRange-error").first()).toBeVisible();
    await shot(page, `validate-inline-${theme}`);
    // Editing makes the result stale: it disappears until validated again.
    await field.locator(".cm-content").click();
    await page.keyboard.press("End");
    await page.keyboard.type("; fi", { delay: 40 });
    await page.keyboard.press("Escape");
    await expect(status).toHaveCount(0);
    await field.getByRole("button", { name: /^Validate/ }).click();
    await expect(field.locator("[data-validation]")).not.toHaveAttribute("data-validation", "errors");

    // The large editor has a labelled Validate button too.
    await field.getByRole("button", { name: /Open .* in a large editor/ }).click();
    const dialog = page.getByRole("dialog");
    await dialog.getByRole("button", { name: "Validate", exact: true }).click();
    await expect(dialog.locator("[data-validation]")).toBeVisible();
    await shot(page, `validate-dialog-${theme}`);
    await page.keyboard.press("Escape");

    // --- Markdown editor on the task's Docs tab.
    await page.getByRole("tab", { name: "Docs" }).click();
    const md = page.locator("[data-markdown-editor]").first();
    const mdText = md.getByRole("textbox").first();
    await mdText.click();
    await page.keyboard.type("Loads data", { delay: 30 });
    await page.keyboard.press("ControlOrMeta+a");
    await md.getByRole("button", { name: /^Bold/ }).click();
    await page.keyboard.press("End");
    await page.keyboard.press("Enter");
    await md.getByRole("button", { name: "Table" }).click();
    await md.getByRole("button", { name: "Split" }).click();
    const preview = md.locator("[data-markdown-preview]");
    await expect(preview.locator("strong")).toHaveText("Loads data");
    await expect(preview.locator("table")).toBeVisible();
    await shot(page, `markdown-split-${theme}`);

    await md.getByRole("button", { name: "Full screen" }).click();
    const full = page.getByRole("dialog");
    await expect(full.getByRole("heading", { name: /Documentation.*bash/i })).toBeVisible();
    await expect(full.locator("[data-markdown-preview] table")).toBeVisible();
    await shot(page, `markdown-fullscreen-${theme}`);
    await page.keyboard.press("Escape");

    // The DAG's doc_md (DAG settings → Docs) uses the same editor.
    await page.locator(".react-flow__pane").click({ position: { x: 20, y: 20 } });
    await page.getByRole("tab", { name: "Docs" }).click();
    const dagDocs = page.locator("[data-markdown-editor]").first();
    await expect(dagDocs.getByRole("toolbar", { name: "Formatting" })).toBeVisible();
    await dagDocs.getByRole("textbox").first().click();
    await dagDocs.getByRole("button", { name: "Heading" }).click();
    await expect(dagDocs.locator(".cm-content")).toHaveText("## Heading");
    await shot(page, `markdown-dag-${theme}`);
    expect(errors).toEqual([]);
  });
}
