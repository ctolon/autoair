import { type APIRequestContext, type Locator, type Page, expect, test } from "@playwright/test";

import { DeployedDags } from "./cleanup";
import { DAG_ID, SPEC, prepare } from "./visual/fixtures";

/**
 * Regressions found while writing the docs: caret in empty code fields, Python indentation,
 * the deploy commit message, the template gallery (dark mode, operator templates).
 *
 * Hermetic, like the visual suite: a standalone production build served by `vite preview`
 * against a throw-away `autoair serve --no-auth` (Airflow REST mocked by the fixtures):
 *
 *   npx vite build --mode standalone --outDir /tmp/aa-dist
 *   AUTOAIR_IAM__ANONYMOUS_ROLES='["Admin"]' autoair serve --no-auth --port 8880 --dags-folder /tmp/aa-dags &
 *   AUTOAIR_API_PORT=8880 npx vite preview --mode standalone --outDir /tmp/aa-dist --port 8881 &
 *   AUTOAIR_E2E_URL=http://127.0.0.1:8881 npx playwright test e2e/editor-regressions.spec.ts
 */
const URL = process.env.AUTOAIR_E2E_URL ?? null;
const SHOTS = process.env.AUTOAIR_E2E_SHOTS ?? null;
const OPERATOR_TEMPLATE = "e2e-regression-notify";

test.skip(!URL, "set AUTOAIR_E2E_URL to a standalone build served against a backend");

async function open(page: Page, theme: "light" | "dark" = "light", dagId: string | null = null) {
  await prepare(page, { dagId });
  await page.addInitScript((th) => {
    try {
      localStorage.setItem("autoair.colorMode", th);
    } catch {
      /* storage unavailable */
    }
  }, theme);
  await page.goto(`${URL}/`);
  await expect(page.getByPlaceholder(/Search operators/)).toBeVisible({ timeout: 30_000 });
}

async function add(page: Page, query: string, name: string) {
  await page.getByPlaceholder(/Search operators/).fill(query);
  await page
    .locator('[role="option"]')
    .filter({ has: page.getByText(name, { exact: true }) })
    .first()
    .dblclick();
  await page.locator(".react-flow__node").last().dispatchEvent("click");
}

/** Types key by key, slower than CodeMirror's 200 ms "still typing" window. */
async function typeSlowly(page: Page, text: string) {
  for (const ch of text) {
    await page.keyboard.type(ch);
    await page.waitForTimeout(250);
  }
  await page.waitForTimeout(400);
}

const docOf = (editor: Locator) =>
  editor.evaluate((el) => [...el.querySelectorAll(".cm-line")].map((l) => (l as HTMLElement).innerText).join("\n"));

async function shot(page: Page, name: string, target?: Locator) {
  if (!SHOTS) return;
  await (target ?? page).screenshot({ path: `${SHOTS}/${name}.png` });
}

test.describe("code fields", () => {
  test("typing while the editor loads keeps the text, focus and caret", async ({ page }) => {
    // Hold back the CodeMirror chunk: the field is a plain textarea until it arrives.
    let release = () => {};
    const gate = new Promise<void>((resolve) => (release = resolve));
    await page.route(/\/assets\/CodeEditor-[^/]+\.js$/, async (route) => {
      await gate;
      await route.continue();
    });
    await open(page);
    await add(page, "BashOperator", "BashOperator");
    await page.getByRole("textbox", { name: "bash_command" }).click();
    await page.keyboard.type("ec");
    release();
    const editor = page.locator('.cm-content[aria-label="bash_command"]');
    await expect(editor).toBeVisible();
    await expect(editor).toBeFocused();
    await page.keyboard.type("ho");
    expect(await docOf(editor)).toBe("echo");
  });

  test("an empty optional field keeps the caret and focus after the first character", async ({ page }) => {
    await open(page);
    await add(page, "EmailOperator", "EmailOperator");
    await page.getByRole("button", { name: /more parameters/ }).click();
    const more = page.locator('[data-scope="collapsible"][data-part="content"]');
    const editor = more.locator(".cm-content").first();
    const label = await editor.getAttribute("aria-label");
    await editor.click();
    await typeSlowly(page, "abc");
    const field = page.locator(`.cm-content[aria-label="${label}"]`);
    expect(await docOf(field)).toBe("abc");
    await expect(field).toBeFocused();
  });

  test("a JSON field keeps the typed text (no reformat while typing)", async ({ page }) => {
    await open(page);
    await add(page, "BashOperator", "BashOperator");
    const env = page.locator('.cm-content[aria-label="env"]');
    await env.click();
    // closeBrackets adds the closing quote / brace; type over them.
    await typeSlowly(page, '{"a": "b", "c": "d"}');
    expect(await docOf(env)).toBe('{"a": "b", "c": "d"}');
  });
});

