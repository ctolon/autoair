import { type APIRequestContext, type Locator, type Page, expect, test } from "@playwright/test";

/**
 * The standalone operator template editor, against a private standalone stack
 * (.claude/skills/run-e2e/scripts/private-stack.sh: `autoair serve --no-auth` + `vite preview`):
 *
 *   AUTOAIR_DEV_URL          the UI (vite preview)
 *   AUTOAIR_E2E_API_BASE     its API (http://127.0.0.1:<port>/api)
 *   AUTOAIR_SCREENSHOT_DIR   where the screenshots go (default e2e/.results)
 *
 * Creates a template with no DAG open in a code-only deployment (command palette), inserts it
 * into a DAG (the required input is asked, the locked field stays read-only), publishes v2
 * from the gallery (the task then reports the update), compares versions and deletes it.
 * Nothing is deployed; the template is removed in afterAll if a step failed.
 */
const UI = process.env.AUTOAIR_DEV_URL ?? "http://127.0.0.1:9001";
const API = process.env.AUTOAIR_E2E_API_BASE ?? "http://127.0.0.1:9000/api";
const SHOTS = process.env.AUTOAIR_SCREENSHOT_DIR ?? "e2e/.results";
const BASH = "airflow.providers.standard.operators.bash.BashOperator";
const SUFFIX = Date.now().toString(36);
const ID = `e2e-notify-${SUFFIX}`;
const TITLE = `Notify ${SUFFIX}`;
const SHOT_ID = `e2e-shot-${SUFFIX}`;

type Theme = "light" | "dark";
type Lang = "en" | "tr";

async function setFeatures(request: APIRequestContext, flags: Record<string, boolean | null>) {
  for (const [name, value] of Object.entries(flags)) {
    const res = await request.put(`${API}/admin/features/${name}`, { data: { value, reason: "e2e template editor" } });
    expect(res.ok(), `${name}: ${await res.text()}`).toBe(true);
  }
}

function watchErrors(page: Page): string[] {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("console", (m) => {
    if (m.type() === "error" && !/Failed to load resource|ERR_CONNECTION_REFUSED|status of 40[49]/.test(m.text()))
      errors.push(m.text());
  });
  return errors;
}

async function open(page: Page, theme: Theme = "light", lang: Lang = "en") {
  await page.addInitScript(
    ([m, l]) => {
      (window as unknown as { __AUTOAIR_CONFIG__: object }).__AUTOAIR_CONFIG__ = { apiBase: "/api" };
      try {
        localStorage.setItem("autoair.lang", l);
        localStorage.setItem("autoair.colorMode", m);
      } catch {
        /* about:blank */
      }
    },
    [theme, lang] as const,
  );
  await page.goto(`${UI}/`);
  await expect(page.getByTestId("toolbar-dag-id")).toBeVisible({ timeout: 30_000 });
}

async function shot(page: Page, name: string) {
  await page.waitForTimeout(300);
  await page.screenshot({ path: `${SHOTS}/${name}.png` });
}

async function runCommand(page: Page, title: string) {
  // The shortcut is ignored while a form field has the focus.
  await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
  await page.keyboard.press("ControlOrMeta+k");
  await page.locator("[cmdk-input]").fill(title);
  await page.locator("[cmdk-item]").filter({ hasText: title }).first().click();
}

const editor = (page: Page) => page.getByTestId("optpl-editor");

async function pickBash(dialog: Locator) {
  await dialog.getByTestId("optpl-operator-picker").getByRole("textbox").fill("BashOperator");
  await dialog.locator(`[role="option"][data-fqn="${BASH}"]`).click();
  await expect(dialog.getByTestId("optpl-operator")).toHaveText(BASH);
}

async function typeInCode(field: Locator, text: string) {
  const cm = field.locator(".cm-content").first();
  if (await cm.count()) {
    await cm.click();
    await field.page().keyboard.press("ControlOrMeta+a");
    await field.page().keyboard.insertText(text);
  } else {
    await field.locator("input, textarea").first().fill(text);
  }
}

/** Opens the gallery on operator templates and selects the test template. */
async function galleryEdit(page: Page) {
  await runCommand(page, "Operator templates…");
  const gallery = page.getByRole("dialog");
  await gallery.getByRole("button", { name: new RegExp(TITLE) }).click();
  await gallery.getByTestId("template-edit").click();
  await expect(editor(page)).toBeVisible();
}

