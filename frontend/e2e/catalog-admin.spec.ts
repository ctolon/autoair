import { type Page, expect, test } from "@playwright/test";

/**
 * Operator packages that ship AutoAir metadata (`autoair.yaml`) look first-class: their own
 * palette group, icon and parameter widgets; the admin panel's Catalog tab explains what the
 * catalog build scanned, found, filtered and failed on. Light and dark.
 *
 * Needs a standalone AutoAir (`autoair serve --port 8930`) with a `bifrost` distribution
 * installed and selected (AUTOAIR_EXTRA_OPERATOR_DISTRIBUTIONS='["bifrost"]'),
 * AUTOAIR_IAM__STRATEGIES='["role_mapping"]' and AUTOAIR_AIRFLOW_URL pointing at an Airflow (or a
 * stub) that accepts the token on /api/v2/dags; its UI served by `vite preview --mode standalone`
 * with AUTOAIR_API_PORT=8930. The token is a JWT with the "AutoAir Admin" role (the admin panel
 * lives in the signed-in user's menu).
 *
 *   AUTOAIR_CATALOG_UI     UI (default http://127.0.0.1:8931)
 *   AUTOAIR_CATALOG_TOKEN  bearer token (default: an unsigned "AutoAir Admin" JWT)
 *   AUTOAIR_E2E_SHOTS      screenshot folder (default e2e/.results)
 *
 * Skipped when the server is not running.
 */
const UI = process.env.AUTOAIR_CATALOG_UI ?? "http://127.0.0.1:8931";
const SHOTS = process.env.AUTOAIR_E2E_SHOTS ?? "e2e/.results";

function adminToken(): string {
  const enc = (o: object) => Buffer.from(JSON.stringify(o)).toString("base64url");
  const claims = { sub: "root", role: "AutoAir Admin", exp: Math.floor(Date.now() / 1000) + 3600 };
  return process.env.AUTOAIR_CATALOG_TOKEN ?? `${enc({ alg: "HS512", typ: "JWT" })}.${enc(claims)}.signature`;
}
const TOKEN = adminToken();
const AUTH = { Authorization: `Bearer ${TOKEN}` };

async function open(page: Page, mode: "light" | "dark") {
  await page.addInitScript(
    ([t, m]) => {
      // vite preview serves the bundle without the server-injected config: proxied /api.
      (window as unknown as { __AUTOAIR_CONFIG__: object }).__AUTOAIR_CONFIG__ = { apiBase: "/api" };
      try {
        sessionStorage.setItem("autoair.airflowToken", t);
        localStorage.setItem("autoair.lang", "en");
        localStorage.setItem("autoair.colorMode", m);
      } catch {
        /* about:blank */
      }
    },
    [TOKEN, mode] as const,
  );
  await page.goto(`${UI}/`);
  await expect(page.getByPlaceholder(/Search operators/)).toBeVisible({ timeout: 60_000 });
}

async function settle(page: Page) {
  await page.waitForFunction(() => document.getAnimations().every((a) => a.playState !== "running"));
}

test("bifrost operators: palette group, widgets and catalog diagnostics", async ({ browser, request }) => {
  const api = await request.get(`${UI}/api/health`).catch(() => null);
  test.skip(!api?.ok(), "AutoAir UI not running");
  const ops = await (await request.get(`${UI}/api/catalog/operators`, { headers: AUTH })).json();
  test.skip(
    !Array.isArray(ops) || !ops.some((o: { group?: string }) => o.group === "Bifrost"),
    "bifrost is not installed",
  );

  for (const mode of ["light", "dark"] as const) {
    const context = await browser.newContext({ viewport: { width: 1600, height: 1000 } });
    const page = await context.newPage();
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(e.message));
    await open(page, mode);
    expect(await page.evaluate(() => document.documentElement.classList.contains("dark"))).toBe(mode === "dark");

    // Palette: a "Bifrost" group (catalog group order 10: before the provider groups).
    const search = page.getByPlaceholder(/Search operators/);
    await search.fill("bifrost");
    const listbox = page.locator('[role="listbox"]').first();
    await expect(listbox.getByText("Bifrost", { exact: true }).first()).toBeVisible();
    const ingest = page
      .locator('[role="option"]')
      .filter({ has: page.getByText("BifrostIngestOperator", { exact: true }) });
    await expect(ingest).toBeVisible();
    await expect(ingest.locator('img[src*="package%3Abifrost-logo"]')).toBeVisible();
    await settle(page);
    await page.screenshot({ path: `${SHOTS}/palette-bifrost-${mode}.png` });

    // Unfiltered Operators tab: the Bifrost group (order 10) sorts before provider groups (100).
    await search.fill("");
    await page.getByRole("tab", { name: "Operators", exact: true }).click();
    const headers = listbox.locator('[role="presentation"]');
    await expect(headers.first()).toContainText("Bifrost");
    await settle(page);
    await page.screenshot({ path: `${SHOTS}/palette-groups-${mode}.png` });
    await page.getByRole("tab", { name: "All", exact: true }).click();
    await search.fill("bifrost");

    // Inspector: sql editor, enum select, masked secret, examples and docs link.
    await ingest.dblclick();
    const node = page.locator(".react-flow__node").filter({ hasText: "BifrostIngestOperator" }).last();
    await node.dispatchEvent("click");
    await expect(page.getByText("Ingest a dataset into Bifrost.").first()).toBeVisible();
    await expect(page.getByTestId("operator-docs-url")).toHaveAttribute("href", /docs\.bifrost\.example/);
    await page.getByTestId("operator-examples").click();
    await page.getByRole("menuitem", { name: /Daily load/ }).click();
    await expect(page.locator(".cm-content").filter({ hasText: "select * from sales" }).first()).toBeVisible();
    // api_token (secret) has a default: under "N more parameter(s)" (mode is set by the example).
    await page.getByRole("button", { name: /\d+ more parameters?/ }).click();
    const secret = page.locator('input[type="password"]').first();
    await secret.scrollIntoViewIfNeeded();
    await expect(secret).toBeVisible();
    await expect(page.getByRole("combobox", { name: /^mode/ })).toContainText(/append|overwrite/);
    await settle(page);
    await page.screenshot({ path: `${SHOTS}/inspector-bifrost-${mode}.png` });

    // Admin panel → Catalog tab.
    const user = page.getByRole("button", { name: /Signed in as/ });
    await user.click();
    await page.getByTestId("open-admin-panel").click();
    await expect(page.getByTestId("admin-panel")).toBeVisible();
    await page.getByTestId("admin-tab-catalog").click();
    const sources = page.getByTestId("admin-catalog-sources");
    await expect(sources).toContainText("DistributionsSource");
    await expect(sources).toContainText("bifrost");
    await expect(page.getByTestId("admin-catalog-metadata")).toContainText("autoair.yaml");
    const built = await page.getByTestId("admin-catalog-built").innerText();
    await page.getByTestId("admin-catalog-refresh").click();
    await expect(page.getByText(/Catalog rebuilt: \d+ operators/).first()).toBeVisible({ timeout: 120_000 });
    await expect.poll(() => page.getByTestId("admin-catalog-built").innerText(), { timeout: 30_000 }).not.toBe(built);
    await expect(page.getByTestId("admin-panel").getByRole("alert")).toHaveCount(0);
    await settle(page);
    await page.screenshot({ path: `${SHOTS}/admin-catalog-${mode}.png` });
    expect(errors).toEqual([]);
    await context.close();
  }
});
