import { type APIRequestContext, type Page, expect, test } from "@playwright/test";

import { DeployedDags } from "./cleanup";

/**
 * Editor modes (visual only, code only, both), raw code DAGs (create, Problems, eject,
 * convert back), the environment preview and runtime mode changes, against a private
 * standalone stack (.claude/skills/run-e2e/scripts/private-stack.sh: `autoair serve --no-auth`
 * plus `vite preview`):
 *
 *   AUTOAIR_DEV_URL          the UI (vite preview)
 *   AUTOAIR_E2E_API_BASE     its API (http://127.0.0.1:<port>/api)
 *   AUTOAIR_SCREENSHOT_DIR   where the light/dark screenshots go (default e2e/.results)
 *
 * Mode switches go through the admin API (runtime feature overrides), as an admin would.
 * Skipped when the stack is not running.
 */
const UI = process.env.AUTOAIR_DEV_URL ?? "http://127.0.0.1:8941";
const API = process.env.AUTOAIR_E2E_API_BASE ?? "http://127.0.0.1:8940/api";
const SHOTS = process.env.AUTOAIR_SCREENSHOT_DIR ?? "e2e/.results";
const THEMES = ["light", "dark"] as const;
type Theme = (typeof THEMES)[number];

const RAW_ID = "e2e_raw_modes";
const deployedDags = new DeployedDags("editor-modes");
const VISUAL_ID = "e2e_visual_modes";
const rawCode = (dagId: string, body = "    hello()") => `from datetime import datetime

from airflow.sdk import DAG, task

with DAG(dag_id="${dagId}", schedule=None, start_date=datetime(2025, 1, 1), catchup=False):

    @task
    def hello() -> str:
        return "hello"

${body}
`;

const VISUAL = {
  dag_id: VISUAL_ID,
  environments: { prod: { tags: ["prod_only"], connections: { warehouse: "warehouse_prod" } } },
  nodes: [
    {
      kind: "operator",
      id: "q",
      task_id: "query",
      operator: "airflow.providers.standard.operators.bash.BashOperator",
      params: { bash_command: "echo {{ conn.warehouse.host }}" },
      position: { x: 0, y: 0 },
    },
  ],
};

async function setFeatures(request: APIRequestContext, flags: Record<string, boolean | null>) {
  for (const [name, value] of Object.entries(flags)) {
    const res = await request.put(`${API}/admin/features/${name}`, { data: { value, reason: "e2e editor modes" } });
    expect(res.ok(), `${name}: ${await res.text()}`).toBe(true);
  }
}

const BOTH = { visual_editor: null, code_editor: null } as const;

async function deploySpec(request: APIRequestContext, spec: Record<string, unknown>) {
  const res = await request.put(`${API}/specs/${String(spec.dag_id)}`, { data: { spec, force: true } });
  deployedDags.track(API, String(spec.dag_id));
  expect(res.ok(), await res.text()).toBe(true);
  expect(((await res.json()) as { deployed: boolean }).deployed).toBe(true);
}

async function open(page: Page, theme: Theme, hash = "") {
  await page.addInitScript((m) => {
    (window as unknown as { __AUTOAIR_CONFIG__: object }).__AUTOAIR_CONFIG__ = { apiBase: "/api" };
    try {
      localStorage.setItem("autoair.lang", "en");
      localStorage.setItem("autoair.colorMode", m);
      localStorage.removeItem("autoair.layout.collapsed");
    } catch {
      /* about:blank */
    }
  }, theme);
  await page.goto(`${UI}/${hash}`);
  await expect(page.getByTestId("toolbar-dag-id")).toBeVisible({ timeout: 30_000 });
}

async function openDag(page: Page, dagId: string) {
  await page.keyboard.press("ControlOrMeta+o");
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.getByRole("button", { name: new RegExp(dagId) }).click();
  await expect(page.getByTestId("toolbar-dag-id")).toHaveText(dagId);
}

async function shot(page: Page, name: string, theme: Theme) {
  await page.waitForTimeout(250);
  await page.screenshot({ path: `${SHOTS}/${name}-${theme}.png` });
}

async function typeCode(page: Page, code: string) {
  const editor = page.locator("[data-code-mode-editor] .cm-content");
  await editor.click();
  await page.keyboard.press("ControlOrMeta+a");
  await page.keyboard.insertText(code);
}

function watchErrors(page: Page): string[] {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  return errors;
}

test.describe.configure({ mode: "serial" });

