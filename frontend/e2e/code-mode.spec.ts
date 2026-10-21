import { type Page, expect, test } from "@playwright/test";

/**
 * Code mode: edit the generated DAG file, the edit is imported back into the visual spec
 * (one undoable step); broken code keeps the draft and shows located problems.
 * Needs a Vite dev server (AUTOAIR_DEV_URL, default :5173) and Airflow for the token.
 */
const DEV = process.env.AUTOAIR_DEV_URL ?? "http://localhost:5173";

async function start(page: Page, token: string) {
  await page.addInitScript((t) => {
    try {
      sessionStorage.setItem("autoair.airflowToken", t);
      localStorage.setItem("autoair.lang", "en");
      localStorage.removeItem("autoair.layout.collapsed");
    } catch {
      /* about:blank */
    }
  }, token);
  await page.goto(`${DEV}/`);
  await expect(page.getByPlaceholder(/Search operators/)).toBeVisible({ timeout: 30_000 });
}

async function replaceCode(page: Page, code: string) {
  const editor = page.locator("[data-code-mode-editor] .cm-content");
  await editor.click();
  await page.keyboard.press("ControlOrMeta+a");
  await page.keyboard.insertText(code);
}

const dagCode = (dagId: string, body: string) => `import datetime

from airflow.providers.standard.operators.empty import EmptyOperator
from airflow.sdk import DAG

with DAG(
    dag_id="${dagId}",
    schedule=None,
    start_date=datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc),
) as dag:
${body}
`;

test("code edits sync back to the canvas and stay undoable", async ({ page, request }) => {
  const dev = await request.get(DEV).catch(() => null);
  test.skip(!dev?.ok(), "vite dev server not running");
  const token = (await (await request.get("/auth/token")).json()).access_token as string;
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await start(page, token);
  const dagId = (await page.getByTestId("toolbar-dag-id").innerText()).trim();

  await page.getByTestId("mode-code").click();
  await expect(page.getByTestId("code-mode")).toBeVisible();
  // The palette and the inspector leave the screen in code mode.
  await expect(page.getByPlaceholder(/Search operators/)).toHaveCount(0);

  // Broken code: located problem, the draft is kept, nothing is applied.
  await replaceCode(page, dagCode(dagId, '    a = EmptyOperator(task_id="start"\n'));
  await expect(page.getByTestId("code-status")).toContainText(/problem/, { timeout: 60_000 });
  await expect(page.getByTestId("code-problems")).toContainText(/line \d+/);
  await expect(page.getByTestId("code-apply")).toBeDisabled();

  // Valid code: the check previews the change, Apply replaces the spec.
  await replaceCode(
    page,
    dagCode(dagId, '    a = EmptyOperator(task_id="start")\n    b = EmptyOperator(task_id="finish")\n    a >> b'),
  );
  await expect(page.getByTestId("code-status")).toContainText(/ready: \+2/, { timeout: 60_000 });
  await expect(page.getByTestId("code-sync-panel")).toContainText("start");
  await page.getByTestId("code-apply").click();
  await expect(page.getByText("Code applied to the canvas")).toBeVisible();

  await page.getByTestId("mode-visual").click();
  const nodes = page.locator(".react-flow__node");
  await expect(nodes).toHaveCount(2);
  await expect(page.locator(".react-flow__edge")).toHaveCount(1);

  // One undo step reverts the whole apply.
  await page.getByRole("button", { name: "Undo", exact: true }).first().click();
  await expect(nodes).toHaveCount(0);
  expect(errors).toEqual([]);
});
