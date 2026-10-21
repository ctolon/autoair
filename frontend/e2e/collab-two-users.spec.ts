import { type Page, expect, test } from "@playwright/test";

import { DeployedDags } from "./cleanup";

/**
 * Two users co-edit one DAG, each through their own browser context and — when two URLs are
 * given — through two different AutoAir replicas sharing one database (collab.sync =
 * database): edits (add a node, change a parameter, move), presence avatars and cursors
 * propagate; concurrent edits converge; each user's undo only reverts their own change.
 *
 *   AUTOAIR_COLLAB_UI_A   UI of replica A (default http://127.0.0.1:8811, a Vite dev server)
 *   AUTOAIR_COLLAB_UI_B   UI of replica B (default: the same as A)
 *   AUTOAIR_COLLAB_API_A  API of replica A (default http://127.0.0.1:8810)
 *
 * The bearer token comes from Airflow's /auth/token (playwright baseURL). Skipped when the
 * servers are not running.
 */
const UI_A = process.env.AUTOAIR_COLLAB_UI_A ?? "http://127.0.0.1:8811";
const UI_B = process.env.AUTOAIR_COLLAB_UI_B ?? UI_A;
const API_A = process.env.AUTOAIR_COLLAB_API_A ?? "http://127.0.0.1:8810";
const DAG_ID = `co_edit_${Date.now().toString(36)}`;
const dags = new DeployedDags("collab-two-users");
test.afterAll(async ({ request }) => {
  await dags.cleanup(request);
});
const EMPTY = "airflow.providers.standard.operators.empty.EmptyOperator";

const nodes = (page: Page) => page.locator(".react-flow__node");
const node = (page: Page, text: string) => page.locator(".react-flow__node", { hasText: text });

async function labels(page: Page): Promise<string[]> {
  return (await nodes(page).allInnerTexts()).map((s) => s.split("\n")[0]?.trim() ?? "").sort();
}

async function join(page: Page, ui: string, token: string, name: string, errors: string[]) {
  page.on("pageerror", (e) => errors.push(`${name}: ${e.message}`));
  await page.addInitScript(
    ([t, n]) => {
      try {
        sessionStorage.setItem("autoair.airflowToken", t);
        localStorage.setItem("autoair.lang", "en");
        localStorage.setItem("autoair.collab.name", n);
      } catch {
        /* about:blank */
      }
    },
    [token, name] as const,
  );
  await page.goto(`${ui}/?dag=${DAG_ID}`);
  await expect(nodes(page)).toHaveCount(2, { timeout: 30_000 });
  await page.getByRole("button", { name: "Collaborate" }).click();
  await page.getByRole("dialog").getByRole("button", { name: "Collaborate" }).click();
  await expect(page.getByTestId("collab-status")).toHaveAttribute("data-status", "connected", { timeout: 20_000 });
  await page.keyboard.press("Escape");
}

async function rename(page: Page, from: string, to: string) {
  await node(page, from).first().click();
  const field = page.getByRole("textbox", { name: /task id/i }).first();
  await field.fill(to);
  await field.blur();
}

/** The node's position in flow coordinates (its transform; independent of the viewport). */
async function flowPosition(page: Page, text: string): Promise<string> {
  const style = (await node(page, text).first().getAttribute("style")) ?? "";
  return /translate\([^)]*\)/.exec(style)?.[0] ?? "";
}

