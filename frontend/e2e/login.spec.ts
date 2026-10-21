import { expect, type Page, test } from "@playwright/test";

/**
 * Standalone sign-in (LoginGate): Airflow username/password → POST /api/auth/login → token.
 *   AUTOAIR_LOGIN_URL=http://127.0.0.1:8765 (default; the demo, see scripts/demo.sh)
 * Skipped when that server is not running or predates /api/auth/login.
 * The demo Airflow runs SimpleAuthManager with `all_admins`, so any credentials sign in;
 * wrong-password and rate-limit answers are exercised by intercepting the login call
 * (the server side is covered by tests/integration/test_iam_api.py).
 */
const STANDALONE = process.env.AUTOAIR_LOGIN_URL ?? process.env.AUTOAIR_STANDALONE_URL ?? "http://127.0.0.1:8765";
const USERNAME = process.env.AUTOAIR_LOGIN_USER ?? "admin";
const PASSWORD = process.env.AUTOAIR_LOGIN_PASSWORD ?? "admin";

async function openSignedOut(page: Page, opts: { lang?: string; color?: string } = {}) {
  await page.goto(`${STANDALONE}/ui/`);
  await page.evaluate(
    ({ lang, color }) => {
      sessionStorage.removeItem("autoair.airflowToken");
      localStorage.setItem("autoair.lang", lang);
      localStorage.setItem("autoair.colorMode", color);
    },
    { lang: opts.lang ?? "en", color: opts.color ?? "light" },
  );
  await page.reload();
}

test.beforeEach(async ({ request }) => {
  const health = await request.get(`${STANDALONE}/api/health`).catch(() => null);
  test.skip(!health?.ok(), "standalone AutoAir not running");
  const login = await request.post(`${STANDALONE}/api/auth/login`, { data: {} }).catch(() => null);
  // 422: the route exists (empty body rejected); 404/405: password sign-in is not mounted
  test.skip(login?.status() !== 422, "server has no password sign-in (/api/auth/login)");
});

test("sign in with an Airflow username and password", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await openSignedOut(page);

  await expect(page.getByText("Airflow sign-in required")).toBeVisible({ timeout: 30_000 });
  const submit = page.getByRole("button", { name: "Sign in", exact: true });
  await expect(submit).toBeDisabled();
  await expect(page.getByLabel("Access token")).toBeVisible(); // the token option stays

  await page.getByLabel("Username").fill(USERNAME);
  await page.getByLabel("Password").fill(PASSWORD);
  const answer = page.waitForResponse((r) => r.url().endsWith("/api/auth/login"));
  await submit.click();
  const resp = await answer;
  expect(resp.status()).toBe(200);
  expect(resp.headers()["cache-control"]).toBe("no-store");
  expect(resp.request().postDataJSON()).toEqual({ username: USERNAME, password: PASSWORD });

  // signed in: the builder loads and the token lives in this tab's session only
  await expect(page.getByText("Airflow sign-in required")).toBeHidden({ timeout: 30_000 });
  await expect(page.getByText("DAG Builder").first()).toBeVisible({ timeout: 30_000 });
  const token = await page.evaluate(() => sessionStorage.getItem("autoair.airflowToken"));
  expect(token?.split(".")).toHaveLength(3);

  // /api/me answers with the caller's AutoAir roles and permissions
  const me = await page.request.get(`${STANDALONE}/api/me`, { headers: { Authorization: `Bearer ${token}` } });
  expect(me.ok()).toBeTruthy();
  const body = await me.json();
  expect(body.permissions).toContain("builder:use");
  expect(body.login_enabled).toBe(true);
  expect(errors).toEqual([]);
});

test("wrong password and rate limiting are reported", async ({ page }) => {
  let calls = 0;
  await page.route("**/api/auth/login", (route) => {
    calls += 1;
    return calls < 3
      ? route.fulfill({ status: 401, json: { detail: "invalid username or password" } })
      : route.fulfill({ status: 429, headers: { "Retry-After": "42" }, json: { detail: "too many" } });
  });
  await openSignedOut(page);
  await page.getByLabel("Username").fill("alice");
  await page.getByLabel("Password").fill("nope");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.getByRole("alert")).toHaveText("Invalid username or password.");
  // Enter submits the form too
  await page.getByLabel("Password").press("Enter");
  await expect(page.getByRole("alert")).toHaveText("Invalid username or password.");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.getByRole("alert")).toHaveText("Too many failed attempts. Try again in 42 s.");
  expect(await page.evaluate(() => sessionStorage.getItem("autoair.airflowToken"))).toBeNull();
});

test("pasting a token still works (Turkish UI)", async ({ page, request }) => {
  const login = await request.post(`${STANDALONE}/api/auth/login`, {
    data: { username: USERNAME, password: PASSWORD },
  });
  const { access_token: token } = (await login.json()) as { access_token: string };
  await openSignedOut(page, { lang: "tr" });
  await expect(page.getByText("Airflow girişi gerekli")).toBeVisible({ timeout: 30_000 });
  await expect(page.getByRole("button", { name: "Giriş yap", exact: true })).toBeVisible();
  await page.getByLabel("Erişim token'ı").fill(token);
  await page.getByRole("button", { name: "Devam" }).click();
  await expect(page.getByText("DAG Oluşturucu").first()).toBeVisible({ timeout: 30_000 });
});

for (const color of ["light", "dark"]) {
  test(`login gate renders in ${color} mode`, async ({ page }, info) => {
    await openSignedOut(page, { color });
    await expect(page.getByRole("button", { name: "Sign in", exact: true })).toBeVisible({ timeout: 30_000 });
    await page.getByLabel("Username").fill("alice");
    await page.screenshot({ path: info.outputPath(`login-${color}.png`) });
    const isDark = await page.evaluate(() => document.documentElement.classList.contains("dark"));
    expect(isDark).toBe(color === "dark");
  });
}
