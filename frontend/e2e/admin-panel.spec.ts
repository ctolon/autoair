import { type Page, expect, test } from "@playwright/test";

/**
 * The admin panel (user menu → Admin panel) against a standalone AutoAir with a SQL state
 * store: every tab renders real data, filters and the CSV export work, light and dark.
 *
 *   AUTOAIR_ADMIN_UI   UI (default http://127.0.0.1:8811, `vite preview` proxying to the API)
 *
 * The bearer token comes from Airflow's /auth/token (playwright baseURL). Skipped when the
 * server is not running.
 */
const UI = process.env.AUTOAIR_ADMIN_UI ?? "http://127.0.0.1:8811";
const TABS = ["overview", "audit", "users", "ai", "templates", "proposals", "database", "caches", "config"];

async function openPanel(page: Page, token: string, mode: "light" | "dark", lang = "en") {
  await page.addInitScript(
    ([t, m, l]) => {
      try {
        sessionStorage.setItem("autoair.airflowToken", t);
        localStorage.setItem("autoair.lang", l);
        localStorage.setItem("autoair.colorMode", m);
      } catch {
        /* about:blank */
      }
    },
    [token, mode, lang] as const,
  );
  await page.goto(`${UI}/`);
  const user = page.getByRole("button", { name: /Signed in as|giriş yapıldı/ });
  await expect(user).toBeVisible({ timeout: 30_000 });
  await user.click();
  await page.getByTestId("open-admin-panel").click();
  await expect(page.getByTestId("admin-panel")).toBeVisible();
  // Screenshots after the opening animation (the dialog fades in).
  await expect(page.getByTestId("admin-panel")).toHaveCSS("opacity", "1");
  await page.waitForFunction(() => document.getAnimations().every((a) => a.playState !== "running"));
}

test("admin panel shows every section, light and dark", async ({ browser, request }) => {
  const ui = await request.get(UI).catch(() => null);
  const api = await request.get(`${UI}/api/health`).catch(() => null);
  test.skip(!ui?.ok() || !api?.ok(), "AutoAir UI not running");
  const token = (await (await request.get("/auth/token")).json()).access_token as string;

  for (const mode of ["light", "dark"] as const) {
    const context = await browser.newContext();
    const page = await context.newPage();
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(e.message));
    await openPanel(page, token, mode);
    expect(await page.evaluate(() => document.documentElement.classList.contains("dark"))).toBe(mode === "dark");

    // Overview: versions, backend, replicas.
    await expect(page.getByTestId("admin-version")).toBeVisible();
    await expect(page.getByTestId("admin-backend")).toContainText(/file|sqlite|postgresql/);
    await expect(page.getByTestId("admin-replicas").locator("tbody tr").first()).toBeVisible();
    await page.screenshot({ path: `e2e/.results/admin-overview-${mode}.png` });

    for (const tab of TABS.slice(1)) {
      await page.getByTestId(`admin-tab-${tab}`).click();
      const panel = page.getByTestId("admin-panel").locator('[role="tabpanel"][data-state="open"]');
      await expect(panel).toBeVisible();
      await expect(panel.getByRole("status")).toHaveCount(0, { timeout: 15_000 });
      await expect(panel.getByRole("alert")).toHaveCount(0);
      await page.screenshot({ path: `e2e/.results/admin-${tab}-${mode}.png` });
    }

    // Audit: filter by action, export CSV.
    await page.getByTestId("admin-tab-audit").click();
    await page.getByLabel("Action").fill("template_save");
    await page.getByRole("button", { name: "Apply filters" }).click();
    const rows = page.getByTestId("admin-audit-table").locator("tbody tr");
    await expect(rows.first()).toBeVisible();
    await expect
      .poll(async () => [...new Set(await rows.locator("td:nth-child(2)").allInnerTexts())])
      .toEqual(["template_save"]);
    const download = page.waitForEvent("download");
    await page.getByTestId("admin-audit-export").click();
    const file = await download;
    expect(file.suggestedFilename()).toMatch(/^autoair-audit-.*\.csv$/);

    // Users carry their AutoAir roles.
    await page.getByTestId("admin-tab-users").click();
    await expect(page.getByTestId("admin-users-table").locator("tbody tr").first()).toBeVisible();

    // Keyboard: Escape closes the panel.
    await page.keyboard.press("Escape");
    await expect(page.getByTestId("admin-panel")).toHaveCount(0);
    expect(errors).toEqual([]);
    await context.close();
  }
});

test("admin panel in Turkish", async ({ browser, request }) => {
  const ui = await request.get(UI).catch(() => null);
  test.skip(!ui?.ok(), "AutoAir UI not running");
  const token = (await (await request.get("/auth/token")).json()).access_token as string;
  const page = await (await browser.newContext()).newPage();
  await openPanel(page, token, "light", "tr");
  await expect(page.getByRole("heading", { name: "Yönetim paneli" })).toBeVisible();
  await expect(page.getByTestId("admin-tab-ai")).toHaveText("Yapay zekâ kullanımı");
  await page.getByTestId("admin-tab-ai").click();
  await expect(page.getByTestId("admin-ai-requests")).toBeVisible();
  await page.screenshot({ path: "e2e/.results/admin-ai-tr.png" });
});