const nodes = (page: Page) => page.locator(".react-flow__node");
const field = (page: Page, name: string) => page.locator(`[data-autoair-field="${name}"] input`).first();

test.describe.configure({ mode: "serial" });

test.beforeAll(async ({ request }) => {
  const ui = await request.get(UI).catch(() => null);
  const api = await request.get(`${API}/health`).catch(() => null);
  test.skip(!ui?.ok() || !api?.ok(), "private stack not running");
});

test.afterAll(async ({ request }) => {
  await request
    .put(`${API}/admin/features/visual_editor`, { data: { value: null, reason: "e2e cleanup" } })
    .catch(() => null);
  for (const id of [ID, SHOT_ID]) await request.delete(`${API}/templates/${id}`).catch(() => null);
});

test("create (code-only), insert, publish v2, compare and delete", async ({ page, request }) => {
  const errors = watchErrors(page);

  // 1. Code-only deployment, no DAG open: the command palette opens the editor.
  await setFeatures(request, { visual_editor: false });
  await open(page);
  await expect(page.locator(".react-flow")).toHaveCount(0);
  await runCommand(page, "New operator template…");
  const dialog = page.getByRole("dialog");
  await expect(editor(page)).toBeVisible();
  await dialog.getByLabel("Title", { exact: true }).fill(TITLE);
  await expect(dialog.getByLabel("Id", { exact: true })).toHaveValue(`notify-${SUFFIX}`);
  await dialog.getByLabel("Id", { exact: true }).fill(ID);
  await dialog.getByLabel("Owner team").fill("platform");
  await pickBash(dialog);

  // Validation: BashOperator needs bash_command (a live Problem), fixed in the params form.
  await expect(dialog.getByTestId("optpl-problems")).toContainText("bash_command", { timeout: 20_000 });
  await expect(dialog.getByTestId("optpl-save")).toBeDisabled();
  await typeInCode(dialog.locator('[data-autoair-field="bash_command"]').first(), "echo notify");
  await expect(dialog.getByTestId("optpl-problems")).toContainText("Valid", { timeout: 20_000 });

  // Task settings and field modes: retries locked at 4, pool asked on insert.
  await dialog.getByRole("tab", { name: "Task settings" }).click();
  await dialog.locator('[data-autoair-field="retries"] input').first().fill("4");
  await dialog.getByRole("tab", { name: "Field modes" }).click();
  await dialog.getByRole("radiogroup", { name: "Mode of retries" }).getByText("Locked").click();
  await dialog.getByLabel("Add a field:").selectOption("pool");
  await expect(dialog.getByRole("radiogroup", { name: "Mode of pool" })).toBeVisible();
  await dialog.getByRole("tab", { name: "Code preview" }).click();
  await expect(dialog.locator(".cm-content").last()).toContainText("BashOperator", { timeout: 20_000 });
  await shot(page, "optpl-new-code-only-light-en");
  await dialog.getByTestId("optpl-save").click();
  await expect(editor(page)).toBeHidden();

  const v1 = await (await request.get(`${API}/templates/${ID}`)).json();
  expect(v1.version).toBe("1");
  expect(v1.blueprint.locked).toEqual(["retries"]);
  expect(v1.blueprint.required).toEqual(["pool"]);
  expect(v1.blueprint.node.retries).toBe(4);

  // 2. Both editors: insert it into a DAG; the required pool is asked first.
  await setFeatures(request, { visual_editor: null });
  await page.reload();
  await expect(page.getByPlaceholder(/Search operators/)).toBeVisible({ timeout: 30_000 });
  await page.getByPlaceholder(/Search operators/).fill(TITLE);
  const row = page.locator('[role="option"]').filter({ hasText: TITLE });
  await row.dblclick();
  const form = page.getByRole("dialog");
  await expect(form.getByText(`Insert “${TITLE}”`)).toBeVisible();
  const insert = form.getByRole("button", { name: "Insert" });
  await expect(insert).toBeDisabled();
  await form.locator('[data-autoair-field="pool"] input').fill("etl");
  await insert.click();
  await expect(nodes(page)).toHaveCount(1);
  await expect(page.getByTestId("template-banner")).toContainText(TITLE);

  // The locked field is read-only in the inspector.
  await page.getByRole("tab", { name: "Execution" }).click();
  await expect(field(page, "retries")).toHaveValue("4");
  await field(page, "retries").fill("0");
  await expect(field(page, "retries")).toHaveValue("4");

  // 3. Publish v2 from the gallery (Edit): the description and a default change.
  await galleryEdit(page);
  await expect(dialog.getByLabel("Id", { exact: true })).toBeDisabled();
  await dialog.getByLabel("Description").fill("Posts a notification (v2)");
  await expect(dialog.getByTestId("optpl-problems")).toContainText("Valid", { timeout: 20_000 });
  await dialog.getByTestId("optpl-save").click();
  await expect(editor(page)).toBeHidden();
  const versions = await (await request.get(`${API}/templates/${ID}/versions`)).json();
  expect(versions.map((v: { version: string }) => v.version)).toEqual(["2", "1"]);
  expect((await (await request.get(`${API}/templates/${ID}/versions/1`)).json()).description).toBe("");

  // The inserted task reports the newer version (banner badge and an info problem).
  await expect(page.getByTestId("template-banner")).toContainText("v2 available", { timeout: 20_000 });
  await field(page, "pool").fill("etl2");
  await page.getByRole("tab", { name: /Problems/ }).click();
  await expect(page.getByText(/has a newer version than this task \(v1\)/).first()).toBeVisible({
    timeout: 20_000,
  });

  // 4. Version history with a diff, then a stale save is a friendly 409.
  await galleryEdit(page);
  await dialog.getByRole("tab", { name: "Versions" }).click();
  await expect(dialog.getByTestId("optpl-history")).toContainText("v1");
  await expect(dialog.getByText("Changes from v1 to this draft")).toBeVisible();
  await expect(dialog.locator(".cm-mergeView, .cm-merge-a").first()).toBeVisible({ timeout: 20_000 });
  await shot(page, "optpl-history-light-en");
  // Someone else publishes v3 meanwhile.
  const current = await (await request.get(`${API}/templates/${ID}`)).json();
  const bump = await request.post(`${API}/templates`, {
    data: { ...current, version: null, description: "changed elsewhere" },
    headers: { "If-Match": current.etag },
  });
  expect(bump.ok()).toBe(true);
  await dialog.getByLabel("Description").fill("my change");
  await dialog.getByTestId("optpl-save").click();
  await expect(dialog.getByTestId("optpl-conflict")).toContainText("now v3");
  await shot(page, "optpl-conflict-light-en");
  await dialog.getByRole("button", { name: "Reload latest" }).click();
  await expect(dialog.getByLabel("Description")).toHaveValue("changed elsewhere");

  // 5. Delete with confirmation: the open DAG's use of it is pointed out.
  await dialog.getByRole("button", { name: "Delete" }).first().click();
  const confirm = dialog.getByTestId("optpl-delete-confirm");
  await expect(confirm).toContainText("The open DAG has one task created from this template.");
  await shot(page, "optpl-delete-light-en");
  await confirm.getByRole("button", { name: "Delete template" }).click();
  await expect(editor(page)).toBeHidden();
  expect((await request.get(`${API}/templates/${ID}`)).status()).toBe(404);
  expect((await request.get(`${API}/templates/${ID}/versions`)).status()).toBe(404);
  expect(errors).toEqual([]);
});

