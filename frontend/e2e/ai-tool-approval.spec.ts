import { type Page, expect, test } from "@playwright/test";

/**
 * Tool-call approval in the developer assistant against an AutoAir whose model is the
 * scripted fake LLM (scripts/e2e_fake_llm.py; a prompt with "remember:" makes it call the
 * `remember` tool, a write tool that waits for approval):
 *
 *   uv run python scripts/e2e_fake_llm.py --port <llm>
 *   AUTOAIR_AI__PROVIDER=openai AUTOAIR_AI__BASE_URL=http://127.0.0.1:<llm>/v1 \
 *     AUTOAIR_AI__API_KEY=fake AUTOAIR_AI__MODEL=fake-gpt \
 *     .claude/skills/run-e2e/scripts/private-stack.sh start
 *   AUTOAIR_APPROVAL_UI=$AUTOAIR_DEV_URL npx playwright test e2e/ai-tool-approval.spec.ts
 *
 *   AUTOAIR_APPROVAL_UI     the editor page (standalone root, or <airflow>/plugin/dag-builder)
 *   AUTOAIR_APPROVAL_API    the AutoAir API base (default <UI origin>/api)
 *   AUTOAIR_APPROVAL_TOKEN_URL  an Airflow /auth/token URL (plugin mode; none for --no-auth)
 *   AUTOAIR_APPROVAL_LABEL  name used in screenshot files (default standalone)
 *   AUTOAIR_APPROVAL_SHOTS  folder for screenshots (light/dark, en/tr)
 *
 * Covers: the paused answer with its approval card (tool, kind, masked arguments), approving
 * (the run resumes and completes the same message), denying with a reason (the model gets
 * the denial) and the composer hint while an approval is pending.
 */
const UI = process.env.AUTOAIR_APPROVAL_UI ?? process.env.AUTOAIR_DEV_URL ?? "http://127.0.0.1:8842";
const API = process.env.AUTOAIR_APPROVAL_API ?? `${new URL(UI).origin}/api`;
const TOKEN_URL = process.env.AUTOAIR_APPROVAL_TOKEN_URL ?? "";
const LABEL = process.env.AUTOAIR_APPROVAL_LABEL ?? "standalone";
const SHOTS = process.env.AUTOAIR_APPROVAL_SHOTS ?? "e2e/.results/ai-approval";

async function open(page: Page, token: string, mode: "light" | "dark", lang: "en" | "tr") {
  await page.addInitScript(
    ([t, m, l]) => {
      // vite preview serves index.html without the server-injected config
      if (!t) (window as unknown as { __AUTOAIR_CONFIG__?: unknown }).__AUTOAIR_CONFIG__ ??= { apiBase: "/api" };
      try {
        if (t) sessionStorage.setItem("autoair.airflowToken", t);
        localStorage.setItem("autoair.lang", l);
        localStorage.setItem("autoair.colorMode", m);
        localStorage.setItem("theme", m); // Airflow's own theme inside the plugin
        localStorage.removeItem("autoair.draft.v1");
      } catch {
        /* about:blank */
      }
    },
    [token, mode, lang] as const,
  );
  await page.emulateMedia({ colorScheme: mode });
  await page.goto(UI);
  await expect(page.locator(".react-flow__pane").first()).toBeVisible({ timeout: 30_000 });
}

async function settle(page: Page) {
  await page.waitForFunction(() => document.getAnimations().every((a) => a.playState !== "running"));
}

test.describe("AI tool approval", () => {
  let token = "";
  test.beforeAll(async ({ request }) => {
    if (TOKEN_URL) token = (await (await request.get(TOKEN_URL)).json()).access_token as string;
    const headers: Record<string, string> = token ? { Authorization: `Bearer ${token}` } : {};
    const status = await request.get(`${API}/ai/status`, { headers }).catch(() => null);
    test.skip(!status?.ok(), "AutoAir not running");
    const body = status ? ((await status.json()) as { configured?: boolean }) : {};
    test.skip(!body.configured, "AI is not configured on that server");
  });

  for (const [mode, lang] of [
    ["light", "en"],
    ["dark", "tr"],
  ] as const) {
    test(`approve and deny tool calls (${mode}, ${lang})`, async ({ page }) => {
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
      if (await panel.getByTestId("chat-empty").isHidden()) {
        await panel.getByRole("button", { name: /New chat|Yeni sohbet/ }).click();
      }

      // the agent calls a write tool: the answer pauses with an approval card
      await panel.getByTestId("chat-input").fill("remember: always set retries on sensors");
      await panel.getByTestId("chat-input").press("Enter");
      const answer = panel.getByTestId("chat-assistant-message").last();
      const card = answer.getByTestId("tool-approval-card");
      await expect(card).toHaveAttribute("data-status", "pending", { timeout: 30_000 });
      await expect(card.getByTestId("tool-approval-call")).toContainText("remember");
      await expect(card.getByTestId("tool-approval-arguments")).toContainText("always set retries on sensors");
      await expect(panel.getByTestId("chat-hint")).toContainText(/cancels|iptal/);
      await settle(page);
      await page.screenshot({ path: `${SHOTS}/approval-pending-${LABEL}-${mode}-${lang}.png` });

      // approve: the run resumes and completes the same message
      await card.getByTestId("tool-approval-approve").click();
      await card.getByTestId("tool-approval-submit").click();
      await expect(card).toHaveAttribute("data-status", "resolved", { timeout: 30_000 });
      await expect(answer).toContainText("Stored the convention");
      await expect(answer.getByTestId("tool-approval-decision")).toBeVisible();
      await expect(panel.getByTestId("chat-assistant-message")).toHaveCount(1);
      await settle(page);
      await page.screenshot({ path: `${SHOTS}/approval-approved-${LABEL}-${mode}-${lang}.png` });

      // deny with a reason: the tool does not run, the model gets the denial
      await panel.getByTestId("chat-input").fill("remember: use pools for the warehouse");
      await panel.getByTestId("chat-send").click();
      const second = panel.getByTestId("chat-assistant-message").last();
      const card2 = second.getByTestId("tool-approval-card");
      await expect(card2).toHaveAttribute("data-status", "pending", { timeout: 30_000 });
      await card2.getByTestId("tool-approval-deny").click();
      await card2.getByTestId("tool-approval-reason").fill("pools are managed elsewhere");
      await settle(page);
      await page.screenshot({ path: `${SHOTS}/approval-deny-${LABEL}-${mode}-${lang}.png` });
      await card2.getByTestId("tool-approval-submit").click();
      await expect(card2).toHaveAttribute("data-status", "resolved", { timeout: 30_000 });
      await expect(second).toContainText("did not store it");
      await expect(card2.getByTestId("tool-approval-decision")).toContainText(/denied|reddedildi/);
      await settle(page);
      await page.screenshot({ path: `${SHOTS}/approval-denied-${LABEL}-${mode}-${lang}.png` });
      expect(errors).toEqual([]);
    });
  }
});
