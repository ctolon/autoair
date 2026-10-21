import { type Page, expect, test } from "@playwright/test";

/**
 * Undo history: every insert is its own step, a drag is one step, and undo walks all the way
 * back (a debounced history used to drop the earlier changes of a quick burst).
 */
const DEV = process.env.AUTOAIR_DEV_URL ?? "http://localhost:5173";

async function add(page: Page, query: string, name?: string) {
  await page.getByPlaceholder(/Search operators/).fill(query);
  const options = page.locator('[role="option"]');
  await (name ? options.filter({ has: page.getByText(name, { exact: true }) }).first() : options.first()).dblclick();
}

const nodes = (page: Page) => page.locator(".react-flow__node");

test("undo walks back every step", async ({ page, request }) => {
  const dev = await request.get(DEV).catch(() => null);
  test.skip(!dev?.ok(), "vite dev server not running");
  const token = (await (await request.get("/auth/token")).json()).access_token as string;
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
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

  await add(page, "BashOperator", "BashOperator");
  await add(page, "PythonOperator", "PythonOperator");
  await add(page, "EmptyOperator", "EmptyOperator");
  await expect(nodes(page)).toHaveCount(3);

  // A drag in several steps is a single history entry.
  const first = nodes(page).first();
  const box = await first.boundingBox();
  if (!box) throw new Error("node not visible");
  const start = { x: box.x + 40, y: box.y + 12 };
  await page.mouse.move(start.x, start.y);
  await page.mouse.down();
  for (let i = 1; i <= 5; i++) await page.mouse.move(start.x + 40 * i, start.y + 20 * i, { steps: 2 });
  await page.mouse.up();
  await page.waitForTimeout(400);
  const moved = await first.boundingBox();
  expect((moved?.x ?? 0) - box.x).toBeGreaterThan(100);

  const undo = async () => {
    await page.locator(".react-flow__pane").click({ position: { x: 5, y: 5 } });
    await page.keyboard.press("ControlOrMeta+z");
  };
  await undo();
  await expect.poll(async () => (await first.boundingBox())?.x).toBeCloseTo(box.x, 0);
  await expect(nodes(page)).toHaveCount(3);
  for (const count of [2, 1, 0]) {
    await undo();
    await expect(nodes(page)).toHaveCount(count);
  }
  // …and redo comes back.
  await page.keyboard.press("ControlOrMeta+Shift+z");
  await expect(nodes(page)).toHaveCount(1);
  expect(errors).toEqual([]);
});
