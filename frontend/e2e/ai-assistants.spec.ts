import { type Page, expect, test } from "@playwright/test";

/**
 * AI assistants against a standalone AutoAir whose model is the scripted fake LLM
 * (scripts/e2e_fake_llm.py) and whose Airflow has a failed run of AUTOAIR_AI_DAG:
 *
 *   uv run python scripts/e2e_fake_llm.py --port 8841
 *   AUTOAIR_AI__PROVIDER=openai AUTOAIR_AI__BASE_URL=http://127.0.0.1:8841/v1 \
 *     AUTOAIR_AI__API_KEY=fake AUTOAIR_AI__MODEL=fake-gpt \
 *     AUTOAIR_AI__PRICING='{"fake-gpt": {"input": 1, "output": 2}}' autoair serve --port 8840 …
 *   AUTOAIR_API_PORT=8840 vite preview --port 8842
 *
 *   AUTOAIR_AI_UI     the UI (default http://127.0.0.1:8842)
 *   AUTOAIR_AI_DAG    a DAG managed by that AutoAir (chat, upgrade)
 *   AUTOAIR_AI_RUN_DAG  a managed DAG with a failed run (default AUTOAIR_AI_DAG)
 *   AUTOAIR_AI_SHOTS  folder for screenshots (light/dark, en/tr)
 *
 * Covers: Ctrl/⌘+Shift+A, streaming answer with tool chips, citations and follow-ups, the
 * proposal diff and "Apply to the editor", regenerate, threads (rename/delete), the admin
 * assistant's config snippet, "Analyze with AI" on a failed run with scope selection,
 * report, approval into the editor, "✨ Generate" in the deploy dialog, the upgrade assistant
 * and the admin tabs.
 */
const UI = process.env.AUTOAIR_AI_UI ?? "http://127.0.0.1:8842";
const DAG = process.env.AUTOAIR_AI_DAG ?? "ux_review_etl_mv9l4ppj";
const RUN_DAG = process.env.AUTOAIR_AI_RUN_DAG ?? DAG;
const SHOTS = process.env.AUTOAIR_AI_SHOTS ?? "e2e/.results/ai";

