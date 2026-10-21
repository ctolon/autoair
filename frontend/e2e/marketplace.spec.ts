import { expect, test } from "@playwright/test";

/**
 * Marketplace (standalone server started with AUTOAIR_MARKETPLACE_SOURCES pointing at a signed
 * index, e.g. examples/marketplace built with `autoair marketplace build`): browse, install a
 * fragment, find it in the gallery. Skipped when no source is configured.
 */
const STANDALONE = process.env.AUTOAIR_STANDALONE_URL ?? "http://127.0.0.1:8765";

test("install a signed marketplace template into the gallery", async ({ page, request }) => {
  const health = await request.get(`${STANDALONE}/api/health`).catch(() => null);
  test.skip(!health?.ok(), "standalone server not running");
  const token = (await (await request.get("/auth/token")).json()).access_token as string;
  const headers = { Authorization: `Bearer ${token}` };
  const listing = await (await request.get(`${STANDALONE}/api/marketplace`, { headers })).json();
  test.skip(!listing.sources?.length, "no marketplace source configured");
  await request.delete(`${STANDALONE}/api/templates/slack-on-failure`, { headers });

  page.on("pageerror", (e) => console.error("pageerror:", e.message));
  await page.goto(`${STANDALONE}/ui/`);
  await page.evaluate((t) => {
    sessionStorage.setItem("autoair.airflowToken", t);
    localStorage.setItem("autoair.lang", "en");
  }, token);
  await page.reload();

  await page.getByRole("button", { name: "File", exact: true }).click();
  await page.getByRole("menuitem", { name: "New from template…" }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByText("Marketplace", { exact: true }).click();
  await expect(dialog.getByText(/signature verified/)).toBeVisible();
  await expect(dialog.getByTestId("marketplace-entry")).toHaveCount(3);
  await expect(dialog.getByText("pip install autoair-dbt-nodes")).toBeVisible();
  await page.screenshot({ path: "e2e/.results/marketplace.png" });

  const entry = dialog.getByTestId("marketplace-entry").filter({ hasText: "Slack alert on failure" });
  await entry.getByRole("button", { name: "Install" }).click();
  // Back in the gallery with the installed fragment selected.
  await expect(dialog.getByRole("button", { name: /Slack alert on failure/ })).toBeVisible();
  await expect(dialog.getByText("marketplace:examples").first()).toBeVisible();
  const tpl = await (await request.get(`${STANDALONE}/api/templates/slack-on-failure`, { headers })).json();
  expect(tpl.kind).toBe("fragment");
});
