import { execFileSync } from "node:child_process";
import { resolve } from "node:path";

import { expect, test } from "@playwright/test";

import { DeployedDags } from "./cleanup";

/**
 * Standalone AutoAir with the git deploy target (see README "Git mode" / docs):
 *   autoair serve --port 8765 with AUTOAIR_STORAGE__MODE=git ...
 * Skipped when that server is not running.
 */
const STANDALONE = process.env.AUTOAIR_STANDALONE_URL ?? "http://127.0.0.1:8765";
const DAG_ID = `tpl_git_${Date.now().toString(36)}`;
// Deployed from the UI (a commit); afterAll deletes it again (another commit).
const dags = new DeployedDags("standalone-git");
test.afterAll(async ({ request }) => {
  await dags.cleanup(request);
});
const REPO = process.env.AUTOAIR_E2E_GIT_REPO ?? resolve(process.cwd(), "../.airflow-dev/git/dags.git");

/** Other tests deploy to the same repository in parallel: Airflow may parse a later commit. */
function contains(version: string, commit: string): boolean {
  try {
    execFileSync("git", ["-C", REPO, "merge-base", "--is-ancestor", commit, version], { stdio: "ignore" });
    return true;
  } catch {
    return version === commit;
  }
}

test("template → deploy commits to git (standalone, Turkish UI)", async ({ page, request }) => {
  const status = await request.get(`${STANDALONE}/api/health`).catch(() => null);
  test.skip(!status?.ok(), "standalone git server not running");
  const token = (await (await request.get("/auth/token")).json()).access_token as string;
  const headers = { Authorization: `Bearer ${token}` };
  const storage = await (await request.get(`${STANDALONE}/api/vcs/status`, { headers })).json();
  test.skip(storage.kind !== "git", "standalone server is not in git mode");

  page.on("pageerror", (e) => console.error("pageerror:", e.message));
  await page.goto(`${STANDALONE}/ui/`);
  await page.evaluate((t) => {
    sessionStorage.setItem("autoair.airflowToken", t);
    localStorage.setItem("autoair.lang", "tr");
  }, token);
  await page.reload();

  // Turkish UI + git storage chip.
  await expect(page.getByText("DAG Oluşturucu").first()).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText(/git/).first()).toBeVisible();

  // Start from the built-in "Fan-out over files" template (dynamic task mapping).
  await page.getByRole("button", { name: "Dosya" }).click();
  await page.getByRole("menuitem", { name: "Şablondan yeni…" }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByText("Fan-out over files").first().click();
  await dialog.getByRole("textbox", { name: "DAG kimliği" }).fill(DAG_ID);
  await dialog.getByRole("button", { name: "Şablonu kullan" }).click();
  await expect(dialog).toBeHidden();
  await expect(page.locator(".react-flow__node")).toHaveCount(3, { timeout: 15_000 });

  // Deploy with Ctrl/⌘+S → a commit in the bundle repository.
  await page
    .locator(".react-flow__pane")
    .first()
    .click({ position: { x: 50, y: 50 } });
  dags.track(`${STANDALONE}/api`, DAG_ID, headers);
  await page.keyboard.press("ControlOrMeta+s");
  await expect
    .poll(async () => (await request.get(`${STANDALONE}/api/vcs/${DAG_ID}/history`, { headers })).json(), {
      timeout: 60_000,
    })
    .toHaveLength(1);
  await page.screenshot({ path: "e2e/.results/standalone-git.png" });

  // Airflow's GitDagBundle picks it up with the commit sha as bundle_version.
  const history = await (await request.get(`${STANDALONE}/api/vcs/${DAG_ID}/history`, { headers })).json();
  await expect
    .poll(
      async () => {
        const r = await request.get(`/api/v2/dags/${DAG_ID}/dagVersions`, { headers });
        const version = r.ok() ? ((await r.json()).dag_versions[0]?.bundle_version ?? null) : null;
        return version ? contains(version, history[0].version) : false;
      },
      { timeout: 60_000 },
    )
    .toBe(true);
});