async function open(page: Page, token: string, mode: "light" | "dark", lang: "en" | "tr", dag = DAG) {
  await page.addInitScript(
    ([t, m, l]) => {
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
  await page.goto(`${UI}/?dag=${dag}`);
  await expect(page.locator(".react-flow__node").first()).toBeVisible({ timeout: 30_000 });
}

async function settle(page: Page) {
  await page.waitForFunction(() => document.getAnimations().every((a) => a.playState !== "running"));
}

test.describe("AI assistants", () => {
  let token = "";
  test.beforeAll(async ({ request }) => {
    const ui = await request.get(UI).catch(() => null);
    const api = await request.get(`${UI}/api/health`).catch(() => null);
    test.skip(!ui?.ok() || !api?.ok(), "AutoAir UI not running");
    token = (await (await request.get("/auth/token")).json()).access_token as string;
    const status = await request.get(`${UI}/api/ai/status`, { headers: { Authorization: `Bearer ${token}` } });
    test.skip(!(await status.json()).configured, "AI is not configured on that server");
  });

  for (const [mode, lang] of [
    ["light", "en"],
    ["dark", "tr"],
  ] as const) {
    test(`developer + admin chat (${mode}, ${lang})`, async ({ page }) => {
      const errors: string[] = [];
      page.on("pageerror", (e) => errors.push(e.message));
      await open(page, token, mode, lang);
      await page
        .locator(".react-flow__pane")
        .first()
        .click({ position: { x: 30, y: 30 } });
      await page.keyboard.press("ControlOrMeta+Shift+A");
      const panel = page.getByTestId("ai-chat-panel");
      await expect(panel).toBeVisible();
      await expect(panel.getByTestId("chat-empty")).toBeVisible();
      await settle(page);
      await page.screenshot({ path: `${SHOTS}/chat-empty-${mode}-${lang}.png` });

      await panel.getByTestId("chat-input").fill("Make the first task more robust");
      await panel.getByTestId("chat-input").press("Enter");
      const answer = panel.getByTestId("chat-assistant-message").last();
      await expect(answer).toBeVisible({ timeout: 30_000 });
      await expect(answer.getByTestId("chat-tool-chip").first()).toBeVisible();
      await expect(answer.getByTestId("chat-citations")).toBeVisible();
      await expect(answer.getByTestId("chat-code")).toBeVisible();
      await expect(panel.getByTestId("chat-follow-ups")).toBeVisible();
      const card = answer.getByTestId("proposal-card");
      await expect(card).toBeVisible();
      await expect(card.getByTestId("spec-diff-review")).toContainText("retries");
      await expect(panel.getByTestId("ai-usage").last()).toContainText("$");
      await settle(page);
      await page.screenshot({ path: `${SHOTS}/chat-answer-${mode}-${lang}.png` });

      // approval: the proposal is applied to the editor only on click
      await card.getByTestId("proposal-apply").click();
      await expect(card.getByTestId("proposal-deploy")).toBeVisible();
      await page.screenshot({ path: `${SHOTS}/chat-approved-${mode}-${lang}.png` });

      // regenerate replaces the answer
      await panel.getByTestId("chat-regenerate").click();
      await expect(panel.getByTestId("chat-assistant-message")).toHaveCount(1, { timeout: 30_000 });
      await expect(panel.getByTestId("chat-pending")).toBeHidden({ timeout: 30_000 });

      // thread list: the conversation is stored
      await panel.getByTestId("chat-threads-toggle").click();
      await expect(panel.getByTestId("chat-thread").first()).toBeVisible();
      await page.screenshot({ path: `${SHOTS}/chat-threads-${mode}-${lang}.png` });
      await panel.getByTestId("chat-threads-toggle").click();

      // admin assistant
      await panel.getByTestId("chat-mode-admin").click();
      await panel.getByTestId("chat-input").fill("Why can't viewer deploy?");
      await panel.getByTestId("chat-send").click();
      const adminAnswer = panel.getByTestId("chat-assistant-message").last();
      await expect(adminAnswer.locator('[data-kind="config"]')).toBeVisible({ timeout: 30_000 });
      await expect(adminAnswer).toContainText("Deployer");
      await settle(page);
      await page.screenshot({ path: `${SHOTS}/chat-admin-${mode}-${lang}.png` });

      // full screen
      await panel.getByRole("button", { name: /Full screen|Tam ekran/ }).click();
      await settle(page);
      await page.screenshot({ path: `${SHOTS}/chat-fullscreen-${mode}-${lang}.png` });
      expect(errors).toEqual([]);
    });

    test(`analyze a failed run with AI (${mode}, ${lang})`, async ({ page }) => {
      const errors: string[] = [];
      page.on("pageerror", (e) => errors.push(e.message));
      await open(page, token, mode, lang, RUN_DAG);
      await page.getByRole("tab", { name: /^(Runs|Çalıştırmalar)/ }).click();
      const failed = page
        .getByRole("row")
        .filter({ hasText: /failed|başarısız/i })
        .first();
      await expect(failed).toBeVisible({ timeout: 20_000 });
      await failed.click();
      await page.getByTestId("analyze-with-ai").click();
      const dialog = page.getByTestId("troubleshoot-dialog");
      await expect(dialog).toBeVisible();
      await expect(page.getByTestId("scope-connections")).toBeVisible();
      await page.getByTestId("scope-pools").click();
      await settle(page);
      await page.screenshot({ path: `${SHOTS}/troubleshoot-setup-${mode}-${lang}.png` });
      await page.getByTestId("troubleshoot-start").click();
      const report = page.getByTestId("troubleshoot-report");
      await expect(report).toBeVisible({ timeout: 60_000 });
      await expect(page.getByTestId("troubleshoot-hypothesis").first()).toBeVisible();
      await expect(page.getByTestId("troubleshoot-diff")).toBeVisible();
      await settle(page);
      await page.screenshot({ path: `${SHOTS}/troubleshoot-report-${mode}-${lang}.png`, fullPage: true });
      await page.getByTestId("troubleshoot-diff").getByRole("button").first().click();
      await expect(page.getByRole("region", { name: /Generated code|Üretilen kod/ })).toBeVisible({ timeout: 20_000 });
      await page.screenshot({ path: `${SHOTS}/troubleshoot-codediff-${mode}-${lang}.png` });
      await page.getByTestId("troubleshoot-approve").click();
      await expect(page.getByText(/The fix is in the editor|Düzeltme düzenleyicide/)).toBeVisible();
      // re-run with a different scope goes back to the scope form
      await page.getByTestId("troubleshoot-rerun").click();
      await expect(page.getByTestId("troubleshoot-start")).toBeVisible();
      expect(errors).toEqual([]);
    });
  }

  test("generate a commit message in the deploy dialog", async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(e.message));
    await open(page, token, "light", "en");
    await page
      .locator(".react-flow__pane")
      .first()
      .click({ position: { x: 30, y: 30 } });
    await page.keyboard.press("ControlOrMeta+Shift+A");
    const panel = page.getByTestId("ai-chat-panel");
    await panel.getByTestId("chat-input").fill("Make the first task more robust");
    await panel.getByTestId("chat-input").press("Enter");
    const card = panel.getByTestId("proposal-card").last();
    await expect(card).toBeVisible({ timeout: 30_000 });
    await card.getByTestId("proposal-apply").click();
    // the deploy dialog opens on the message; nothing is deployed by this test
    await card.getByTestId("proposal-deploy").click();
    const governance = page.getByTestId("deploy-governance");
    await expect(governance).toBeVisible();
    await governance.getByTestId("generate-pr-description").click();
    await expect(governance.getByTestId("generated-title")).toContainText("Tune retries and timeouts");
    const dialog = page.getByRole("dialog").filter({ has: governance });
    await expect(dialog.locator("textarea").first()).toHaveValue("Tune retries and timeouts");
    await settle(page);
    await page.screenshot({ path: `${SHOTS}/deploy-message-light-en.png` });
    await page.keyboard.press("Escape");
    expect(errors).toEqual([]);
  });

  test("upgrade assistant and admin AI tabs", async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(e.message));
    await open(page, token, "light", "en");
    await page
      .locator(".react-flow__pane")
      .first()
      .click({ position: { x: 30, y: 30 } });
    await page.keyboard.press("ControlOrMeta+k");
    await page.keyboard.type("Airflow upgrade assistant");
    await page.keyboard.press("Enter");
    await expect(page.getByTestId("upgrade-dialog")).toBeVisible();
    await page.getByTestId("upgrade-run").click();
    await expect(page.getByTestId("upgrade-dag")).toBeVisible({ timeout: 60_000 });
    await settle(page);
    await page.screenshot({ path: `${SHOTS}/upgrade-light-en.png` });
    await page.keyboard.press("Escape");

    // an admin-defined MCP server, so the table renders (header value never shown)
    const put = await page.request.put(`${UI}/api/admin/mcp-servers/metrics`, {
      headers: { Authorization: `Bearer ${token}` },
      data: {
        url: "https://mcp.example.invalid/mcp",
        description: "Grafana metrics",
        headers: { Authorization: "Bearer e2e-secret" },
        tools: ["query_*"],
      },
    });
    expect(put.ok()).toBeTruthy();
    // start without global rules (the test adds them through the UI)
    await page.request.delete(`${UI}/api/ai/rules/global`, { headers: { Authorization: `Bearer ${token}` } });
    for (const mode of ["light", "dark"] as const) {
      // runs after open()'s init script, so it wins on reload
      await page.addInitScript((m) => localStorage.setItem("autoair.colorMode", m), mode);
      await page.reload();
      const user = page.getByRole("button", { name: /Signed in as/ });
      await expect(user).toBeVisible({ timeout: 30_000 });
      await user.click();
      await page.getByTestId("open-admin-panel").click();
      for (const tab of ["aiRules", "mcp"]) {
        await page.getByTestId(`admin-tab-${tab}`).click();
        if (tab === "mcp") {
          await expect(page.getByTestId("admin-mcp-table")).toContainText("metrics");
          await expect(page.getByTestId("admin-panel")).not.toContainText("e2e-secret");
        }
        await settle(page);
        await page.screenshot({ path: `${SHOTS}/admin-${tab}-${mode}.png` });
      }
      await page.getByTestId("admin-tab-aiRules").click();
      if (mode === "light") {
        await page.getByRole("button", { name: "Add rules" }).click();
        const editor = page.getByTestId("ai-rules-editor").first();
        await editor.getByRole("textbox", { name: "Rules (Markdown)" }).fill("- Every DAG has an owner.");
        await editor.getByRole("button", { name: "Save" }).click();
        await expect(page.getByText("Rules for global saved")).toBeVisible();
      }
      await page.keyboard.press("Escape");
    }
    expect(errors).toEqual([]);
  });
});
