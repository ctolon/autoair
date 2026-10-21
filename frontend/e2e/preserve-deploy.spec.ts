import { readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { type APIRequestContext, type Page, expect, test } from "@playwright/test";

import { DeployedDags } from "./cleanup";

/**
 * Editing existing Python DAGs: a hand-written DAG file keeps its comments and formatting when
 * it is edited on the canvas (the deploy dialog previews the patch as a unified diff), and a
 * generated file edited by hand is never overwritten silently.
 *
 * Needs a private standalone stack (.claude/skills/run-e2e/scripts/private-stack.sh):
 *   AUTOAIR_DEV_URL       the UI (vite preview)
 *   AUTOAIR_E2E_API_BASE  its API (http://127.0.0.1:<port>/api)
 *   E2E_STATE             the stack's state folder (DAG files: $E2E_STATE/dags/autoair, or
 *                         AUTOAIR_E2E_DAGS)
 */
const DEV = process.env.AUTOAIR_DEV_URL ?? "http://localhost:5173";
const API = process.env.AUTOAIR_E2E_API_BASE ?? "";
// The stack serves --dags-folder $E2E_STATE/dags; DAG files go to its default subfolder.
const DAGS =
  process.env.AUTOAIR_E2E_DAGS ?? (process.env.E2E_STATE ? join(process.env.E2E_STATE, "dags", "autoair") : "");

const HAND = (dagId: string) => `"""Nightly load, written by hand."""

from __future__ import annotations

import pendulum

from airflow.providers.standard.operators.bash import BashOperator
from airflow.sdk import DAG

# keep the defaults in one place
DEFAULTS = {"owner": "data"}

with DAG(
    dag_id="${dagId}",
    schedule="@daily",  # nightly
    start_date=pendulum.datetime(2026, 1, 1, tz="UTC"),
    catchup=False,
    default_args=DEFAULTS,
) as dag:
    users = BashOperator(task_id="users",   bash_command="echo users")
    orders = BashOperator(
        task_id="orders",
        bash_command="echo orders",
        retries=2,  # the orders API is flaky
    )

    users >> orders
`;

const dags = new DeployedDags("preserve-deploy");
test.afterAll(({ request }) => dags.cleanup(request));

test.beforeEach(async ({ request }) => {
  test.skip(!API || !DAGS, "needs the private stack (AUTOAIR_E2E_API_BASE, E2E_STATE)");
  const ui = await request.get(DEV).catch(() => null);
  test.skip(!ui?.ok(), "UI not running");
});

async function importHandWritten(request: APIRequestContext, dagId: string): Promise<void> {
  const res = await request.post(`${API}/interchange/python/import`, {
    data: { source: HAND(dagId), filename: `${dagId}.py` },
  });
  expect(res.ok(), await res.text()).toBe(true);
  const spec = (await res.json()).dags[0].spec;
  expect(spec.source.origin).toBe("python");
  dags.track(API, dagId);
  const deployed = await request.put(`${API}/specs/${dagId}`, { data: { spec } });
  expect((await deployed.json()).deployed).toBe(true);
}

async function open(page: Page, dagId: string, color: "light" | "dark"): Promise<string[]> {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.addInitScript((c) => {
    // vite preview proxies /api to the stack's server (a production build looks elsewhere)
    (window as unknown as { __AUTOAIR_CONFIG__: object }).__AUTOAIR_CONFIG__ = { apiBase: "/api" };
    try {
      localStorage.setItem("autoair.lang", "en");
      localStorage.setItem("autoair.colorMode", c);
      localStorage.removeItem("autoair.draft.v1");
    } catch {
      /* about:blank */
    }
  }, color);
  await page.goto(`${DEV}/?dag=${dagId}`);
  await expect(page.getByPlaceholder(/Search operators/)).toBeVisible({ timeout: 30_000 });
  await expect(page.getByTestId("toolbar-dag-id")).toContainText(dagId, { timeout: 30_000 });
  return errors;
}

async function addEmptyOperator(page: Page): Promise<void> {
  const before = await page.locator(".react-flow__node").count();
  await page.getByPlaceholder(/Search operators/).fill("EmptyOperator");
  await page
    .locator('[role="option"]')
    .filter({ has: page.getByText("EmptyOperator", { exact: true }) })
    .first()
    .dblclick();
  await expect(page.locator(".react-flow__node")).toHaveCount(before + 1);
}

async function openDeployDialog(page: Page) {
  await page.getByRole("button", { name: "Deploy options" }).click();
  await page.getByRole("menuitem", { name: /Deploy…|Deploy with message/ }).click();
  await expect(page.getByTestId("deploy-source")).toBeVisible({ timeout: 10_000 });
}

for (const color of ["light", "dark"] as const) {
  test(`deploy dialog previews the patch of a hand-written file (${color})`, async ({ page, request }) => {
    const dagId = `hand_${color}_${Date.now().toString(36)}`;
    await importHandWritten(request, dagId);
    const file = join(DAGS, `${dagId}.py`);
    expect(readFileSync(file, "utf8")).toBe(HAND(dagId)); // the first deploy writes it as is

    const errors = await open(page, dagId, color);
    await addEmptyOperator(page);
    await openDeployDialog(page);
    const dialog = page.getByRole("dialog");
    // Auto = preserve for an imported DAG; the diff only adds the new task.
    await expect(dialog.getByRole("combobox", { name: "Source policy" })).toHaveValue("auto");
    const diff = dialog.getByTestId("source-diff");
    await expect(diff).toContainText("EmptyOperator(task_id=", { timeout: 60_000 });
    await expect(dialog.getByTestId("source-plan")).toHaveAttribute("data-action", "preserve");
    expect(await diff.innerText()).not.toMatch(/^-(?!--)/m); // nothing of the file is removed
    await page.screenshot({ path: `e2e/.results/preserve-deploy-dialog-${color}.png` });

    await dialog.getByRole("button", { name: "Deploy", exact: true }).click();
    await expect(dialog).toBeHidden({ timeout: 60_000 });
    const written = readFileSync(file, "utf8");
    expect(written).toContain("# keep the defaults in one place");
    expect(written).toContain('users = BashOperator(task_id="users",   bash_command="echo users")');
    expect(written).toContain("retries=2,  # the orders API is flaky");
    expect(written).toMatch(/EmptyOperator\(task_id=/);
    expect(written).toMatch(/^from airflow\.providers\.[a-z_.]+ import EmptyOperator$/m);
    expect(errors).toEqual([]);
  });
}

test("a generated file edited by hand stops the deploy and is patched on request", async ({ page, request }) => {
  const dagId = `ejected_${Date.now().toString(36)}`;
  const spec = {
    dag_id: dagId,
    nodes: [
      {
        kind: "operator",
        id: "a",
        task_id: "a",
        operator: "airflow.providers.standard.operators.bash.BashOperator",
        params: { bash_command: "echo a" },
        position: { x: 0, y: 0 },
      },
    ],
  };
  dags.track(API, dagId);
  const first = await request.put(`${API}/specs/${dagId}`, { data: { spec } });
  expect((await first.json()).source.action).toBe("generate");
  const file = join(DAGS, `${dagId}.py`);
  const generated = readFileSync(file, "utf8");
  writeFileSync(file, generated.replace("    t_a = BashOperator(", "    # tuned by hand\n    t_a = BashOperator("));

  const errors = await open(page, dagId, "light");
  await addEmptyOperator(page);
  await page.getByTestId("deploy-button").click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByRole("heading", { name: "The DAG file was changed outside AutoAir" })).toBeVisible({
    timeout: 60_000,
  });
  await expect(dialog.getByTestId("source-overwrite")).toHaveCount(0); // ejected_files=block, allow_force_overwrite off
  await expect(dialog.getByTestId("source-diff")).toContainText("EmptyOperator(task_id=", { timeout: 60_000 });
  await page.screenshot({ path: "e2e/.results/preserve-ejected-dialog-light.png" });
  await dialog.getByTestId("source-apply-preserve").click();
  await expect(page.getByText("Deployed", { exact: true })).toBeVisible({ timeout: 60_000 });
  const written = readFileSync(file, "utf8");
  expect(written).toContain("# tuned by hand");
  expect(written).toMatch(/EmptyOperator\(task_id=/);
  expect(errors).toEqual([]);
});
