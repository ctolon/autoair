import { type Page, expect, test } from "@playwright/test";

/**
 * The documentation knowledge base: the admin panel's "Knowledge base" tab and a developer
 * assistant answer that cites a KB page. Runs against a standalone AutoAir (no auth) whose
 * model is the scripted fake LLM (scripts/e2e_fake_llm.py: prompts starting with "docs:"
 * call search_docs in the docs scope) and whose KB has been populated without internet,
 * e.g. a `path` source of Markdown files:
 *
 *   uv run python scripts/e2e_fake_llm.py --port 8961
 *   AUTOAIR_AI__PROVIDER=openai AUTOAIR_AI__BASE_URL=http://127.0.0.1:8961/v1 \
 *     AUTOAIR_AI__API_KEY=fake AUTOAIR_AI__MODEL=fake-gpt \
 *     AUTOAIR_KB__SOURCES='[{"id":"local-docs","kind":"path","url":"<folder>","layout":"files",
 *       "component":"core","public_base_url":"https://airflow.apache.org/docs/apache-airflow/stable/"}]'
 *   autoair kb refresh && autoair serve --no-auth --port 8962 …
 *   AUTOAIR_API_PORT=8962 vite preview --port 8963
 *
 *   AUTOAIR_KB_UI      the UI (default http://127.0.0.1:8963)
 *   AUTOAIR_KB_QUERY   a query with a hit in that KB (default "jinja template not found")
 *   AUTOAIR_KB_SHOTS   folder for screenshots (light/dark, en/tr)
 */
const UI = process.env.AUTOAIR_KB_UI ?? "http://127.0.0.1:8963";
const QUERY = process.env.AUTOAIR_KB_QUERY ?? "jinja template not found";
const SHOTS = process.env.AUTOAIR_KB_SHOTS ?? "e2e/.results/kb";

let token = "";

async function prepare(page: Page, mode: "light" | "dark", lang: "en" | "tr") {
  await page.addInitScript(
    ([t, m, l]) => {
      // `vite preview` serves the standalone build without the server-injected config.
      (window as unknown as { __AUTOAIR_CONFIG__?: { apiBase: string } }).__AUTOAIR_CONFIG__ ??= { apiBase: "/api" };
      try {
        if (t) sessionStorage.setItem("autoair.airflowToken", t);
        localStorage.setItem("autoair.lang", l);
        localStorage.setItem("autoair.colorMode", m);
        localStorage.removeItem("autoair.draft.v1");
      } catch {
        /* about:blank */
      }
    },
    [token, mode, lang] as const,
  );
}

async function settle(page: Page) {
  await page.waitForFunction(() => document.getAnimations().every((a) => a.playState !== "running"));
}

