// @vitest-environment jsdom
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { client as apiClient } from "../../api/gen/client.gen";
import type { DagSpec, SourcePlan } from "../../api/gen/types.gen";
import { useBuilder } from "../../store/builder";
import { metaFixture, renderWithProviders } from "../../test/render";
import { useLifecycle } from "../store";
import SourcePlanDialog from "./SourcePlanDialog";

const HAND = 'from airflow.sdk import DAG\n\nwith DAG("hand", schedule=None):\n    pass\n';
const SPEC: DagSpec = { dag_id: "hand", nodes: [], edges: [], source: { origin: "python", code: HAND } } as DagSpec;

const BLOCKED: SourcePlan = {
  applicable: true,
  policy: "generate",
  action: "blocked",
  target_state: "ejected",
  target_file: "/dags/hand.py",
  options: ["preserve", "overwrite", "raw"],
  refusals: [],
  warnings: [],
  operations: [],
  diff: "",
  message: "/dags/hand.py was changed outside AutoAir",
  current_code: HAND,
};

const REFUSED: SourcePlan = {
  ...BLOCKED,
  policy: "strict-preserve",
  action: "refused",
  target_state: "clean",
  options: ["raw", "generate"],
  refusals: [{ code: "unmappable", message: "task x is created in a loop" }],
};

function stubFetch() {
  apiClient.setConfig({ baseUrl: "http://autoair.test/api", throwOnError: true });
  const requests: Request[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (req: Request) => {
      requests.push(req);
      if (req.url.includes("source-preview")) return Response.json({ ...BLOCKED, action: "preserve", diff: "" });
      return Response.json({
        deployed: true,
        status: "deployed",
        report: { ok: true, problems: [] },
        etag: "e2",
        message: "written",
      });
    }),
  );
  return requests;
}

const FORCE_ON = metaFixture({ allow_force_overwrite: true });

