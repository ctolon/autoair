// @vitest-environment jsdom
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { client as apiClient } from "../../api/gen/client.gen";
import type { DagSpec, SourcePlan } from "../../api/gen/types.gen";
import { flushHistory, useBuilder } from "../../store/builder";
import { renderWithProviders } from "../../test/render";
import { useLifecycle } from "../store";
import DeployDialog from "./DeployDialog";

const HAND = 'from airflow.sdk import DAG\n\nwith DAG("hand", schedule=None):\n    pass\n';
const SPEC = { dag_id: "hand", nodes: [], edges: [], source: { origin: "python", code: HAND } } as DagSpec;

function plan(policy: string): SourcePlan {
  return {
    applicable: true,
    policy: policy as SourcePlan["policy"],
    action: policy === "generate" ? "blocked" : "preserve",
    target_state: "hand_written",
    target_file: "/dags/hand.py",
    options: policy === "generate" ? ["preserve", "raw"] : [],
    refusals: [],
    warnings: [],
    operations: [],
    diff: "",
    target_sha: "abc",
  };
}

function stubFetch() {
  apiClient.setConfig({ baseUrl: "http://autoair.test/api", throwOnError: true });
  const previews: unknown[] = [];
  const deploys: unknown[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (req: Request) => {
      if (req.url.includes("source-preview")) {
        const body = (await req.json()) as { spec: DagSpec };
        previews.push(body.spec.source?.policy ?? null);
        return Response.json(plan(body.spec.source?.policy === "generate" ? "generate" : "preserve"));
      }
      if (req.method === "PUT") {
        deploys.push(((await req.json()) as { spec: DagSpec }).spec.source?.policy ?? null);
        return Response.json({
          deployed: true,
          status: "deployed",
          report: { ok: true, problems: [] },
          etag: "e2",
          message: "written",
        });
      }
      return Response.json([]);
    }),
  );
  return { previews, deploys };
}

const policySelect = () => screen.getByLabelText(/Source policy/) as HTMLSelectElement;

describe("DeployDialog source policy", () => {
  beforeEach(() => {
    useBuilder.getState().load(SPEC, true, { etag: "e1" });
    useLifecycle.getState().openDialog({ kind: "deploy", focus: "message" });
  });
  afterEach(() => vi.unstubAllGlobals());

  it("previews a chosen policy without changing the document, and Cancel drops it", async () => {
    const { previews } = stubFetch();
    const view = renderWithProviders(<DeployDialog focus="message" />);
    fireEvent.change(policySelect(), { target: { value: "generate" } });
    expect(policySelect().value).toBe("generate");
    expect(screen.getByTestId("deploy-policy-draft").textContent).toContain("when you deploy");
    expect(useBuilder.getState().dag.source?.policy ?? "auto").toBe("auto");
    await waitFor(() => expect(previews).toContain("generate"), { timeout: 3000 });
    await waitFor(() => expect(screen.getByTestId("source-plan").dataset.action).toBe("blocked"));
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(useLifecycle.getState().dialog).toBeNull();
    view.unmount();
    expect(useBuilder.getState().dag.source?.policy ?? "auto").toBe("auto");
    expect(useBuilder.getState().dag.source?.code).toBe(HAND);
  });

  it("applies the chosen policy on Deploy as one undoable step", async () => {
    const { deploys } = stubFetch();
    renderWithProviders(<DeployDialog focus="message" />);
    fireEvent.change(policySelect(), { target: { value: "strict-preserve" } });
    fireEvent.click(screen.getByRole("button", { name: /Deploy$/ }));
    await waitFor(() => expect(deploys).toEqual(["strict-preserve"]));
    expect(useBuilder.getState().dag.source?.policy).toBe("strict-preserve");
    flushHistory();
    useBuilder.temporal.getState().undo();
    expect(useBuilder.getState().dag.source?.policy ?? "auto").toBe("auto");
  });
});
