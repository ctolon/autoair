import { execFileSync } from "node:child_process";

import { expect, type Page, test } from "@playwright/test";

/**
 * Deploy dialog: commit message + pull request description typed by the user, and the
 * mandatory deploy context (who, versions, validation…) previewed and written to git.
 *   AUTOAIR_SSO_UI     UI of an AutoAir whose deploy target is git in PR mode (see sso.spec.ts)
 *   AUTOAIR_E2E_GOV_REPO  that target's bare repository (to read the commit back)
 */
const UI = process.env.AUTOAIR_SSO_UI ?? "http://localhost:8832";
const AIRFLOW = process.env.AIRFLOW_URL ?? "http://localhost:8080";
const REPO = process.env.AUTOAIR_E2E_GOV_REPO ?? "";

async function signedIn(page: Page, color: "light" | "dark") {
  await page.goto(`${AIRFLOW}/auth/token/login`);
  await page.goto(`${UI}/`);
  await page.evaluate((c) => {
    sessionStorage.removeItem("autoair.airflowToken");
    localStorage.removeItem("autoair.draft.v1");
    localStorage.setItem("autoair.lang", "en");
    localStorage.setItem("autoair.colorMode", c);
  }, color);
  await page.reload();
  await expect(page.getByText("DAG Builder").first()).toBeVisible({ timeout: 30_000 });
}

async function fromTemplate(page: Page, dagId: string) {
  await page.getByRole("button", { name: "File", exact: true }).click();
  await page.getByRole("menuitem", { name: "New from template…" }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByText("Fan-out over files").first().click();
  await dialog.getByRole("textbox", { name: "DAG id" }).fill(dagId);
  await dialog.getByRole("button", { name: "Use template" }).click();
  await expect(dialog).toBeHidden();
  await expect(page.locator(".react-flow__node")).toHaveCount(3, { timeout: 15_000 });
}

async function openDeployDialog(page: Page) {
  await page
    .locator(".react-flow__pane")
    .first()
    .click({ position: { x: 40, y: 40 } });
  await page.keyboard.press("ControlOrMeta+k");
  await page.keyboard.type("Deploy with message");
  await page.keyboard.press("Enter");
  await expect(page.getByTestId("deploy-governance")).toBeVisible({ timeout: 10_000 });
}

test.beforeEach(async ({ request }) => {
  const config = await request.get(`${UI}/api/auth/config`).catch(() => null);
  test.skip(!config?.ok(), "AutoAir test server not running");
  const airflow = await request.get(`${AIRFLOW}/api/v2/version`).catch(() => null);
  test.skip(!airflow || airflow.status() >= 500, "Airflow not running");
});

for (const color of ["light", "dark"] as const) {
  test(`deploy dialog: message, PR description and context preview (${color})`, async ({ page }) => {
    const errors: string[] = [];
    page.on("pageerror", (e) => errors.push(e.message));
    await signedIn(page, color);
    const status = await page.evaluate(async () => (await fetch("/api/vcs/status")).json());
    test.skip(status.kind !== "git" || status.details?.mode !== "pr", "needs a git target in PR mode");

    const dagId = `gov_${color}_${Date.now().toString(36)}`;
    await fromTemplate(page, dagId);
    await openDeployDialog(page);
    const dialog = page.getByRole("dialog");
    const message = dialog.getByRole("textbox", { name: "Message" });
    await message.fill("Tune the fan-out\n\nSmaller batches for the nightly load.");
    const description = dialog.getByTestId("pr-description");
    await expect(description).toBeVisible();
    await description.fill("Reviewers: check the batch size against the pool.");

    await dialog.getByTestId("context-preview-toggle").click();
    const commit = dialog.getByTestId("commit-preview");
    await expect(commit).toContainText("Tune the fan-out", { timeout: 15_000 });
    await expect(commit).toContainText("--- AutoAir deploy context ---");
    await expect(commit).toContainText(`DAG: ${dagId}`);
    await expect(commit).toContainText("Validation: passed");
    await expect(commit).toContainText("Link: http://localhost:8832/ui/?dag=");
    await expect(commit).toContainText("X-AutoAir-User:");
    const pr = dialog.getByTestId("pr-preview");
    await expect(pr).toContainText("Reviewers: check the batch size");
    await expect(pr).toContainText("--- AutoAir deploy context ---");
    await page.screenshot({ path: `e2e/.results/deploy-governance-${color}.png` });

    await dialog.getByRole("button", { name: "Deploy", exact: true }).click();
    await expect(dialog).toBeHidden({ timeout: 60_000 });

    // The commit on the pending branch carries the message, the context and the trailers.
    const audit = await page.evaluate(async (id) => (await fetch(`/api/specs/${id}/audit`)).json(), dagId);
    const record = audit.find((r: { action: string }) => r.action === "deploy");
    expect(record.details.validation.ok).toBe(true);
    expect(record.details.link).toContain(`dag=${dagId}`);
    expect(record.details.autoair_version).toBeTruthy();
    expect(record.details.branch).toBe(`autoair/${dagId}`);
    if (REPO) {
      const body = execFileSync("git", ["-C", REPO, "log", "-1", "--format=%B", `autoair/${dagId}`]).toString();
      expect(body.startsWith("Tune the fan-out")).toBe(true);
      expect(body).toContain("--- AutoAir deploy context ---");
      expect(body).toMatch(/X-AutoAir-Dag: gov_/);
      expect(body).not.toContain("Reviewers: check"); // the PR description is not in the commit
    }
    expect(errors).toEqual([]);
  });
}