describe("SourcePlanDialog", () => {
  beforeEach(() => {
    useBuilder.getState().load(SPEC, true, { etag: "e1" });
    useLifecycle.getState().openDialog({ kind: "source", plan: BLOCKED, request: {}, spec: SPEC });
  });
  afterEach(() => vi.unstubAllGlobals());

  it("applies the change to an ejected file with preserve", async () => {
    const requests = stubFetch();
    renderWithProviders(<SourcePlanDialog plan={BLOCKED} request={{}} spec={SPEC} />);
    expect(screen.getByText("The DAG file was changed outside AutoAir")).toBeTruthy();
    fireEvent.click(screen.getByTestId("source-apply-preserve"));
    await waitFor(() => expect(requests.some((r) => r.method === "PUT")).toBe(true));
    const put = requests.find((r) => r.method === "PUT");
    if (!put) throw new Error("no deploy");
    expect(JSON.parse(await put.clone().text())).toMatchObject({ source_resolution: "preserve" });
  });

  it("hides the overwrite while the server does not allow it (preserve.allow_force_overwrite)", () => {
    stubFetch();
    renderWithProviders(<SourcePlanDialog plan={BLOCKED} request={{}} spec={SPEC} />);
    expect(screen.queryByTestId("source-overwrite")).toBeNull();
    expect(screen.queryByTestId("source-confirm-overwrite")).toBeNull();
    expect(screen.getByTestId("source-apply-preserve")).toBeTruthy();
  });

  it("overwrites only after the explicit confirmation", async () => {
    const requests = stubFetch();
    renderWithProviders(<SourcePlanDialog plan={BLOCKED} request={{}} spec={SPEC} />, { meta: FORCE_ON });
    const overwrite = screen.getByTestId("source-overwrite") as HTMLButtonElement;
    expect(overwrite.disabled).toBe(true);
    const confirm = screen.getByText(/I understand: the changes made outside AutoAir/).closest("label");
    if (!confirm) throw new Error("no confirmation");
    fireEvent.click(confirm);
    await waitFor(() => expect(overwrite.disabled).toBe(false));
    fireEvent.click(overwrite);
    await waitFor(() => expect(requests.some((r) => r.method === "PUT")).toBe(true));
    const put = requests.find((r) => r.method === "PUT");
    if (!put) throw new Error("no deploy");
    expect(JSON.parse(await put.clone().text())).toMatchObject({ source_resolution: "overwrite" });
  });

  it("offers raw code or a deliberate switch to generated code for a refused patch", () => {
    stubFetch();
    const view = renderWithProviders(<SourcePlanDialog plan={REFUSED} request={{}} spec={SPEC} />);
    expect(screen.getByText("This change cannot be applied to the existing file")).toBeTruthy();
    expect(screen.getByTestId("source-refusals").textContent).toContain("created in a loop");
    expect(screen.queryByTestId("source-apply-preserve")).toBeNull();
    fireEvent.click(screen.getByTestId("source-generate"));
    expect(useBuilder.getState().dag.source?.policy).toBe("generate");
    expect(useLifecycle.getState().dialog?.kind).toBe("deploy");
    view.unmount();

    renderWithProviders(<SourcePlanDialog plan={REFUSED} request={{}} spec={SPEC} />);
    fireEvent.click(screen.getByTestId("source-keep-raw"));
    expect(useBuilder.getState().dag.raw_code).toBe(HAND);
    expect(useBuilder.getState().nodes).toHaveLength(0);
  });

  it("titles a blocked generate of the kept hand-written file by what it is", () => {
    stubFetch();
    const plan: SourcePlan = { ...BLOCKED, target_state: "hand_written", options: ["preserve", "raw"] };
    renderWithProviders(<SourcePlanDialog plan={plan} request={{}} spec={SPEC} />);
    expect(screen.getByText("Generating code would replace the hand-written DAG file")).toBeTruthy();
    expect(screen.getByText(/keeps this DAG's hand-written file and never regenerates it/)).toBeTruthy();
    expect(screen.queryByText(/changed outside AutoAir/)).toBeNull();
    expect(screen.getByTestId("source-overwrite-permission").textContent).toContain("preserve.allow_force_overwrite");
    expect(screen.queryByTestId("source-overwrite")).toBeNull();
  });

  it("asks to confirm replacing a kept hand-written file in its own words", () => {
    stubFetch();
    const plan: SourcePlan = { ...BLOCKED, target_state: "hand_written" };
    renderWithProviders(<SourcePlanDialog plan={plan} request={{}} spec={SPEC} />, { meta: FORCE_ON });
    expect(screen.getByText("I understand: the hand-written file is replaced by generated code.")).toBeTruthy();
    expect(screen.queryByTestId("source-overwrite-permission")).toBeNull();
  });

  it("titles a file AutoAir did not create as such", () => {
    stubFetch();
    const plan: SourcePlan = { ...BLOCKED, target_state: "foreign", options: ["preserve", "raw"] };
    renderWithProviders(<SourcePlanDialog plan={plan} request={{}} spec={SPEC} />);
    expect(screen.getByText("The DAG file was not created by AutoAir")).toBeTruthy();
  });

  it("follows the user's permissions for raw code and overwriting", () => {
    stubFetch();
    const meta = metaFixture({ allow_force_overwrite: true, iam: { permissions: ["spec:write", "deploy"] } });
    renderWithProviders(<SourcePlanDialog plan={BLOCKED} request={{}} spec={SPEC} />, { meta });
    const raw = screen.getByTestId("source-keep-raw") as HTMLButtonElement;
    expect(raw.disabled).toBe(true);
    expect(raw.title).toContain("code:raw");
    fireEvent.click(raw);
    expect(useBuilder.getState().dag.raw_code ?? null).toBeNull();
    expect(screen.queryByTestId("source-overwrite")).toBeNull();
    expect(screen.queryByTestId("source-confirm-overwrite")).toBeNull();
  });

  it("offers raw code and overwriting to users who hold the permissions", () => {
    stubFetch();
    const meta = metaFixture({
      allow_force_overwrite: true,
      iam: { permissions: ["spec:write", "deploy", "code:raw", "deploy:overwrite_ejected"] },
    });
    renderWithProviders(<SourcePlanDialog plan={BLOCKED} request={{}} spec={SPEC} />, { meta });
    expect((screen.getByTestId("source-keep-raw") as HTMLButtonElement).disabled).toBe(false);
    expect(screen.getByTestId("source-overwrite")).toBeTruthy();
  });

  it("hides raw code where the deployment switched raw code DAGs off", () => {
    stubFetch();
    renderWithProviders(<SourcePlanDialog plan={BLOCKED} request={{}} spec={SPEC} />, {
      meta: metaFixture({ features: { code_editor: false } as never }),
    });
    expect(screen.queryByTestId("source-keep-raw")).toBeNull();
  });
});
