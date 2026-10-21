import { type Page, expect, test } from "@playwright/test";

/**
 * Admin panel → Features: runtime feature flags against a throw-away standalone AutoAir.
 *
 *   AUTOAIR_FEATURES_UI   UI (default http://127.0.0.1:8935: `vite preview --mode standalone`
 *                          with AUTOAIR_API_PORT=8936)
 *
 * The backend runs `autoair serve --port 8936` with AUTOAIR_IAM__STRATEGIES='["role_mapping"]',
 * AUTOAIR_FEATURES__MARKETPLACE=false (a flag locked by the environment) and AUTOAIR_AIRFLOW_URL
 * pointing at an Airflow (or a stub) that accepts the token on /api/v2/dags. The token is a JWT
 * with the "AutoAir Admin" role (AUTOAIR_FEATURES_TOKEN overrides it). Skipped when the server
 * is not running.
 */
const UI = process.env.AUTOAIR_FEATURES_UI ?? "http://127.0.0.1:8935";
const SHOTS = process.env.AUTOAIR_SCREENSHOT_DIR ?? "e2e/.results";

function adminToken(): string {
  const enc = (o: object) => Buffer.from(JSON.stringify(o)).toString("base64url");
  const claims = { sub: "root", role: "AutoAir Admin", exp: Math.floor(Date.now() / 1000) + 3600 };
  return process.env.AUTOAIR_FEATURES_TOKEN ?? `${enc({ alg: "HS512", typ: "JWT" })}.${enc(claims)}.signature`;
}
const TOKEN = adminToken();
const AUTH = { Authorization: `Bearer ${TOKEN}` };

async function openFeatures(page: Page, mode: "light" | "dark", lang = "en") {
  await page.addInitScript(
    ([t, m, l]) => {
      // `vite preview` serves the bare bundle: point it at the proxied API.
      (window as unknown as { __AUTOAIR_CONFIG__: object }).__AUTOAIR_CONFIG__ = { apiBase: "/api" };
      try {
        sessionStorage.setItem("autoair.airflowToken", t);
        localStorage.setItem("autoair.lang", l);
        localStorage.setItem("autoair.colorMode", m);
      } catch {
        /* about:blank */
      }
    },
    [TOKEN, mode, lang] as const,
  );
  await page.goto(`${UI}/`);
  const user = page.getByRole("button", { name: /Signed in as|giriş yapıldı/ });
  await expect(user).toBeVisible({ timeout: 30_000 });
  await user.click();
  // Regression: the role badge's permission tooltip used to cover and swallow this click.
  await page.getByTestId("user-roles").locator("[tabindex]").first().hover();
  await expect(page.getByRole("tooltip")).toBeVisible();
  await page.getByTestId("open-admin-panel").click();
  await expect(page.getByTestId("admin-panel")).toBeVisible();
  await page.getByTestId("admin-tab-features").click();
  await expect(page.getByTestId("admin-features-table").locator("tbody tr").first()).toBeVisible();
  await expect(page.getByTestId("admin-panel")).toHaveCSS("opacity", "1");
  await page.waitForFunction(() => document.getAnimations().every((a) => a.playState !== "running"));
}

async function metaFeature(page: Page, name: string): Promise<boolean> {
  const res = await page.request.get(`${UI}/api/meta`, { headers: AUTH });
  return ((await res.json()) as { features: Record<string, boolean> }).features[name];
}

test("features tab switches flags at runtime, light and dark", async ({ browser, request }) => {
  const ui = await request.get(UI).catch(() => null);
  const api = await request.get(`${UI}/api/health`).catch(() => null);
  test.skip(!ui?.ok() || !api?.ok(), "AutoAir UI not running");

  for (const mode of ["light", "dark"] as const) {
    const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    const page = await context.newPage();
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(e.message));
    await openFeatures(page, mode);
    expect(await page.evaluate(() => document.documentElement.classList.contains("dark"))).toBe(mode === "dark");

    // Locked through the environment: badge shown, switch disabled.
    const market = page.getByTestId("admin-feature-marketplace");
    await expect(market.getByTestId("feature-locked")).toBeVisible();
    await expect(market.getByRole("checkbox")).toBeDisabled();

    // Switches stay disabled until a reason is given.
    const templates = page.getByTestId("admin-feature-templates");
    await expect(templates.getByRole("checkbox")).toBeDisabled();
    await page.getByTestId("admin-features-reason").fill(`e2e ${mode} check`);
    await expect(templates.getByRole("checkbox")).toBeEnabled();
    await page.screenshot({ path: `${SHOTS}/admin-features-${mode}.png` });

    // Off at runtime: the table, /api/meta and the UI follow at once.
    await templates.getByTestId("feature-switch-templates").click();
    await expect(templates.getByTestId("feature-effective")).toHaveText("off");
    await expect(templates.getByTestId("feature-override")).toHaveText("off");
    expect(await metaFeature(page, "templates")).toBe(false);
    await page.screenshot({ path: `${SHOTS}/admin-features-override-${mode}.png` });

    // Reset: the configured value applies again.
    await templates.getByRole("button", { name: /Reset/ }).click();
    await expect(templates.getByTestId("feature-effective")).toHaveText("on");
    await expect(templates.getByTestId("feature-override")).toHaveText("—");
    expect(await metaFeature(page, "templates")).toBe(true);
    expect(errors).toEqual([]);
    await context.close();
  }
});

test("features tab in Turkish", async ({ browser, request }) => {
  const api = await request.get(`${UI}/api/health`).catch(() => null);
  test.skip(!api?.ok(), "AutoAir UI not running");
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await context.newPage();
  await openFeatures(page, "light", "tr");
  await expect(page.getByTestId("admin-tab-features")).toContainText("Özellikler");
  await expect(page.getByText("Değişikliğin nedeni")).toBeVisible();
  await context.close();
});