test.describe("knowledge base", () => {
  test.beforeAll(async ({ request }) => {
    // A bearer token from Airflow (playwright baseURL), as in the other standalone specs.
    const auth = await request.get("/auth/token").catch(() => null);
    token = auth?.ok() ? (((await auth.json()) as { access_token?: string }).access_token ?? "") : "";
    const status = await request
      .get(`${UI}/api/kb/status`, { headers: token ? { Authorization: `Bearer ${token}` } : {} })
      .catch(() => null);
    const body = status?.ok() ? ((await status.json()) as { store?: { pages?: number } }) : null;
    test.skip(!body, "AutoAir with the knowledge base not running");
    test.skip(!body?.store?.pages, "the knowledge base is empty (run autoair kb refresh)");
  });

  for (const [mode, lang] of [
    ["light", "en"],
    ["dark", "en"],
    ["light", "tr"],
    ["dark", "tr"],
  ] as const) {
    test(`admin tab (${mode}, ${lang})`, async ({ page }) => {
      const errors: string[] = [];
      page.on("pageerror", (e) => errors.push(e.message));
      await prepare(page, mode, lang);
      await page.goto(`${UI}/`);
      const user = page.getByRole("button", { name: /Signed in as|giriş yapıldı/ }).first();
      await expect(user).toBeVisible({ timeout: 30_000 });
      await user.click();
      await page.getByTestId("open-admin-panel").click();
      await expect(page.getByTestId("admin-panel")).toBeVisible();
      await page.getByTestId("admin-tab-kb").click();
      const kb = page.getByTestId("admin-kb");
      await expect(kb).toBeVisible({ timeout: 15_000 });
      await expect(page.getByTestId("admin-kb-pages")).not.toContainText(/^0$/);
      await expect(page.getByTestId("admin-kb-sources").locator("tbody tr").first()).toBeVisible();
      await expect(page.getByTestId("admin-kb-components")).toBeVisible();

      await page.getByTestId("admin-kb-query").fill(QUERY);
      await page.getByTestId("admin-kb-search").click();
      const hit = page.getByTestId("admin-kb-hit").first();
      await expect(hit).toBeVisible({ timeout: 15_000 });
      await expect(hit.getByRole("link").first()).toHaveAttribute("href", /^https?:\/\//);
      await settle(page);
      await page.screenshot({ path: `${SHOTS}/kb-tab-${mode}-${lang}.png`, fullPage: true });

      // Invalid live input is shown inline, never saved, never breaks the panel.
      const versions = page.getByTestId("admin-kb-sources").locator("tbody tr").first().locator("input").last();
      const before = await versions.inputValue();
      await versions.fill("bad/version");
      await expect(page.getByTestId("admin-kb-sources")).toContainText("bad/version");
      await versions.fill(before);
      await page.getByTestId("admin-kb-runs").scrollIntoViewIfNeeded();
      await settle(page);
      await page.screenshot({ path: `${SHOTS}/kb-tab-bottom-${mode}-${lang}.png` });

      if (mode === "light" && lang === "en") {
        // Bundle round trip: export downloads a zip, importing it reports the pages.
        const download = page.waitForEvent("download");
        await page.getByTestId("admin-kb-export").click();
        const file = await download;
        expect(file.suggestedFilename()).toMatch(/^autoair-kb-.*\.zip$/);
        const path = `${SHOTS}/kb-bundle.zip`;
        await file.saveAs(path);
        await page.getByTestId("admin-kb-import-file").setInputFiles(path);
        await expect(page.getByText(/^Imported \d+ pages/)).toBeVisible({ timeout: 30_000 });
      }

      // A refresh from the panel: started (202) or already running (409 toast); never a crash.
      await page.getByTestId("admin-kb-refresh").click();
      await expect(page.getByTestId("admin-kb-outcome")).toBeVisible({ timeout: 30_000 });
      expect(errors).toEqual([]);
    });
  }

  for (const [mode, lang] of [
    ["light", "en"],
    ["dark", "tr"],
  ] as const) {
    test(`assistant cites the docs (${mode}, ${lang})`, async ({ page, request }) => {
      const ai = await request
        .get(`${UI}/api/ai/status`, { headers: { Authorization: `Bearer ${token}` } })
        .catch(() => null);
      test.skip(!ai?.ok() || !(await ai.json()).configured, "AI is not configured on that server");
      const errors: string[] = [];
      page.on("pageerror", (e) => errors.push(e.message));
      await prepare(page, mode, lang);
      await page.goto(`${UI}/`);
      await expect(page.locator(".react-flow__pane").first()).toBeVisible({ timeout: 30_000 });
      await page
        .locator(".react-flow__pane")
        .first()
        .click({ position: { x: 30, y: 30 } });
      await page.keyboard.press("ControlOrMeta+Shift+A");
      const panel = page.getByTestId("ai-chat-panel");
      await expect(panel).toBeVisible();
      await panel.getByTestId("chat-input").fill(`docs: ${QUERY}`);
      await panel.getByTestId("chat-input").press("Enter");
      const answer = panel.getByTestId("chat-assistant-message").last();
      await expect(answer).toContainText("[1]", { timeout: 30_000 });
      const citations = answer.getByTestId("chat-citations");
      await expect(citations).toBeVisible();
      await expect(citations.getByRole("link").first()).toHaveAttribute(
        "href",
        /^https:\/\/airflow\.apache\.org\/docs\//,
      );
      await settle(page);
      await page.screenshot({ path: `${SHOTS}/kb-chat-${mode}-${lang}.png` });
      expect(errors).toEqual([]);
    });
  }
});
