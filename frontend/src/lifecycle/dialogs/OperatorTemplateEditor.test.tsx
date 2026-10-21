// @vitest-environment jsdom
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  getOperatorQueryKey,
  getTemplateQueryKey,
  listOperatorsQueryKey,
  listTemplatesQueryKey,
} from "../../api/gen/@tanstack/react-query.gen";
import { client as apiClient } from "../../api/gen/client.gen";
import type { Template } from "../../api/gen/types.gen";
import { renderWithProviders, testQueryClient } from "../../test/render";
import { useLifecycle } from "../store";
import OperatorTemplateEditor from "./OperatorTemplateEditor";

const BASH = "airflow.providers.standard.operators.bash.BashOperator";

const TEMPLATE: Template = {
  id: "notify",
  title: "Notify",
  kind: "operator",
  builtin: false,
  version: "1",
  etag: "etag-1",
  blueprint: {
    node: { id: "template", kind: "operator", task_id: "notify", operator: BASH, params: { bash_command: "echo" } },
    locked: ["params.bash_command"],
    required: [],
  },
};

function setup(template: Template, conflict = false) {
  const requests: { url: string; method: string; headers: Headers; body: string }[] = [];
  const fetch = vi.fn(async (req: Request) => {
    requests.push({ url: req.url, method: req.method, headers: req.headers, body: await req.clone().text() });
    if (req.url.endsWith("/validate?import_check=false")) return Response.json({ ok: true, problems: [] });
    if (req.url.endsWith("/codegen")) return Response.json({ code: "BashOperator(task_id='notify')", problems: [] });
    if (req.url.endsWith("/templates") && req.method === "POST") {
      if (conflict)
        return Response.json(
          { detail: { message: "changed", current_version: "2", current_etag: "etag-2", updated_by: "bob" } },
          { status: 409 },
        );
      return Response.json({ ...template, version: "2", etag: "etag-2" });
    }
    if (req.url.endsWith(`/templates/${template.id}`)) return Response.json(template);
    return Response.json([]);
  });
  vi.stubGlobal("fetch", fetch);
  apiClient.setConfig({ baseUrl: "http://autoair.test/api", throwOnError: true });
  const client = testQueryClient();
  client.setQueryData(getTemplateQueryKey({ path: { template_id: template.id } }), template);
  client.setQueryData(listTemplatesQueryKey(), [template]);
  client.setQueryData(listOperatorsQueryKey(), []);
  client.setQueryData(getOperatorQueryKey({ path: { fqn: BASH } }), {
    fqn: BASH,
    class_name: "BashOperator",
    module: "airflow.providers.standard.operators.bash",
    provider: "apache-airflow-providers-standard",
    category: "operator",
    params_schema: { type: "object", properties: { bash_command: { type: "string" } }, required: ["bash_command"] },
  });
  renderWithProviders(<OperatorTemplateEditor mode="edit" templateId={template.id} />, { client });
  return requests;
}

describe("OperatorTemplateEditor", () => {
  beforeEach(() =>
    useLifecycle.getState().openDialog({ kind: "operatorTemplateEditor", mode: "edit", templateId: "notify" }),
  );
  afterEach(() => vi.unstubAllGlobals());

  it("publishes the next version with If-Match", async () => {
    const requests = setup(TEMPLATE);
    expect(await screen.findByText("Edit operator template “Notify”")).toBeTruthy();
    expect(screen.getByTestId("optpl-operator").textContent).toBe(BASH);
    const save = screen.getByTestId("optpl-save");
    await waitFor(() => expect((save as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(save);
    await waitFor(() => expect(requests.some((r) => r.method === "POST")).toBe(true));
    const post = requests.find((r) => r.method === "POST");
    expect(post?.headers.get("if-match")).toBe("etag-1");
    const body = JSON.parse(post?.body ?? "{}") as Template;
    expect(body.blueprint?.locked).toEqual(["params.bash_command"]);
    expect(body.version).toBeNull();
  });

  it("explains a conflicting edit instead of failing", async () => {
    setup(TEMPLATE, true);
    const save = await screen.findByTestId("optpl-save");
    await waitFor(() => expect((save as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(save);
    const alert = await screen.findByTestId("optpl-conflict");
    expect(alert.textContent).toContain("bob changed this template since you opened it (now v2)");
    expect(screen.getByText("Reload latest")).toBeTruthy();
  });

  it("renders a malformed stored template without breaking", async () => {
    const odd = {
      ...TEMPLATE,
      tags: undefined,
      blueprint: {
        node: { id: "template", kind: "operator", task_id: "", operator: BASH, params: null },
        locked: ["params.gone", "retries"],
      },
    } as unknown as Template;
    setup(odd);
    expect(await screen.findByText("Edit operator template “Notify”")).toBeTruthy();
    fireEvent.click(screen.getByText("Field modes"));
    expect(await screen.findByText("params.gone")).toBeTruthy();
  });
});