test.beforeAll(async ({ request }) => {
  const ui = await request.get(UI).catch(() => null);
  const api = await request.get(`${API}/health`).catch(() => null);
  test.skip(!ui?.ok() || !api?.ok(), "private stack not running");
  await setFeatures(request, BOTH);
  await deploySpec(request, VISUAL);
  await deploySpec(request, { dag_id: RAW_ID, raw_code: rawCode(RAW_ID), tags: ["handmade"] });
});

test.afterAll(async ({ request }) => {
  await request
    .put(`${API}/admin/features/visual_editor`, { data: { value: null, reason: "e2e cleanup" } })
    .catch(() => null);
  await request
    .put(`${API}/admin/features/code_editor`, { data: { value: null, reason: "e2e cleanup" } })
    .catch(() => null);
  // After the editors are back on: deleting a raw code DAG needs the code editor.
  await deployedDags.cleanup(request);
});

for (const theme of THEMES) {
  test(`both editors: toggle, raw DAG with problems, eject and convert (${theme})`, async ({ page }) => {
    const errors = watchErrors(page);
    await open(page, theme);
    await expect(page.getByTestId("mode-visual")).toBeVisible();
    await expect(page.getByTestId("mode-code")).toBeVisible();
    await shot(page, "mode-both-visual", theme);

    // Open dialog: type badges and the type filter.
    await page.keyboard.press("ControlOrMeta+o");
    const dialog = page.getByRole("dialog");
    await expect(dialog.getByTestId("raw-badge")).toHaveCount(1);
    await dialog.getByTestId("open-type-filter").selectOption("raw");
    await expect(dialog.getByRole("button", { name: new RegExp(VISUAL_ID) })).toHaveCount(0);
    await shot(page, "open-dialog-raw-filter", theme);
    await dialog.getByRole("button", { name: new RegExp(RAW_ID) }).click();
    await expect(page.getByTestId("raw-code-mode")).toBeVisible();
    await expect(page.getByTestId("mode-code")).toHaveCount(0); // no toggle for raw code
    await expect(page.getByTestId("raw-dag-settings")).toBeVisible();
    // Spec-only dialogs are not offered for raw code: the toolbar gates them like the palette.
    const specOnly = /Edit with AI|Fix with AI|Review DAG|Scan for (leaks|secrets)|Optimise parameters|Write docs/;
    const aiMenu = page.getByRole("button", { name: /^AI\b/ });
    if (await aiMenu.count()) {
      await aiMenu.click();
      await expect(page.getByRole("menuitem", { name: "Explain this DAG" })).toBeVisible();
      await expect(page.getByRole("menuitem", { name: specOnly })).toHaveCount(0);
      await page.keyboard.press("Escape");
    }
    await page.getByRole("button", { name: /^Run\b/ }).click();
    await expect(page.getByRole("menuitem", { name: "Validate in Airflow" })).toBeVisible();
    await expect(page.getByRole("menuitem", { name: specOnly })).toHaveCount(0);
    await page.keyboard.press("Escape");

    // Problems with line numbers: a lint error and a hard-coded secret.
    await typeCode(page, rawCode(RAW_ID, '    hello()\n    print(missing_name)\n    password = "hunter2hunter2"'));
    const problems = page.getByRole("tab", { name: /Problems/ });
    await problems.click();
    await expect(page.getByText(/F821/).first()).toBeVisible({ timeout: 30_000 });
    await expect(page.getByText(/hard-coded secret/).first()).toBeVisible();
    await expect(page.getByTestId("raw-errors")).toBeVisible();
    await shot(page, "raw-problems", theme);

    // Convert back: review the importer's result, then replace the raw code.
    await typeCode(page, rawCode(RAW_ID));
    await expect(page.getByTestId("raw-errors")).toHaveCount(0, { timeout: 30_000 });
    await page.getByTestId("raw-to-visual").click();
    await expect(page.getByTestId("convert-review")).toContainText(/hello/, { timeout: 60_000 });
    await shot(page, "convert-review", theme);
    await page.getByTestId("convert-confirm").click();
    await expect(page.getByTestId("raw-code-mode")).toHaveCount(0);
    await expect(page.locator(".react-flow__node")).toHaveCount(1);

    // Eject again (one-way warning), from the code mode of the visual DAG.
    await page.getByTestId("mode-code").click();
    await page.getByTestId("eject-raw").click();
    await expect(page.getByTestId("eject-confirm")).toBeDisabled();
    await page.getByTestId("eject-understood").check();
    await shot(page, "eject-dialog", theme);
    await page.getByTestId("eject-confirm").click();
    await expect(page.getByTestId("raw-code-mode")).toBeVisible();
    await expect(page.getByTestId("raw-badge").first()).toBeVisible();
    await shot(page, "raw-after-eject", theme);
    expect(errors).toEqual([]);
  });

  test(`environment preview of a visual DAG (${theme})`, async ({ page }) => {
    const errors = watchErrors(page);
    await open(page, theme);
    await openDag(page, VISUAL_ID);
    await page.getByTestId("mode-code").click();
    await expect(page.getByTestId("code-mode")).toBeVisible();
    await page.getByTestId("preview-env").selectOption("prod");
    await expect(page.getByTestId("preview-badge")).toHaveText("Preview: prod (read-only)");
    await expect(page.getByTestId("preview-hint")).toBeVisible();
    const editor = page.locator("[data-code-mode-editor] .cm-content");
    await expect(editor).toContainText("warehouse_prod", { timeout: 30_000 });
    await expect(editor).toHaveAttribute("contenteditable", "false");
    await expect(page.getByTestId("code-apply")).toHaveCount(0);
    await shot(page, "env-preview", theme);
    await page.getByTestId("preview-env").selectOption("");
    await expect(editor).not.toContainText("warehouse_prod");
    expect(errors).toEqual([]);
  });

  test(`code only: no canvas, palette or insert (${theme})`, async ({ page, request }) => {
    const errors = watchErrors(page);
    await setFeatures(request, { visual_editor: false, code_editor: null });
    try {
      await open(page, theme);
      await expect(page.getByTestId("code-mode")).toBeVisible();
      await expect(page.locator(".react-flow")).toHaveCount(0);
      await expect(page.getByTestId("panel-palette")).toHaveCount(0);
      await expect(page.getByTestId("mode-visual")).toHaveCount(0);
      await expect(page.getByTestId("code-empty")).toBeVisible();
      await expect(page.locator("[data-code-mode-editor] .cm-content")).toContainText("with DAG", { timeout: 30_000 });
      await page
        .locator("header")
        .first()
        .click({ position: { x: 5, y: 5 } });
      await page.keyboard.press("Shift+A");
      await expect(page.getByPlaceholder(/Insert/i)).toHaveCount(0);
      await shot(page, "mode-code-only", theme);
      await openDag(page, RAW_ID);
      await expect(page.getByTestId("raw-code-mode")).toBeVisible();
      await expect(page.getByTestId("raw-to-visual")).toHaveCount(0); // no visual builder here
      await shot(page, "mode-code-only-raw", theme);
    } finally {
      await setFeatures(request, BOTH);
    }
    expect(errors).toEqual([]);
  });

  test(`visual only: no code mode, raw DAGs read-only (${theme})`, async ({ page, request }) => {
    const errors = watchErrors(page);
    await setFeatures(request, { visual_editor: null, code_editor: false });
    try {
      await open(page, theme);
      await expect(page.locator(".react-flow")).toBeVisible();
      await expect(page.getByTestId("mode-code")).toHaveCount(0);
      await page.getByRole("button", { name: /^File/ }).click();
      await expect(page.getByTestId("menu-new-raw")).toHaveCount(0);
      await page.keyboard.press("Escape");
      await shot(page, "mode-visual-only", theme);
      await openDag(page, RAW_ID);
      await expect(page.getByTestId("raw-code-off")).toBeVisible();
      await expect(page.getByTestId("raw-to-visual")).toHaveCount(0); // the importer is code mode
      await expect(page.getByTestId("deploy-button")).toBeDisabled();
      await expect(page.locator("[data-code-mode-editor] .cm-content")).toHaveAttribute("contenteditable", "false");
      await shot(page, "mode-visual-only-raw", theme);
    } finally {
      await setFeatures(request, BOTH);
    }
    expect(errors).toEqual([]);
  });
}

test("a runtime mode change keeps the open work", async ({ page, request }) => {
  const errors = watchErrors(page);
  await open(page, "light");
  await openDag(page, VISUAL_ID);
  await page.getByTestId("mode-code").click();
  await typeCode(page, "# draft that must survive\n");
  await setFeatures(request, { visual_editor: null, code_editor: false });
  try {
    // The UI re-reads /api/meta when the window regains focus.
    await page.evaluate(() => window.dispatchEvent(new Event("focus")));
    await page.evaluate(() => document.dispatchEvent(new Event("visibilitychange")));
    await expect(page.getByText("Code editing was switched off").first()).toBeVisible({ timeout: 70_000 });
    await expect(page.getByTestId("draft-kept")).toBeVisible();
    await expect(page.getByTestId("toolbar-dag-id")).toHaveText(VISUAL_ID);
    await shot(page, "mode-change-draft-kept", "light");
  } finally {
    await setFeatures(request, BOTH);
  }
  expect(errors).toEqual([]);
});
