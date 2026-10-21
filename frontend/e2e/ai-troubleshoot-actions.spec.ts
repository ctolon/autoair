import { type Page, expect, test } from "@playwright/test";

/**
 * Airflow actions of AI troubleshooting against a standalone AutoAir whose model is the
 * scripted fake LLM (scripts/e2e_fake_llm.py) and whose Airflow has failed runs of
 * AUTOAIR_ACTIONS_DAG. The server runs with AUTOAIR_AI__TROUBLESHOOT__AIRFLOW_ACTIONS=apply,
 * so the dialog offers every mode; each test picks one (a request may only be stricter):
 *
 *   uv run python scripts/e2e_fake_llm.py --port 8953
 *   AUTOAIR_AI__PROVIDER=openai AUTOAIR_AI__BASE_URL=http://127.0.0.1:8953/v1 \
 *     AUTOAIR_AI__API_KEY=fake AUTOAIR_AI__MODEL=fake-gpt \
 *     AUTOAIR_AI__TROUBLESHOOT__AIRFLOW_ACTIONS=apply autoair serve --port 8951 …
 *   AUTOAIR_API_PORT=8951 vite preview --port 8952
 *
 *   AUTOAIR_AI_UI         the UI (default http://127.0.0.1:8952)
 *   AUTOAIR_ACTIONS_DAG   a managed DAG with failed runs (default e2e_actions_fail)
 *   AUTOAIR_AI_SHOTS      folder for screenshots (light/dark, en/tr)
 *
 * The fake model proposes a retry of the failed task, a clear on another DAG (must be
 * blocked) and a connection fix (an instruction).
 */
const UI = process.env.AUTOAIR_AI_UI ?? "http://127.0.0.1:8952";
const DAG = process.env.AUTOAIR_ACTIONS_DAG ?? "e2e_actions_fail";
const SHOTS = process.env.AUTOAIR_AI_SHOTS ?? "e2e/.results/ai";

async function open(page: Page, token: string, mode: "light" | "dark", lang: "en" | "tr") {
  await page.addInitScript(
    ([t, m, l]) => {
      // a `vite preview` build has no server-injected config (AutoAir's own page overrides it)
      (window as unknown as { __AUTOAIR_CONFIG__?: unknown }).__AUTOAIR_CONFIG__ ??= { apiBase: "/api" };
      try {
        sessionStorage.setItem("autoair.airflowToken", t);
        localStorage.setItem("autoair.lang", l);
        localStorage.setItem("autoair.colorMode", m);
        localStorage.removeItem("autoair.draft.v1");
      } catch {
        /* about:blank */
      }
    },
    [token, mode, lang] as const,
  );
  await page.goto(`${UI}/?dag=${DAG}`);
  await expect(page.locator(".react-flow__node").first()).toBeVisible({ timeout: 30_000 });
}

async function settle(page: Page) {
  await page.waitForFunction(() => document.getAnimations().every((a) => a.playState !== "running"));
}

async function analyse(page: Page, mode: "hitl" | "apply") {
  await page.getByRole("tab", { name: /^(Runs|Çalıştırmalar)/ }).click();
  const failed = page
    .getByRole("row")
    .filter({ hasText: /failed|başarısız/i })
    .first();
  await expect(failed).toBeVisible({ timeout: 60_000 });
  await failed.click();
  await page.getByTestId("analyze-with-ai").click();
  await expect(page.getByTestId("troubleshoot-dialog")).toBeVisible();
  const picker = page.getByTestId("troubleshoot-actions-picker");
  await expect(picker).toBeVisible();
  await page.getByTestId(`actions-mode-${mode}`).click();
}

test.describe("AI troubleshooting: Airflow actions", () => {
  let token = "";
  test.beforeAll(async ({ request }) => {
    const ui = await request.get(UI).catch(() => null);
    const api = await request.get(`${UI}/api/health`).catch(() => null);
    test.skip(!ui?.ok() || !api?.ok(), "AutoAir UI not running");
    token = (await (await request.get("/auth/token")).json()).access_token as string;
    const headers = { Authorization: `Bearer ${token}` };
    const status = await request.get(`${UI}/api/ai/status`, { headers });
    test.skip(!(await status.json()).configured, "AI is not configured on that server");
    const options = await request.get(`${UI}/api/ai/troubleshoot/options`, { headers });
    test.skip((await options.json()).actions_mode !== "apply", "server is not in apply mode");
  });

  for (const [theme, lang] of [
    ["light", "en"],
    ["dark", "tr"],
  ] as const) {
    test(`hitl: execute an approved action (${theme}, ${lang})`, async ({ page }) => {
      const errors: string[] = [];
      page.on("pageerror", (e) => errors.push(e.message));
      await open(page, token, theme, lang);
      await analyse(page, "hitl");
      await settle(page);
      await page.screenshot({ path: `${SHOTS}/actions-setup-${theme}-${lang}.png` });
      await page.getByTestId("troubleshoot-start").click();
      const actions = page.getByTestId("troubleshoot-actions");
      await expect(actions).toBeVisible({ timeout: 60_000 });
      await expect(page.getByTestId("troubleshoot-actions-mode")).toHaveAttribute("data-mode", "hitl");
      const ready = actions.locator('[data-testid="troubleshoot-action"][data-status="proposed"]');
      const blocked = actions.locator('[data-testid="troubleshoot-action"][data-status="blocked"]');
      await expect(ready).toHaveCount(1);
      await expect(blocked).toHaveCount(1); // the clear on another DAG
      await expect(blocked).toContainText(/another DAG|Başka bir DAG/);
      await expect(blocked.getByTestId("troubleshoot-action-execute")).toHaveCount(0);
      await expect(actions.locator('[data-status="instruction"]')).toHaveCount(1);
      await actions.scrollIntoViewIfNeeded();
      await settle(page);
      await page.screenshot({ path: `${SHOTS}/actions-hitl-${theme}-${lang}.png` });

      await ready.getByTestId("troubleshoot-action-execute").click();
      const done = actions.locator('[data-testid="troubleshoot-action"][data-status="succeeded"]');
      await expect(done.or(actions.locator('[data-status="skipped"]'))).toHaveCount(1, { timeout: 30_000 });
      await expect(page.getByTestId("troubleshoot-action-log")).toBeVisible();
      await expect(actions.getByTestId("troubleshoot-action-execute")).toHaveCount(0);
      await settle(page);
      await page.screenshot({ path: `${SHOTS}/actions-executed-${theme}-${lang}.png` });
      expect(errors).toEqual([]);
    });

    test(`apply: vetted actions run after the analysis (${theme}, ${lang})`, async ({ page }) => {
      const errors: string[] = [];
      page.on("pageerror", (e) => errors.push(e.message));
      await open(page, token, theme, lang);
      await analyse(page, "apply");
      await page.getByTestId("troubleshoot-start").click();
      const actions = page.getByTestId("troubleshoot-actions");
      await expect(actions).toBeVisible({ timeout: 60_000 });
      await expect(page.getByTestId("troubleshoot-actions-mode")).toHaveAttribute("data-mode", "apply");
      const log = page.getByTestId("troubleshoot-action-log");
      await expect(log).toBeVisible();
      await expect(log).toContainText(/retry/);
      await expect(actions.getByTestId("troubleshoot-action-execute")).toHaveCount(0);
      await expect(actions.locator('[data-status="blocked"]')).toHaveCount(1);
      await actions.scrollIntoViewIfNeeded();
      await settle(page);
      await page.screenshot({ path: `${SHOTS}/actions-apply-${theme}-${lang}.png` });
      expect(errors).toEqual([]);
    });
  }
});
