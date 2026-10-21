import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync, readdirSync, statSync } from "node:fs";
import { join, resolve } from "node:path";

import { type Page, expect, test } from "@playwright/test";

import { DeployedDags } from "./cleanup";
import { DAG_ID, SPEC, prepare } from "./visual/fixtures";

/**
 * The user types a git token for one deploy (storage.git.request_token=required): Ctrl/⌘+S
 * opens the deploy dialog, the masked field toggles visibility, the deploy commits to the
 * bare repository, and the token is left nowhere (browser storage, the page, the state
 * folder). Servers: playwright.git-token.config.ts (ports 8900/8901).
 */
const STATE = process.env.AUTOAIR_GIT_TOKEN_STATE;
const SHOTS = resolve(process.env.AUTOAIR_GIT_TOKEN_SHOTS ?? "e2e/.results/git-token");
const TOKEN = "glpat-e2eUserToken-0123456789";
const HEADER = "X-AutoAir-Git-Token";

test.describe.configure({ mode: "serial" });
test.skip(!STATE, "run with -c playwright.git-token.config.ts");

const commits = () =>
  execFileSync("git", ["--git-dir", `${STATE}/remote.git`, "rev-list", "--count", "main"])
    .toString()
    .trim();

function filesUnder(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    return statSync(path).isDirectory() ? filesUnder(path) : [path];
  });
}

const dags = new DeployedDags("git-token");
test.afterAll(async ({ request }) => {
  await dags.cleanup(request);
});

test.beforeAll(async ({ request }) => {
  dags.track("/api", DAG_ID, { [HEADER]: TOKEN, "X-AutoAir-CSRF": "1" });
  // Seed the DAG (a write: it needs a token too, sent from this loopback client).
  const put = await request.put(`/api/specs/${DAG_ID}`, {
    data: { spec: SPEC, force: true },
    headers: { [HEADER]: TOKEN, "X-AutoAir-CSRF": "1" },
  });
  expect(put.ok(), await put.text()).toBeTruthy();
  const missing = await request.put(`/api/specs/${DAG_ID}`, {
    data: { spec: SPEC, force: true },
    headers: { "X-AutoAir-CSRF": "1" },
  });
  expect(missing.status()).toBe(400);
  expect((await missing.json()).detail.code).toBe("git_token_required");
});

const LABELS = {
  en: { label: "Git token", show: "Show token", hide: "Hide token", deploy: "Deploy", done: "Deployed" },
  tr: { label: "Git belirteci", show: "Belirteci göster", hide: "Belirteci gizle", deploy: "Dağıt", done: "Dağıtıldı" },
} as const;

async function open(page: Page, lang: "en" | "tr"): Promise<void> {
  await prepare(page, { dagId: DAG_ID });
  await page.addInitScript((l) => localStorage.setItem("autoair.lang", l), lang);
  await page.goto("/");
  await expect(page.locator(".react-flow__node")).toHaveCount(4, { timeout: 30_000 });
}

for (const lang of ["en", "tr"] as const) {
  test(`types the token, toggles the eye and deploys (${lang})`, async ({ page, request }, info) => {
    const l = LABELS[lang];
    mkdirSync(SHOTS, { recursive: true });
    const sent: (string | null)[] = [];
    page.on("request", (r) => {
      if (r.method() === "PUT" && r.url().includes(`/api/specs/${DAG_ID}`))
        sent.push(r.headers()[HEADER.toLowerCase()] ?? null);
    });
    await open(page, lang);
    const audits = async () =>
      (await (await request.get(`/api/specs/${DAG_ID}/audit`)).json()) as {
        action: string;
        details: Record<string, unknown>;
      }[];
    const before = (await audits()).length;

    // required: the quick deploy opens the dialog instead of deploying without a token
    await page
      .locator(".react-flow__pane")
      .first()
      .click({ position: { x: 40, y: 40 } });
    await page.keyboard.press("ControlOrMeta+s");
    const dialog = page.getByRole("dialog");
    await expect(dialog).toBeVisible();
    const field = dialog.getByTestId("git-token-input");
    await expect(field).toBeVisible();
    await expect(field).toHaveAttribute("type", "password");
    await expect(field).toHaveAttribute("autocomplete", "off");
    await expect(field).toHaveAttribute("spellcheck", "false");
    await expect(dialog.getByTestId("git-token")).toContainText(l.label);

    // empty: validated before anything is sent
    await dialog.getByRole("button", { name: l.deploy, exact: true }).click();
    await expect(dialog.getByTestId("git-token")).toContainText(
      lang === "en" ? "needs your own git token" : "gerektiriyor",
    );
    expect(sent).toHaveLength(0);

    await field.fill(TOKEN);
    await dialog.getByRole("button", { name: l.show }).click();
    await expect(field).toHaveAttribute("type", "text");
    await page.screenshot({ path: `${SHOTS}/deploy-token-${lang}-${info.project.name}-shown.png` });
    await dialog.getByRole("button", { name: l.hide }).click();
    await expect(field).toHaveAttribute("type", "password");
    await dialog.getByTestId("git-token").scrollIntoViewIfNeeded();
    await page.screenshot({ path: `${SHOTS}/deploy-token-${lang}-${info.project.name}.png` });

    await dialog.getByRole("button", { name: l.deploy, exact: true }).click();
    await expect(dialog).toBeHidden({ timeout: 30_000 });
    await expect(page.getByText(l.done, { exact: true }).first()).toBeVisible({ timeout: 30_000 });
    await expect.poll(async () => (await audits()).length, { timeout: 30_000 }).toBeGreaterThan(before);
    const [latest] = await audits();
    expect(latest?.action).toBe("deploy");
    expect(latest?.details.credential).toBe("request-token");
    expect(JSON.stringify(latest)).not.toContain(TOKEN);
    expect(Number(commits())).toBeGreaterThanOrEqual(2); // seed + the token-authenticated deploys
    expect(sent).toEqual([TOKEN]);
    await page.screenshot({ path: `${SHOTS}/deployed-${lang}-${info.project.name}.png` });

    // nothing kept it: browser storage, the page (the dialog is gone), the URL
    const leftovers = await page.evaluate(
      () =>
        JSON.stringify({ ...localStorage }) +
        JSON.stringify({ ...sessionStorage }) +
        document.body.innerHTML +
        location.href,
    );
    expect(leftovers).not.toContain(TOKEN);
    // reopening the dialog starts empty
    await page.keyboard.press("ControlOrMeta+s");
    await expect(page.getByRole("dialog").getByTestId("git-token-input")).toHaveValue("");
    await page.keyboard.press("Escape");
  });
}

test("the server kept the token nowhere", () => {
  for (const path of filesUnder(STATE as string)) {
    if (statSync(path).size > 5_000_000) continue;
    expect(readFileSync(path).includes(Buffer.from(TOKEN)), path).toBe(false);
  }
});
