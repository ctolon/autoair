import { expect, test } from "@playwright/test";

/**
 * Integration logos on palette rows and canvas nodes; a custom icon uploaded through the
 * browser resizer becomes the default of an operator class (standalone server).
 */
const STANDALONE = process.env.AUTOAIR_STANDALONE_URL ?? "http://127.0.0.1:8765";
const SVG =
  '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><path d="M50 92 L8 40 L20 8 L32 40 H68 L80 8 L92 40 Z" fill="#fc6d26"/></svg>';

test("logos and custom icons", async ({ page, request }) => {
  const health = await request.get(`${STANDALONE}/api/health`).catch(() => null);
  test.skip(!health?.ok(), "standalone server not running");
  const token = (await (await request.get("/auth/token")).json()).access_token as string;
  const headers = { Authorization: `Bearer ${token}` };
  const name = `e2e-icon-${Date.now().toString(36)}`;
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
  await page.goto(`${STANDALONE}/ui/`);
  const search = page.getByPlaceholder(/Search operators/);
  await search.fill("BashOperator");
  const bash = page.locator('[role="option"]').filter({ has: page.getByText("BashOperator", { exact: true }) });
  await expect(bash).toBeVisible();
  await search.fill("PythonOperator");
  const row = page.locator('[role="option"]').filter({ has: page.getByText("PythonOperator", { exact: true }) });
  await expect(row.locator('img[src*="builtin%3Apython"]')).toBeVisible();
  await row.dblclick();
  const node = page.locator(".react-flow__node").filter({ hasText: "PythonOperator" }).last();
  await expect(node.locator('img[src*="builtin%3Apython"]')).toBeVisible();

  // Upload a custom icon (SVG → 64 px PNG in the browser) for this node only.
  await node.dispatchEvent("click");
  await page.getByRole("button", { name: "Change…" }).click();
  await page.getByRole("button", { name: "Upload…" }).click();
  const dialog = page.getByRole("dialog").last();
  await dialog.locator('input[type="file"]').setInputFiles({
    name: "logo.svg",
    mimeType: "image/svg+xml",
    buffer: Buffer.from(SVG),
  });
  await expect(dialog.getByTestId("icon-previews").locator("img")).toHaveCount(8);
  await dialog.getByRole("textbox", { name: "Name" }).fill(name);
  await dialog.getByRole("button", { name: "Upload", exact: true }).click();
  await expect(node.locator(`img[src*="custom%3A${name}"]`)).toBeVisible();
  const icon = await request.get(`${STANDALONE}/api/icons/custom:${name}`);
  expect(icon.headers()["content-type"]).toBe("image/png");
  expect(errors).toEqual([]);
  await request.delete(`${STANDALONE}/api/icons/custom:${name}`, { headers });
});
