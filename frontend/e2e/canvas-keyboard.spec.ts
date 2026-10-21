import { type Page, expect, test } from "@playwright/test";

/**
 * Canvas keyboard operations: copy / cut / paste keep params, positions and the edges
 * between copied nodes (ids remapped); Delete and Backspace remove the selection through the
 * store (undoable); none of it fires while typing in an input or a code editor.
 */
const DEV = process.env.AUTOAIR_DEV_URL ?? "http://localhost:5173";

const nodes = (page: Page) => page.locator(".react-flow__node");
const edges = (page: Page) => page.locator(".react-flow__edge");
const node = (page: Page, name: string) => nodes(page).filter({ hasText: name }).first();

async function add(page: Page, name: string) {
  await page.getByPlaceholder(/Search operators/).fill(name);
  await page
    .locator('[role="option"]')
    .filter({ has: page.getByText(name, { exact: true }) })
    .first()
    .dblclick();
}

async function select(page: Page, name: string) {
  await node(page, name).click();
}

test("copy, cut, paste and delete on the canvas", async ({ page, request, context }) => {
  const dev = await request.get(DEV).catch(() => null);
  test.skip(!dev?.ok(), "vite dev server not running");
  await context.grantPermissions(["clipboard-read", "clipboard-write"], { origin: DEV });
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
  await page.goto(`${DEV}/`);
  await expect(page.getByPlaceholder(/Search operators/)).toBeVisible({ timeout: 30_000 });

  await add(page, "BashOperator");
  await add(page, "EmptyOperator");
  await expect(nodes(page)).toHaveCount(2);
  // bash_1 gets a parameter; connect bash_1 -> empty_1.
  await select(page, "bash_1");
  const cmd = page.locator('[data-autoair-field*="bash_command"] .cm-content').first();
  await cmd.click();
  await page.keyboard.type("echo copied");
  // Typing in the editor: Backspace / Ctrl+A / Delete edit the text, not the canvas.
  await page.keyboard.press("Backspace");
  await page.keyboard.press("ControlOrMeta+a");
  await page.keyboard.press("Delete");
  await page.keyboard.type("echo copied");
  await expect(nodes(page)).toHaveCount(2);
  await node(page, "bash_1")
    .locator(".react-flow__handle-right")
    .dragTo(node(page, "empty_1").locator(".react-flow__handle-left"));
  await expect(edges(page)).toHaveCount(1);

  // Same in the palette search box.
  await select(page, "empty_1");
  const search = page.getByPlaceholder(/Search operators/);
  await search.fill("abc");
  await search.press("Backspace");
  await search.press("Delete");
  await expect(nodes(page)).toHaveCount(2);

  // Select both, copy, paste: 4 nodes, the copied edge is duplicated between the copies.
  await page.locator(".react-flow__pane").click({ position: { x: 20, y: 20 } });
  await page.keyboard.press("ControlOrMeta+a");
  await page.keyboard.press("ControlOrMeta+c");
  await page.locator(".react-flow__pane").hover({ position: { x: 200, y: 300 } });
  await page.keyboard.press("ControlOrMeta+v");
  await expect(nodes(page)).toHaveCount(4);
  await expect(edges(page)).toHaveCount(2);
  const ids = await nodes(page).evaluateAll((els) => els.map((e) => e.getAttribute("data-id")));
  expect(new Set(ids).size).toBe(4);
  // The pasted Bash task kept its command.
  const pastedBash = nodes(page).filter({ hasText: "BashOperator" }).nth(1);
  await pastedBash.click();
  await expect(page.locator('[data-autoair-field*="bash_command"] .cm-content').first()).toContainText("echo copied");

  // Cut removes, paste brings it back.
  await page.keyboard.press("Escape");
  await pastedBash.click();
  await page.keyboard.press("ControlOrMeta+x");
  await expect(nodes(page)).toHaveCount(3);
  await page.locator(".react-flow__pane").hover({ position: { x: 300, y: 380 } });
  await page.keyboard.press("ControlOrMeta+v");
  await expect(nodes(page)).toHaveCount(4);

  // Delete and Backspace remove the selection; undo restores it. (Lay out first: pasted
  // nodes may overlap.)
  await page.locator(".react-flow__pane").click({ position: { x: 20, y: 20 } });
  await page.keyboard.press("Shift+L");
  await page.waitForTimeout(800);
  await page.keyboard.press("Shift+F");
  await page.waitForTimeout(500);
  // An edge: select it and delete it (only bash_1 -> empty_1 is left: the cut node lost its edge).
  await expect(edges(page)).toHaveCount(1);
  const a = await node(page, "bash_1").locator(".react-flow__handle-right").boundingBox();
  const b = await node(page, "empty_1").locator(".react-flow__handle-left").boundingBox();
  if (!a || !b) throw new Error("handles not visible");
  await page.mouse.click(a.x + (b.x - a.x) * 0.25, a.y + a.height / 2);
  await expect(edges(page).first()).toHaveClass(/selected/);
  await page.keyboard.press("Delete");
  await expect(edges(page)).toHaveCount(0);
  await page.keyboard.press("ControlOrMeta+z");
  await expect(edges(page)).toHaveCount(1);
  await node(page, "empty_1").click();
  await page.keyboard.press("Delete");
  await expect(nodes(page)).toHaveCount(3);
  await page.keyboard.press("ControlOrMeta+z");
  await expect(nodes(page)).toHaveCount(4);
  await node(page, "bash_1").click();
  await page.keyboard.press("Backspace");
  await expect(nodes(page)).toHaveCount(3);
  expect(errors).toEqual([]);
});
