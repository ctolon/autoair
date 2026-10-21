import { type Page, expect, test } from "@playwright/test";

/**
 * Auto layout runs ELK in a module Web Worker, and on the main thread (importing the same ELK
 * file) when workers are unavailable. Runs against a production build (`vite preview`, e.g. the
 * private stack of the run-e2e skill).
 */
const DEV = process.env.AUTOAIR_DEV_URL ?? "http://localhost:5173";

type Box = { x: number; y: number; width: number; height: number };
const overlaps = (a: Box, b: Box) =>
  a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height;

async function open(page: Page, errors: string[]) {
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("console", (m) => {
    // The private stack has no Airflow: its REST calls answer 502, which is not our concern.
    if (m.type() === "error" && !m.text().startsWith("Failed to load resource")) errors.push(m.text());
  });
  await page.addInitScript(() => {
    // `vite preview` proxies `/api` (like the dev server); a production build would otherwise
    // look for the plugin's `autoair/api`.
    window.__AUTOAIR_CONFIG__ = { apiBase: "/api" };
    try {
      localStorage.setItem("autoair.lang", "en");
    } catch {
      /* about:blank */
    }
  });
  await page.goto(`${DEV}/`);
  await expect(page.locator(".react-flow")).toBeVisible({ timeout: 30_000 });
}

async function addStackedNodes(page: Page): Promise<void> {
  const search = page.getByPlaceholder(/Search operators/);
  await search.fill("EmptyOperator");
  const row = page.locator('[role="option"]').filter({ has: page.getByText("EmptyOperator", { exact: true }) });
  for (let i = 0; i < 3; i++) await row.first().dblclick();
  await expect(page.locator(".react-flow__node")).toHaveCount(3);
}

async function autoLayoutAndCheck(page: Page): Promise<void> {
  const nodes = page.locator(".react-flow__node");
  const before = await Promise.all((await nodes.all()).map((n) => n.boundingBox()));
  await page.locator(".react-flow__pane").click({ button: "right", position: { x: 40, y: 40 } });
  await page.getByRole("menuitem", { name: "Auto layout" }).click();
  await expect
    .poll(async () => JSON.stringify(await Promise.all((await nodes.all()).map((n) => n.boundingBox()))))
    .not.toBe(JSON.stringify(before));
  const after = (await Promise.all((await nodes.all()).map((n) => n.boundingBox()))) as Box[];
  for (let i = 0; i < after.length; i++)
    for (let j = i + 1; j < after.length; j++) expect(overlaps(after[i], after[j])).toBe(false);
}

test.beforeEach(async ({ request }) => {
  const ui = await request.get(DEV).catch(() => null);
  test.skip(!ui?.ok(), "no UI server at AUTOAIR_DEV_URL");
});

test("auto layout runs ELK in a module worker", async ({ page }) => {
  const errors: string[] = [];
  const workers: string[] = [];
  page.on("worker", (w) => workers.push(w.url()));
  await open(page, errors);
  await addStackedNodes(page);
  await autoLayoutAndCheck(page);
  expect(workers.some((u) => u.includes("layout.worker"))).toBe(true);
  expect(errors).toEqual([]);
});

test("auto layout falls back to the main thread without workers", async ({ page }) => {
  const errors: string[] = [];
  const workers: string[] = [];
  page.on("worker", (w) => workers.push(w.url()));
  await page.addInitScript(() => {
    Object.defineProperty(window, "Worker", { value: undefined, configurable: true });
  });
  await open(page, errors);
  await addStackedNodes(page);
  await autoLayoutAndCheck(page);
  expect(workers).toEqual([]);
  expect(errors).toEqual([]);
});