test("two users co-edit a DAG across replicas", async ({ browser, request }) => {
  const up = await request.get(`${API_A}/api/health`).catch(() => null);
  const ui = await request.get(UI_A).catch(() => null);
  test.skip(!up?.ok() || !ui?.ok(), "AutoAir / UI servers not running");
  const token = (await (await request.get("/auth/token")).json()).access_token as string;
  const headers = { Authorization: `Bearer ${token}` };
  const meta = await (await request.get(`${API_A}/api/meta`, { headers })).json();
  test.skip(!meta.collab_enabled, "collaboration disabled");

  const spec = {
    spec_version: 3,
    dag_id: DAG_ID,
    output_mode: "python",
    nodes: [
      { kind: "operator", id: "a", task_id: "extract", operator: EMPTY, position: { x: 0, y: 0 } },
      { kind: "operator", id: "b", task_id: "load", operator: EMPTY, position: { x: 320, y: 0 } },
    ],
    edges: [{ id: "e1", source: "a", target: "b" }],
  };
  const put = await request.put(`${API_A}/api/specs/${DAG_ID}`, { headers, data: { spec } });
  dags.track(`${API_A}/api`, DAG_ID, headers);
  expect(put.ok(), await put.text()).toBeTruthy();

  const errors: string[] = [];
  const alice = await (await browser.newContext()).newPage();
  const bob = await (await browser.newContext()).newPage();
  await join(alice, UI_A, token, "Alice Doe", errors);
  await join(bob, UI_B, token, "Bob Roe", errors);

  // Presence: each one sees the other's avatar.
  await expect(alice.getByTestId("collab-peer")).toHaveText("BR", { timeout: 15_000 });
  await expect(bob.getByTestId("collab-peer")).toHaveText("AD", { timeout: 15_000 });

  // Cursor + selection: Alice points at the canvas and selects "extract".
  await node(alice, "extract").first().click();
  await expect(bob.getByTestId("collab-remote-selection")).toHaveCount(1, { timeout: 10_000 });
  await alice
    .locator(".react-flow__pane")
    .first()
    .hover({ position: { x: 420, y: 320 } });
  await alice
    .locator(".react-flow__pane")
    .first()
    .hover({ position: { x: 460, y: 340 } });
  await expect(bob.getByTestId("collab-cursor")).toBeVisible({ timeout: 10_000 });
  await expect(bob.getByTestId("collab-cursor")).toContainText("Alice Doe");

  // Add a node: Alice inserts a BashOperator from the palette; Bob gets it.
  await alice.getByPlaceholder(/Search operators/).fill("BashOperator");
  await alice
    .locator('[role="option"]')
    .filter({ has: alice.getByText("BashOperator", { exact: true }) })
    .first()
    .dblclick();
  await expect(nodes(alice)).toHaveCount(3);
  await expect(nodes(bob)).toHaveCount(3, { timeout: 10_000 });

  // Change a parameter: Bob renames "extract"; Alice sees it.
  await rename(bob, "extract", "extract_live");
  await expect(node(alice, "extract_live")).toBeVisible({ timeout: 10_000 });

  // Move: Alice drags "load"; its flow position changes for Bob.
  const before = await flowPosition(bob, "load");
  const box = await node(alice, "load").first().boundingBox();
  if (!box) throw new Error("load is not visible");
  await alice.mouse.move(box.x + 30, box.y + 12);
  await alice.mouse.down();
  for (let i = 1; i <= 5; i++) await alice.mouse.move(box.x + 30 + 30 * i, box.y + 12 + 25 * i, { steps: 2 });
  await alice.mouse.up();
  await expect.poll(() => flowPosition(bob, "load"), { timeout: 10_000 }).not.toBe(before);
  await expect.poll(() => flowPosition(bob, "load")).toBe(await flowPosition(alice, "load"));

  // Concurrent edits of the same node converge to one value on both sides.
  await Promise.all([rename(alice, "extract_live", "from_alice"), rename(bob, "extract_live", "from_bob")]);
  await expect
    .poll(async () => JSON.stringify(await labels(alice)) === JSON.stringify(await labels(bob)), { timeout: 15_000 })
    .toBe(true);
  const converged = await labels(alice);
  expect(converged.some((l) => l === "from_alice" || l === "from_bob")).toBe(true);

  // Per-user undo: each user changes something, Alice's undo reverts only her change.
  await rename(alice, "load", "load_alice");
  await expect(node(bob, "load_alice")).toBeVisible({ timeout: 10_000 });
  const bobTarget = converged.find((l) => l === "from_alice" || l === "from_bob") ?? "from_bob";
  await rename(bob, bobTarget, "bob_was_here");
  await expect(node(alice, "bob_was_here")).toBeVisible({ timeout: 10_000 });
  await alice
    .locator(".react-flow__pane")
    .first()
    .click({ position: { x: 20, y: 20 } });
  await alice.keyboard.press("ControlOrMeta+z");
  await expect(node(bob, "load_alice")).toHaveCount(0, { timeout: 10_000 });
  await expect(node(bob, "load")).toBeVisible();
  await expect(node(bob, "bob_was_here")).toBeVisible();
  await expect(node(alice, "bob_was_here")).toBeVisible();
  await expect.poll(async () => JSON.stringify(await labels(alice)) === JSON.stringify(await labels(bob))).toBe(true);

  await alice.screenshot({ path: "e2e/.results/collab-two-users-alice.png" });
  await bob.screenshot({ path: "e2e/.results/collab-two-users-bob.png" });
  expect(errors).toEqual([]);
});