test.describe("python indentation", () => {
  test("Enter indents a task body by 4 spaces", async ({ page }) => {
    await open(page);
    await add(page, "Python task", "Python task");
    const body = page.getByRole("textbox", { name: "Function body" });
    await body.click();
    await page.keyboard.press("ControlOrMeta+a");
    await page.keyboard.press("Delete");
    await page.keyboard.type("if x:");
    await page.waitForTimeout(400);
    // Close a completion popup (Enter would accept it); Escape with none open deselects the node.
    if (await page.locator(".cm-tooltip-autocomplete").count()) await page.keyboard.press("Escape");
    await page.keyboard.press("Enter");
    await page.keyboard.type("y");
    expect(await docOf(body)).toBe("if x:\n    y");
  });

  test("Enter indents code mode by the file's 4 spaces", async ({ page }) => {
    await open(page);
    await add(page, "Python task", "Python task");
    await page.getByRole("button", { name: "Code", exact: true }).click();
    const code = page.locator("[data-code-mode-editor] .cm-content");
    const def = code.locator(".cm-line", { hasText: "def python_1():" });
    await expect(def).toBeVisible({ timeout: 15_000 });
    await def.click();
    await page.keyboard.press("End");
    await page.keyboard.press("Enter");
    await page.keyboard.type("z = 1");
    const lines = (await docOf(code)).split("\n");
    const at = lines.findIndex((l) => l.includes("def python_1():"));
    expect(lines[at + 1]).toBe("        z = 1");
    await page.waitForTimeout(1500);
    await shot(page, "code-mode-indent");
  });
});

test.describe("deploy", () => {
  const dags = new DeployedDags("editor-regressions");
  test.beforeAll(async ({ request }) => {
    const put = await request.put(`${URL}/api/specs/${DAG_ID}`, { data: { spec: SPEC, force: true } });
    dags.track(`${URL}/api`, DAG_ID);
    expect(put.ok(), await put.text()).toBeTruthy();
  });
  test.afterAll(async ({ request }) => {
    await dags.cleanup(request);
  });

  test("the suggested commit message names only the changed task", async ({ page }) => {
    await open(page, "light", DAG_ID);
    await expect(page.locator(".react-flow__node")).toHaveCount(4, { timeout: 30_000 });
    await page.locator(".react-flow__node").filter({ hasText: "load_orders" }).click();
    await page.getByRole("tab", { name: "Execution" }).click();
    await page.locator('[data-autoair-field="retries"] input').first().fill("5");
    await page.getByRole("button", { name: "More deploy options" }).click();
    await page.getByRole("menuitem", { name: "Deploy…" }).click();
    const dialog = page.getByRole("dialog");
    await expect(dialog.getByRole("textbox", { name: "Message" })).toHaveValue(`Update ${DAG_ID}: change load_orders`);
  });
});

async function saveOperatorTemplate(request: APIRequestContext) {
  const res = await request.post(`${URL}/api/templates`, {
    data: {
      id: OPERATOR_TEMPLATE,
      title: "Notify the team",
      description: "Posts a message to the team channel",
      category: "operator",
      tags: ["ops"],
      kind: "operator",
      owner_team: "platform",
      blueprint: {
        node: {
          kind: "operator",
          id: "n",
          task_id: "notify",
          operator: "airflow.providers.standard.operators.bash.BashOperator",
          params: { bash_command: "echo notify" },
          retries: 3,
        },
        locked: ["retries"],
        required: [],
      },
    },
  });
  expect(res.ok(), await res.text()).toBeTruthy();
}

test.describe("template gallery", () => {
  test.beforeAll(async ({ request }) => saveOperatorTemplate(request));
  test.afterAll(async ({ request }) => {
    await request.delete(`${URL}/api/templates/${OPERATOR_TEMPLATE}`);
  });

  for (const theme of ["light", "dark"] as const) {
    test(`DAG template preview is themed (${theme})`, async ({ page }) => {
      await open(page, theme);
      await page.getByRole("button", { name: "File", exact: true }).click();
      await page.getByRole("menuitem", { name: /New from template/ }).click();
      await page.getByRole("button", { name: /Daily ETL/ }).click();
      const graph = page.getByRole("img", { name: /Graph preview of Daily ETL/ });
      await expect(graph.locator(".react-flow__node")).toHaveCount(3);
      await expect(graph.locator(".react-flow")).toHaveClass(new RegExp(`\\b${theme}\\b`));
      // Node cards use the theme's panel colour (React Flow's `light` class used to force white).
      const [card, panel] = await graph
        .locator(".react-flow__node")
        .first()
        .evaluate((n) => {
          const box = n.firstElementChild as HTMLElement;
          const probe = document.createElement("div");
          probe.style.background = "var(--chakra-colors-bg-panel)";
          document.body.append(probe);
          const expected = getComputedStyle(probe).backgroundColor;
          probe.remove();
          return [getComputedStyle(box).backgroundColor, expected];
        });
      expect(card).toBe(panel);
      await graph.scrollIntoViewIfNeeded();
      await page.waitForTimeout(300);
      await shot(page, `template-gallery-${theme}`, page.getByRole("dialog"));
    });

    test(`operator templates can be inserted from the gallery (${theme})`, async ({ page }) => {
      await open(page, theme);
      const before = await page.locator(".react-flow__node").count();
      await page.getByRole("button", { name: "File", exact: true }).click();
      await page.getByRole("menuitem", { name: /New from template/ }).click();
      const dialog = page.getByRole("dialog");
      await dialog.locator('[data-part="item"]', { hasText: "Operator template" }).click();
      await expect(dialog.getByRole("button", { name: /Daily ETL/ })).toHaveCount(0);
      await dialog.getByRole("button", { name: /Notify the team/ }).click();
      await expect(dialog.getByText("Locked: retries")).toBeVisible();
      await expect(dialog.getByRole("img", { name: /Graph preview of Notify the team/ })).toBeVisible();
      await shot(page, `template-gallery-operator-${theme}`, dialog);
      await dialog.getByRole("button", { name: "Insert into canvas" }).click();
      await expect(dialog).toBeHidden();
      await expect(page.locator(".react-flow__node")).toHaveCount(before + 1);
      await expect(page.locator(".react-flow__node").filter({ hasText: "notify" })).toBeVisible();
    });
  }
});