for (const theme of ["light", "dark"] as const) {
  for (const lang of ["en", "tr"] as const) {
    test(`editor screenshots (${theme}, ${lang})`, async ({ page, request }) => {
      const errors = watchErrors(page);
      await open(page, theme, lang);
      await page.getByTestId("palette-new-operator-template").click();
      const dialog = page.getByRole("dialog");
      await expect(editor(page)).toBeVisible();
      await dialog
        .locator("input")
        .first()
        .fill(lang === "tr" ? "Slack bildirimi" : "Slack notification");
      await pickBash(dialog);
      await expect(dialog.getByTestId("optpl-problems").locator("button").first()).toBeVisible({ timeout: 20_000 });
      await shot(page, `optpl-params-${theme}-${lang}`);
      await dialog.getByRole("tab").nth(2).click(); // field modes
      await dialog.locator("select[aria-label]").last().selectOption({ index: 1 });
      await shot(page, `optpl-modes-${theme}-${lang}`);
      await page.keyboard.press("Escape");
      await expect(editor(page)).toBeHidden();
      // Invalid input never breaks the editor: a malformed import shows an error toast.
      await page.getByTestId("palette-new-operator-template").click();
      await page.getByTestId("optpl-import-file").setInputFiles({
        name: "bad.json",
        mimeType: "application/json",
        buffer: Buffer.from("{ not json"),
      });
      await expect(editor(page)).toBeVisible();
      expect(errors).toEqual([]);
      void request;
    });
  }
}
