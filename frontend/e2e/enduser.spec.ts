import { type Locator, type Page, expect, test } from "@playwright/test";

/**
 * End-user features on the standalone server: DAG timezone, Python functions and script
 * files written in the inspector, the hard-coded secret warning, creating an Airflow
 * connection from a picker, the deploy target dialog and duplicating a DAG.
 */
const STANDALONE = process.env.AUTOAIR_STANDALONE_URL ?? "http://127.0.0.1:8765";

async function open(page: Page, token: string) {
  await page.addInitScript((t) => {
    try {
      sessionStorage.setItem("autoair.airflowToken", t);
      localStorage.setItem("autoair.lang", "en");
    } catch {
      /* about:blank */
    }
  }, token);
  await page.goto(`${STANDALONE}/ui/`);
  await expect(page.getByPlaceholder(/Search operators/)).toBeVisible();
}

async function box(l: Locator) {
  const b = await l.boundingBox();
  if (!b) throw new Error("element not visible");
  return b;
}

async function addOperator(page: Page, name: string) {
  await page.getByPlaceholder(/Search operators/).fill(name);
  await page
    .locator('[role="option"]')
    .filter({ has: page.getByText(name, { exact: true }) })
    .first()
    .dblclick();
  const node = page.locator(".react-flow__node").filter({ hasText: name }).last();
  await node.dispatchEvent("click");
  return node;
}

/** The latest generated source the code preview received from the server. */
function trackCode(page: Page): () => string {
  let latest = "";
  page.on("response", async (resp) => {
    if (!resp.url().endsWith("/api/codegen") || !resp.ok()) return;
    const body = (await resp.json().catch(() => null)) as { code?: string | null } | null;
    if (body?.code) latest = body.code;
  });
  return () => latest;
}

async function expectCode(page: Page, latest: () => string, ...needles: (string | RegExp)[]) {
  await page.getByRole("tab", { name: "Code" }).click();
  for (const needle of needles) {
    await expect
      .poll(latest, { timeout: 20_000 })
      .toEqual(typeof needle === "string" ? expect.stringContaining(needle) : expect.stringMatching(needle));
  }
}

