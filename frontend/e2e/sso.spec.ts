import { expect, type Page, test } from "@playwright/test";

/**
 * Airflow UI session → AutoAir (docs/sso.md), against a real Airflow.
 *   AUTOAIR_SSO_UI      AutoAir UI on Airflow's host, another port (default http://localhost:8832:
 *                       `vite --mode standalone --port 8832` with AUTOAIR_API_PORT=8831 →
 *                       `autoair serve --port 8831 --airflow-url http://localhost:8080`,
 *                       AUTOAIR_SSO__SESSION_CHECK_SECONDS=3)
 *   AIRFLOW_URL         Airflow (SimpleAuthManager `all_admins`), default http://localhost:8080
 * Cookies are scoped to the host, not the port: Airflow's `_token` reaches AutoAir.
 */
const UI = process.env.AUTOAIR_SSO_UI ?? "http://localhost:8832";
const AIRFLOW = process.env.AIRFLOW_URL ?? "http://localhost:8080";

test.beforeEach(async ({ request }) => {
  const config = await request.get(`${UI}/api/auth/config`).catch(() => null);
  test.skip(!config?.ok(), "AutoAir with /api/auth/config not running on the Airflow host");
  test.skip(!(await config.json()).sso?.enabled, "sso disabled on that server");
  const airflow = await request.get(`${AIRFLOW}/api/v2/version`).catch(() => null);
  test.skip(!airflow || airflow.status() >= 500, "Airflow not running");
});

async function airflowSignIn(page: Page) {
  // all_admins: Airflow's token login sets the `_token` session cookie and redirects to the UI.
  await page.goto(`${AIRFLOW}/auth/token/login`);
  const cookies = await page.context().cookies(AIRFLOW);
  expect(cookies.find((c) => c.name === "_token")?.httpOnly).toBe(true);
}

test("signed in to Airflow → signed in to AutoAir, and signed out with it", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await airflowSignIn(page);

  await page.goto(`${UI}/`);
  await page.evaluate(() => {
    sessionStorage.removeItem("autoair.airflowToken");
    localStorage.setItem("autoair.lang", "en");
  });
  await page.reload();
  // No sign-in page: the builder loads with the Airflow session, and the banner is shown.
  await expect(page.getByText("DAG Builder").first()).toBeVisible({ timeout: 30_000 });
  await expect(page.getByTestId("login-gate")).toHaveCount(0);
  await expect(page.getByTestId("environment-banner")).toContainText("E2E ENVIRONMENT");
  const token = await page.evaluate(() => sessionStorage.getItem("autoair.airflowToken"));
  expect(token).toBeNull(); // the HttpOnly cookie never reaches JavaScript
  const session = await page.evaluate(async () => (await fetch("/api/auth/session")).json());
  expect(session.source).toBe("cookie");
  expect(session.verified).toBe(true);
  await page.screenshot({ path: "e2e/.results/sso-signed-in.png" });

  // A state-changing request with only the cookie and no CSRF header is refused.
  const csrf = await page.evaluate(async () => (await fetch("/api/auth/refresh", { method: "POST" })).status);
  expect(csrf).toBe(403);

  // Signing out of Airflow (cookie gone) signs AutoAir out: the watcher shows the sign-in panel.
  await page.context().clearCookies();
  await expect(page.getByTestId("session-ended")).toBeVisible({ timeout: 20_000 });
  await expect(page.getByTestId("sso-login")).toBeVisible();
  await page.screenshot({ path: "e2e/.results/sso-ended.png" });
  expect(errors).toEqual([]);
});

test("'Sign in with Airflow' opens Airflow's login and continues", async ({ page, context }) => {
  await context.clearCookies();
  await page.goto(`${UI}/`);
  await page.evaluate(() => {
    sessionStorage.removeItem("autoair.airflowToken");
    localStorage.setItem("autoair.lang", "en");
    localStorage.setItem("autoair.colorMode", "dark");
  });
  await page.reload();
  const button = page.getByTestId("sso-login");
  await expect(button).toBeVisible({ timeout: 30_000 });
  await expect(page.getByLabel("Access token")).toBeVisible(); // the other options stay
  await page.screenshot({ path: "e2e/.results/sso-login-dark.png" });

  const popupPromise = page.waitForEvent("popup");
  await button.click();
  const popup = await popupPromise;
  await popup.waitForLoadState();
  expect(popup.url().startsWith(AIRFLOW)).toBe(true);
  // Airflow's login (all_admins signs in immediately; otherwise a user would type here).
  await popup.goto(`${AIRFLOW}/auth/token/login`);
  // The AutoAir tab notices the session and continues into the builder.
  await expect(page.getByText("DAG Builder").first()).toBeVisible({ timeout: 30_000 });
  await expect(page.getByTestId("login-gate")).toHaveCount(0);
});

test("logout in AutoAir clears the shared Airflow session", async ({ page, request }) => {
  await airflowSignIn(page);
  await page.goto(`${UI}/`);
  await expect(page.getByText("DAG Builder").first()).toBeVisible({ timeout: 30_000 });
  const status = await page.evaluate(
    async () => (await fetch("/api/auth/logout", { method: "POST", headers: { "X-AutoAir-CSRF": "1" } })).status,
  );
  expect(status).toBe(200);
  const left = (await page.context().cookies(AIRFLOW)).find((c) => c.name === "_token");
  expect(left).toBeUndefined();
  void request;
});
