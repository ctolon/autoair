import { type Page, expect, test } from "@playwright/test";

import { DeployedDags } from "./cleanup";

/**
 * Two browsers co-edit one DAG through the standalone server (`autoair serve`, collab enabled):
 * edits, presence avatars, remote selections and cursors propagate live.
 * Skipped when the standalone server is not running.
 */
const STANDALONE = process.env.AUTOAIR_STANDALONE_URL ?? "http://127.0.0.1:8765";
const DAG_ID = `collab_${Date.now().toString(36)}`;
const dags = new DeployedDags("collab");
test.afterAll(async ({ request }) => {
  await dags.cleanup(request);
});

async function open(page: Page, token: string, name: string) {
  page.on("pageerror", (e) => console.error(`pageerror[${name}]:`, e.message));
  await page.goto(`${STANDALONE}/ui/`);
  await page.evaluate(
    ([t, n]) => {
      sessionStorage.setItem("autoair.airflowToken", t);
      localStorage.setItem("autoair.lang", "en");
      localStorage.setItem("autoair.collab.name", n);
    },
    [token, name],
  );
  await page.goto(`${STANDALONE}/ui/?dag=${DAG_ID}`);
  await expect(page.locator(".react-flow__node")).toHaveCount(2, { timeout: 30_000 });
  await page.getByRole("button", { name: "Collaborate" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Collaborate" }).click();
  await expect(page.getByTestId("collab-status")).toHaveAttribute("data-status", "connected", { timeout: 15_000 });
  await page.keyboard.press("Escape");
}

test("two editors see each other's changes live", async ({ browser, request }) => {
  const health = await request.get(`${STANDALONE}/api/health`).catch(() => null);
  test.skip(!health?.ok(), "standalone server not running");
  const meta = await request.get(`${STANDALONE}/api/meta`).catch(() => null);
  const token = (await (await request.get("/auth/token")).json()).access_token as string;
  const headers = { Authorization: `Bearer ${token}` };
  const info = await (await request.get(`${STANDALONE}/api/meta`, { headers })).json();
  test.skip(!meta || !info.collab_enabled, "collaboration disabled");

  const spec = {
    spec_version: 2,
    dag_id: DAG_ID,
    output_mode: "python",
    nodes: [
      {
        kind: "operator",
        id: "a",
        task_id: "extract",
        operator: "airflow.providers.standard.operators.empty.EmptyOperator",
        position: { x: 0, y: 0 },
      },
      {
        kind: "operator",
        id: "b",
        task_id: "load",
        operator: "airflow.providers.standard.operators.empty.EmptyOperator",
        position: { x: 320, y: 0 },
      },
    ],
    edges: [{ id: "e1", source: "a", target: "b" }],
  };
  const deployed = await request.put(`${STANDALONE}/api/specs/${DAG_ID}`, { headers, data: { spec } });
  dags.track(`${STANDALONE}/api`, DAG_ID, headers);
  expect(deployed.ok(), await deployed.text()).toBeTruthy();

  const alice = await (await browser.newContext()).newPage();
  const bob = await (await browser.newContext()).newPage();
  await open(alice, token, "Alice Doe");
  await open(bob, token, "Bob Roe");

  // Presence: each sees the other's avatar.
  await expect(alice.getByTestId("collab-peer")).toHaveText("BR");
  await expect(bob.getByTestId("collab-peer")).toHaveText("AD");

  // Alice selects "extract" → Bob sees a remote selection; Alice's cursor shows on Bob's canvas.
  await alice.locator(".react-flow__node", { hasText: "extract" }).click();
  await expect(bob.getByTestId("collab-remote-selection")).toHaveCount(1);
  await alice
    .locator(".react-flow__pane")
    .first()
    .hover({ position: { x: 400, y: 300 } });
  await expect(bob.getByTestId("collab-cursor")).toBeVisible();
  await expect(bob.getByTestId("collab-cursor")).toContainText("Alice Doe");

  // Alice renames the task in the inspector → Bob's canvas updates.
  await alice.locator(".react-flow__node", { hasText: "extract" }).click();
  const taskId = alice.getByRole("textbox", { name: /task id/i }).first();
  await taskId.fill("extract_live");
  await taskId.blur();
  await expect(bob.locator(".react-flow__node", { hasText: "extract_live" })).toBeVisible({ timeout: 10_000 });

  // Bob deletes the edge-target node → it disappears for Alice too.
  await bob.locator(".react-flow__node", { hasText: "load" }).click();
  await bob.keyboard.press("Delete");
  await expect(alice.locator(".react-flow__node")).toHaveCount(1, { timeout: 10_000 });

  // Alice's undo only reverts her own change (the rename), not Bob's deletion.
  await alice
    .locator(".react-flow__pane")
    .first()
    .click({ position: { x: 50, y: 50 } });
  await alice.keyboard.press("ControlOrMeta+z");
  await expect(bob.locator(".react-flow__node", { hasText: "extract" })).toBeVisible();
  await expect(bob.locator(".react-flow__node", { hasText: "extract_live" })).toHaveCount(0);
  await expect(bob.locator(".react-flow__node")).toHaveCount(1);
  await alice.screenshot({ path: "e2e/.results/collab-alice.png" });
  await bob.screenshot({ path: "e2e/.results/collab-bob.png" });
});

test("co-editing inside the Airflow UI (plugin, ticket-authenticated WebSocket)", async ({ browser, request }) => {
  const token = (await (await request.get("/auth/token")).json()).access_token as string;
  const meta = await request.get("/autoair/api/meta", { headers: { Authorization: `Bearer ${token}` } });
  test.skip(!meta.ok() || !(await meta.json()).collab_enabled, "plugin collaboration not available");
  const dagId = `${DAG_ID}_plugin`;
  const spec = {
    spec_version: 2,
    dag_id: dagId,
    output_mode: "python",
    nodes: [
      {
        kind: "operator",
        id: "a",
        task_id: "first",
        operator: "airflow.providers.standard.operators.empty.EmptyOperator",
        position: { x: 0, y: 0 },
      },
    ],
    edges: [],
  };
  const put = await request.put(`/autoair/api/specs/${dagId}`, {
    headers: { Authorization: `Bearer ${token}` },
    data: { spec },
  });
  dags.track("/autoair/api", dagId, { Authorization: `Bearer ${token}` });
  expect(put.ok(), await put.text()).toBeTruthy();

  const pages = await Promise.all([0, 1].map(async () => (await browser.newContext()).newPage()));
  for (const [i, page] of pages.entries()) {
    page.on("pageerror", (e) => console.error(`pageerror[${i}]:`, e.message));
    await page.goto(`/plugin/dag-builder#dag=${dagId}`);
    await expect(page.locator(".react-flow__node")).toHaveCount(1, { timeout: 30_000 });
    await page.getByRole("button", { name: "Collaborate" }).click();
    await page.getByRole("dialog").getByRole("button", { name: "Collaborate" }).click();
    await expect(page.getByTestId("collab-status")).toHaveAttribute("data-status", "connected", { timeout: 15_000 });
    await page.keyboard.press("Escape");
  }
  const [a, b] = pages as [Page, Page];
  await expect(a.getByTestId("collab-peer")).toHaveCount(1);
  await a.locator(".react-flow__node", { hasText: "first" }).click();
  const taskId = a.getByRole("textbox", { name: /task id/i }).first();
  await taskId.fill("first_live");
  await taskId.blur();
  await expect(b.locator(".react-flow__node", { hasText: "first_live" })).toBeVisible({ timeout: 10_000 });
  await b.screenshot({ path: "e2e/.results/collab-plugin.png" });
});