test("end-user features", async ({ page, request }) => {
  const health = await request.get(`${STANDALONE}/api/health`).catch(() => null);
  test.skip(!health?.ok(), "standalone server not running");
  const token = (await (await request.get("/auth/token")).json()).access_token as string;
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  const latest = trackCode(page);
  await open(page, token);

  // PythonOperator: python_callable written in place (function mode).
  const python = await addOperator(page, "PythonOperator");
  await page.getByRole("button", { name: /^Value source of python_callable/ }).click();
  await page.getByRole("menuitem", { name: "Python function (write it here)" }).click();
  const fn = page.locator('[data-autoair-field*="python_callable"] .cm-content').first();
  await expect(fn).toContainText("def ");
  await fn.click();
  await page.keyboard.press("ControlOrMeta+a");
  await page.keyboard.insertText('def build_report(**context):\n    api_key = "abcd1234efgh"\n    return 42\n');

  // Code fields grow from the bottom edge and open in a large editor.
  const fnBox = page.locator('[data-autoair-field*="python_callable"] [data-code-editor]').first();
  const before = (await box(fnBox.locator(".cm-editor"))).height;
  const handle = fnBox.getByRole("separator");
  const hb = await box(handle);
  await page.mouse.move(hb.x + hb.width / 2, hb.y + hb.height / 2);
  await page.mouse.down();
  await page.mouse.move(hb.x + hb.width / 2, hb.y + 150, { steps: 5 });
  await page.mouse.up();
  expect((await box(fnBox.locator(".cm-editor"))).height).toBeGreaterThan(before + 100);
  await fnBox.getByRole("button", { name: /in a large editor/ }).click();
  const big = page.getByRole("dialog", { name: /Python function for python_callable/ });
  await expect(big.locator(".cm-content")).toContainText("def build_report");
  await big.getByRole("button", { name: "Close" }).click();

  // BashOperator: bash_command from a script file run with bash.
  await addOperator(page, "BashOperator");
  await page.getByRole("button", { name: /^Value source of bash_command/ }).click();
  await page.getByRole("menuitem", { name: "Script file (write it here)" }).click();
  await expect(page.getByRole("textbox", { name: "File name for bash_command" })).toHaveValue("script.sh");

  await expectCode(
    page,
    latest,
    "def build_report(**context):",
    /python_callable=build_report/,
    /bash_command="bash "\s*\+\s*_script_file\(/,
  );
  await page.getByRole("tab", { name: "Problems" }).click();
  await expect(page.getByText(/hard-coded secret/).first()).toBeVisible();

  // DAG timezone → pendulum dates in that zone.
  await page.locator(".react-flow__pane").click({ position: { x: 5, y: 5 } });
  await page.getByRole("tab", { name: "Schedule" }).click();
  const tz = page
    .getByRole("combobox", { name: "DAG timezone" })
    .or(page.getByRole("textbox", { name: "DAG timezone" }));
  // A half-typed zone must not break the inspector: it is flagged, not committed.
  await tz.first().fill("Euro");
  await expect(page.getByText(/Unknown timezone/)).toBeVisible();
  await expect(page.getByTestId("error-boundary")).toHaveCount(0);
  await tz.first().fill("Europe/Istanbul");
  await tz.first().press("Enter");
  await expectCode(page, latest, 'tz="Europe/Istanbul"');

  // A new Airflow connection from the HTTP connection picker.
  await python.dispatchEvent("click");
  await addOperator(page, "HttpOperator");
  const connId = `e2e_http_${Date.now().toString(36)}`;
  await page.getByRole("button", { name: /more parameters/ }).click();
  const connField = page.locator('[data-autoair-field="http_conn_id"]');
  await connField.getByRole("button", { name: "New connection" }).click();
  const dialog = page.getByRole("dialog", { name: "New Airflow connection" });
  await dialog.getByRole("textbox", { name: "Connection id" }).fill(connId);
  await dialog.getByRole("textbox", { name: "Host" }).fill("https://example.com");
  await dialog.getByRole("button", { name: "Create" }).click();
  await expect(dialog).toBeHidden();
  await expect(connField.locator("input").first()).toHaveValue(connId);
  const created = await request.get(`/api/v2/connections/${connId}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  expect(created.ok()).toBeTruthy();
  await request.delete(`/api/v2/connections/${connId}`, { headers: { Authorization: `Bearer ${token}` } });

  // Deploy target dialog lists this Airflow's options.
  await page.getByTestId("storage-chip").click();
  const target = page.getByRole("dialog", { name: "Deploy target" });
  await expect(target.getByText("DAG folder").first()).toBeVisible();
  await target.getByRole("button", { name: "Close" }).click();

  // Duplicate opens the copy as a new, unsaved DAG.
  await page.locator(".react-flow__pane").click({ position: { x: 5, y: 5 } });
  await page.getByRole("tab", { name: "General" }).click();
  const dagId = await page.getByRole("textbox", { name: "DAG id" }).inputValue();
  await page.getByRole("button", { name: "File", exact: true }).click();
  await page.getByRole("menuitem", { name: "Duplicate…" }).click();
  const dup = page.getByRole("dialog", { name: "Duplicate DAG" });
  await dup.getByRole("textbox", { name: "New DAG id" }).fill(`${dagId}_copy2`);
  await dup.getByRole("button", { name: "Duplicate" }).click();
  await expect(page.getByRole("textbox", { name: "DAG id" })).toHaveValue(`${dagId}_copy2`);
  await expect(page.locator(".react-flow__node").filter({ hasText: "HttpOperator" })).toHaveCount(1);

  expect(errors).toEqual([]);
});
