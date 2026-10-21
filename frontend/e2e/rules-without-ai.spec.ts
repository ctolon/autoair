import { expect, test } from "@playwright/test";

/**
 * The rules-only review and the secret scan without AI: against a standalone AutoAir with
 * features.ai off (or without the autoair[ai] extra) there is no AI menu, and both checks
 * are in the Run menu, answered by /api/review/rules and /api/lint/secrets.
 *
 *   AUTOAIR_FEATURES__AI=false autoair serve --port 8951 …
 *   AUTOAIR_API_PORT=8951 vite preview --port 8952
 *
 *   AUTOAIR_NOAI_UI   the UI (default http://127.0.0.1:8952)
 *   AUTOAIR_NOAI_DAG  a DAG managed by that AutoAir (default e2e_actions_fail)
 *   AUTOAIR_AI_SHOTS  folder for screenshots (light/dark, en/tr)
 */
const UI = process.env.AUTOAIR_NOAI_UI ?? "http://127.0.0.1:8952";
const DAG = process.env.AUTOAIR_NOAI_DAG ?? "e2e_actions_fail";
const SHOTS = process.env.AUTOAIR_AI_SHOTS ?? "e2e/.results/ai";

test.describe("rules-only checks without AI", () => {
  let token = "";
  test.beforeAll(async ({ request }) => {
    const api = await request.get(`${UI}/api/health`).catch(() => null);
    test.skip(!api?.ok(), "AutoAir UI not running");
    token = (await (await request.get("/auth/token")).json()).access_token as string;
    const status = await request.get(`${UI}/api/ai/status`, { headers: { Authorization: `Bearer ${token}` } });
    test.skip((await status.json()).enabled !== false, "AI is on for that server");
  });

  for (const [mode, lang] of [
    ["light", "en"],
    ["dark", "tr"],
  ] as const) {
    test(`review and secret scan from the Run menu (${mode}, ${lang})`, async ({ page }) => {
      const errors: string[] = [];
      page.on("pageerror", (e) => errors.push(e.message));
      await page.addInitScript(
        ([t, m, l]) => {
          // a `vite preview` build has no server-injected config
          (window as unknown as { __AUTOAIR_CONFIG__?: unknown }).__AUTOAIR_CONFIG__ ??= { apiBase: "/api" };
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
      await page.goto(`${UI}/?dag=${DAG}`);
      await expect(page.locator(".react-flow__node").first()).toBeVisible({ timeout: 30_000 });
      await expect(page.getByRole("button", { name: /^(AI|Yapay zekâ)$/ })).toHaveCount(0);
      const run = page.getByRole("button", { name: /^(Run|Çalıştır)$/ });

      await run.click();
      await page.getByRole("menuitem", { name: /Scan for secrets|Gizli bilgileri tara/ }).click();
      await expect(page.getByText(/rules only|yalnızca kurallar/i)).toBeVisible({ timeout: 20_000 });
      await page.screenshot({ path: `${SHOTS}/secrets-noai-${mode}-${lang}.png` });
      await page.keyboard.press("Escape");

      await run.click();
      await page.getByRole("menuitem", { name: /Review DAG \(rules\)|DAG'i incele \(kurallar\)/ }).click();
      await expect(page.getByText(/rules only|yalnızca kurallar/i)).toBeVisible({ timeout: 20_000 });
      await page.screenshot({ path: `${SHOTS}/review-noai-${mode}-${lang}.png` });
      expect(errors).toEqual([]);
    });
  }
});
