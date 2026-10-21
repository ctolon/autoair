import { type Page, expect, test } from "@playwright/test";

/**
 * Workbench layout: hidden side panels give the canvas all their space (no leftover border
 * or handle), rails/edge buttons/shortcuts toggle them, narrow builders get drawers, and the
 * header stays on one row at 1280px in English and Turkish.
 */
const DEV = process.env.AUTOAIR_DEV_URL ?? "http://localhost:5173";

async function start(page: Page, token: string, lang = "en") {
  await page.addInitScript(
    ([t, l]) => {
      try {
        sessionStorage.setItem("autoair.airflowToken", t);
        localStorage.setItem("autoair.lang", l);
        localStorage.removeItem("autoair.layout.collapsed");
        localStorage.removeItem("autoair.layout.sizes");
      } catch {
        /* about:blank */
      }
    },
    [token, lang] as const,
  );
  await page.goto(`${DEV}/`);
  await expect(page.locator("header").first()).toBeVisible({ timeout: 30_000 });
}

const canvasWidth = async (page: Page) => (await page.locator(".react-flow").boundingBox())?.width ?? 0;

test("collapsed panels give their space to the canvas", async ({ page, request }) => {
  const dev = await request.get(DEV).catch(() => null);
  test.skip(!dev?.ok(), "vite dev server not running");
  const token = (await (await request.get("/auth/token")).json()).access_token as string;
  await page.setViewportSize({ width: 1600, height: 1000 });
  await start(page, token);
  const before = await canvasWidth(page);

  await page.getByTestId("collapse-palette").click();
  await page.getByTestId("collapse-inspector").click();
  await expect(page.getByTestId("panel-palette")).toBeHidden();
  await expect(page.getByTestId("resize-palette")).toHaveCount(0);
  await expect(page.getByTestId("resize-inspector")).toHaveCount(0);
  // Only the two 28px rails remain next to the canvas.
  await expect.poll(() => canvasWidth(page)).toBeGreaterThan(1600 - 2 * 28 - 4);
  expect(await canvasWidth(page)).toBeGreaterThan(before + 500);

  // Bottom panel: edge button hides it, the bar brings it back.
  await page.getByTestId("collapse-bottom").click();
  await expect(page.getByTestId("panel-bottom")).toBeHidden();
  await page.getByTestId("expand-bottom").click();
  await expect(page.getByTestId("panel-bottom")).toBeVisible();

  // Rails and the keyboard bring the side panels back.
  await page.getByTestId("expand-palette").click();
  await expect(page.getByPlaceholder(/Search operators/)).toBeVisible();
  await page.locator(".react-flow__pane").click({ position: { x: 300, y: 200 } });
  await page.keyboard.press("ControlOrMeta+i");
  await expect(page.getByTestId("panel-inspector")).toBeVisible();
  await page.keyboard.press("ControlOrMeta+b");
  await expect(page.getByTestId("panel-palette")).toBeHidden();
  // Not while typing.
  await page.keyboard.press("ControlOrMeta+b");
  await page.getByPlaceholder(/Search operators/).click();
  await page.keyboard.press("ControlOrMeta+b");
  await expect(page.getByTestId("panel-palette")).toBeVisible();

  // Resizing with the keyboard.
  const handle = page.getByTestId("resize-palette");
  const w0 = Number(await handle.getAttribute("aria-valuenow"));
  await handle.focus();
  await page.keyboard.press("ArrowRight");
  await expect(handle).toHaveAttribute("aria-valuenow", String(w0 + 16));
});

test("narrow builders use drawers and never scroll sideways", async ({ page, request }) => {
  const dev = await request.get(DEV).catch(() => null);
  test.skip(!dev?.ok(), "vite dev server not running");
  const token = (await (await request.get("/auth/token")).json()).access_token as string;
  await page.setViewportSize({ width: 1000, height: 720 });
  await start(page, token);
  // The inspector starts as a rail; it opens over the canvas.
  await expect(page.getByTestId("panel-inspector")).toBeHidden();
  await page.getByTestId("expand-inspector").click();
  await expect(page.getByTestId("panel-inspector")).toHaveAttribute("data-overlay", "true");
  await page.getByTestId("collapse-inspector").click();
  await expect(page.getByTestId("panel-inspector")).toBeHidden();

  await page.setViewportSize({ width: 420, height: 800 });
  await expect(page.getByTestId("panel-palette")).toBeHidden();
  await page.getByTestId("expand-palette").click();
  await expect(page.getByPlaceholder(/Search operators/)).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(420);
});

for (const lang of ["en", "tr"]) {
  test(`header stays on one row at 1280px (${lang})`, async ({ page, request }) => {
    const dev = await request.get(DEV).catch(() => null);
    test.skip(!dev?.ok(), "vite dev server not running");
    const token = (await (await request.get("/auth/token")).json()).access_token as string;
    await page.setViewportSize({ width: 1280, height: 720 });
    await start(page, token, lang);
    const header = page.locator("header").first();
    await expect(page.getByTestId("more-menu")).toBeVisible();
    const box = await header.boundingBox();
    expect(box?.height ?? 0).toBeLessThan(56);
    // Everything fits: the last control is inside the header.
    const more = await page.getByTestId("more-menu").boundingBox();
    expect((more?.x ?? 9999) + (more?.width ?? 0)).toBeLessThanOrEqual(1280);
    // Long values are truncated with tooltips.
    await page.getByTestId("toolbar-dag-id").hover();
    await expect(page.getByRole("tooltip")).toBeVisible();
  });
}
