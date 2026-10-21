import { type Page, expect, test } from "@playwright/test";

/**
 * common.ai provider tasks (LLMOperator, AgentOperator, @task.llm): the dedicated LLM editor
 * (structured output builder, toolsets, approval) produces the expected generated code, and
 * half-typed / invalid input never breaks the inspector. Light and dark screenshots.
 *
 * Needs a server whose catalog includes apache-airflow-providers-common-ai, e.g.
 *   .venv-allproviders/bin/autoair local --port 8855 + `vite preview` with AUTOAIR_API_PORT=8855.
 *   AUTOAIR_DEV_URL   UI base URL (default http://localhost:5173)
 *   AUTOAIR_AI_OPS_SHOTS  folder for screenshots (optional)
 */
const UI = process.env.AUTOAIR_DEV_URL ?? "http://localhost:5173";
const SHOTS = process.env.AUTOAIR_AI_OPS_SHOTS;

async function open(page: Page, mode: "light" | "dark", errors: string[]) {
  page.on("pageerror", (e) => errors.push(`pageerror: ${e.message}`));
  await page.addInitScript((m) => {
    try {
      localStorage.setItem("autoair.lang", "en");
      localStorage.setItem("autoair.colorMode", m);
    } catch {
      /* about:blank */
    }
  }, mode);
  await page.goto(`${UI}/`);
  await expect(page.getByPlaceholder(/Search operators/)).toBeVisible({ timeout: 30_000 });
}

async function add(page: Page, name: string) {
  await page.getByPlaceholder(/Search operators/).fill(name);
  await page
    .locator('[role="option"]')
    .filter({ has: page.getByText(name, { exact: true }) })
    .first()
    .dblclick();
  await page.locator(".react-flow__node").last().dispatchEvent("click");
}

function trackCode(page: Page): () => string {
  let latest = "";
  page.on("response", async (resp) => {
    if (!resp.url().endsWith("/api/codegen") || !resp.ok()) return;
    const body = (await resp.json().catch(() => null)) as { code?: string | null } | null;
    if (body?.code) latest = body.code;
  });
  return () => latest;
}

test("LLM and agent tasks: structured output, toolsets, approval", async ({ page, request }) => {
  const decorators = await request
    .get(`${UI}/${process.env.AUTOAIR_API_PREFIX ?? "api"}/catalog/decorators`)
    .catch(() => null);
  const names = decorators?.ok() ? ((await decorators.json()) as { name: string }[]).map((d) => d.name) : [];
  test.skip(!names.includes("agent"), "apache-airflow-providers-common-ai is not in the catalog");

  for (const mode of ["light", "dark"] as const) {
    const errors: string[] = [];
    await page.context().clearCookies();
    await open(page, mode, errors);
    const code = trackCode(page);

    await add(page, "LLMOperator");
    const editor = page.getByTestId("llm-editor");
    await expect(editor).toBeVisible();
    await page.getByRole("combobox", { name: /^Output/ }).selectOption("model");
    const className = page.getByRole("textbox", { name: /Class name/ });
    // Invalid live input: the panel keeps working and explains the problem.
    await className.fill("not a class");
    await expect(page.getByText(/Use a Python class name/).first()).toBeVisible();
    await className.fill("Sentiment");
    await page.getByRole("textbox", { name: "Field 1" }).fill("label");
    await page.getByRole("button", { name: /Add field/ }).click();
    await page.getByRole("textbox", { name: "Field 2" }).fill("1bad");
    await page.getByRole("textbox", { name: "Field 2" }).fill("score");
    await page.getByRole("combobox", { name: "Type 2" }).selectOption("float");
    await expect(page.getByTestId("output-model-preview")).toContainText("score: float");
    await page.getByText("Require human approval").click();
    await expect(page.getByRole("spinbutton", { name: /Approval timeout/ })).toBeVisible();
    if (SHOTS) await editor.screenshot({ path: `${SHOTS}/llm-operator-${mode}.png` });

    await add(page, "AgentOperator");
    await page.getByRole("button", { name: /SQL toolset/ }).click();
    await page.getByRole("button", { name: /MCP toolset/ }).click();
    await expect(page.getByTestId("toolset-1")).toContainText("MCPToolset");
    await page.getByTestId("toolset-0").getByRole("combobox").first().fill("warehouse");
    await page.getByTestId("toolset-1").getByRole("combobox").first().fill("docs_mcp");
    await page.getByText("Iterative human review").click();
    if (SHOTS) await page.getByTestId("llm-editor").screenshot({ path: `${SHOTS}/agent-operator-${mode}.png` });

    await add(page, "@task.agent");
    await page.getByRole("combobox", { name: /LLM connection/ }).fill("pydanticai_default");
    await expect(page.getByTestId("llm-editor")).toContainText("Toolsets");
    if (SHOTS) await page.getByTestId("llm-editor").screenshot({ path: `${SHOTS}/task-agent-${mode}.png` });

    await page.locator(".react-flow__pane").click({ position: { x: 5, y: 5 } }); // deselect: one "Code" tab
    await page.getByRole("tab", { name: "Code" }).click();
    await expect.poll(code, { timeout: 20_000 }).toContain("class Sentiment(BaseModel):");
    await expect.poll(code, { timeout: 20_000 }).toContain("output_type=Sentiment");
    await expect.poll(code, { timeout: 20_000 }).toContain('SQLToolset(db_conn_id="warehouse")');
    await expect.poll(code, { timeout: 20_000 }).toContain('MCPToolset(mcp_conn_id="docs_mcp")');
    await expect.poll(code, { timeout: 20_000 }).toContain("enable_hitl_review=True");
    await expect
      .poll(code, { timeout: 20_000 })
      .toMatch(/@task\.agent\(task_id="agent_\d+", llm_conn_id="pydanticai_default"\)/);
    expect(errors).toEqual([]);
  }
});
