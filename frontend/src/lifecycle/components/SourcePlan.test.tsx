// @vitest-environment jsdom
import { screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { client as apiClient } from "../../api/gen/client.gen";
import type { DagSpec, SourcePlan } from "../../api/gen/types.gen";
import { metaFixture, renderWithProviders } from "../../test/render";
import { SourcePlanSummary, SourcePreview } from "./SourcePlan";
import { UnifiedDiff } from "./UnifiedDiff";

const DIFF = [
  "--- a/hand.py",
  "+++ b/hand.py",
  "@@ -1,3 +1,3 @@",
  " import pendulum",
  "-    retries=2,  # flaky",
  "+    retries=5,  # flaky",
  " ",
].join("\n");

const PLAN: SourcePlan = {
  applicable: true,
  requested: "auto",
  policy: "preserve",
  action: "preserve",
  target_state: "clean",
  target_file: "/dags/hand.py",
  base: "target",
  diff: DIFF,
  diff_truncated: false,
  operations: ["task orders: changed retries"],
  warnings: ["not mapped (left as written): a task with a computed task_id"],
  refusals: [],
  options: [],
  message: "the change is applied to the existing file",
};

describe("UnifiedDiff", () => {
  it("colours added and removed lines", () => {
    renderWithProviders(<UnifiedDiff diff={DIFF} label="diff" />);
    const region = screen.getByTestId("source-diff");
    expect(region.textContent).toContain("+    retries=5,  # flaky");
    expect(region.querySelectorAll("span")).toHaveLength(7);
  });

  it.each([null, undefined, 42, {}, [], "", "   \n"])("renders %p as no changes", (value) => {
    renderWithProviders(<UnifiedDiff diff={value} label="diff" />);
    expect(screen.getByTestId("source-diff-empty")).toBeTruthy();
  });

  it("says when the diff was cut", () => {
    renderWithProviders(<UnifiedDiff diff={DIFF} truncated label="diff" />);
    expect(screen.getByText(/The diff is cut here/)).toBeTruthy();
  });
});

describe("SourcePlanSummary", () => {
  it("shows the policy, the action, warnings and the diff", () => {
    renderWithProviders(<SourcePlanSummary plan={PLAN} />);
    expect(screen.getByText("Preserve")).toBeTruthy();
    expect(screen.getByText(/patched into the existing file/)).toBeTruthy();
    expect(screen.getByTestId("source-warnings").textContent).toContain("computed task_id");
    expect(screen.getByTestId("source-diff")).toBeTruthy();
  });

  it("lists refusals and hides the diff of a refused plan", () => {
    const refused: SourcePlan = {
      ...PLAN,
      action: "refused",
      refusals: [{ code: "risky", message: "default_args refers to a variable" }],
    };
    renderWithProviders(<SourcePlanSummary plan={refused} />);
    expect(screen.getByTestId("source-refusals").textContent).toContain("default_args refers to a variable");
    expect(screen.getByText("strict")).toBeTruthy();
    expect(screen.queryByTestId("source-diff")).toBeNull();
  });

  const HAND_BLOCKED: SourcePlan = {
    ...PLAN,
    policy: "generate",
    action: "blocked",
    target_state: "hand_written",
    diff: "",
    operations: [],
    warnings: [],
    options: ["preserve", "raw"],
  };

  it("says a kept hand-written file would be replaced, not that it was changed outside AutoAir", () => {
    renderWithProviders(<SourcePlanSummary plan={HAND_BLOCKED} />);
    expect(screen.getByText(/generating code would replace the hand-written file AutoAir keeps/)).toBeTruthy();
    expect(screen.queryByText(/changed outside AutoAir/)).toBeNull();
    expect(screen.getByTestId("source-state").dataset.tone).toBe("warning");
    // Overwrites are off by default: the setting that would allow them is named.
    expect(screen.getByTestId("source-overwrite-permission").textContent).toContain("preserve.allow_force_overwrite");
  });

  it("names the permission when the server allows overwrites", () => {
    renderWithProviders(<SourcePlanSummary plan={HAND_BLOCKED} />, {
      meta: metaFixture({ allow_force_overwrite: true }),
    });
    expect(screen.getByTestId("source-overwrite-permission").textContent).toContain("deploy:overwrite_ejected");
  });

  it("does not name the permission when the overwrite is offered", () => {
    renderWithProviders(<SourcePlanSummary plan={{ ...HAND_BLOCKED, options: ["preserve", "overwrite", "raw"] }} />);
    expect(screen.queryByTestId("source-overwrite-permission")).toBeNull();
  });

  it("shows the kept hand-written file as information in a normal preserve deploy", () => {
    renderWithProviders(<SourcePlanSummary plan={{ ...PLAN, target_state: "hand_written" }} />);
    const state = screen.getByTestId("source-state");
    expect(state.dataset.tone).toBe("info");
    expect(state.textContent).toBe("/dags/hand.py is the hand-written file AutoAir keeps for this DAG.");
    expect(screen.queryByTestId("source-overwrite-permission")).toBeNull();
  });

  it.each([
    ["ejected", /Stopped: the DAG file was changed outside AutoAir/],
    ["foreign", /Stopped: the DAG file is a hand-written file AutoAir did not create/],
    ["something-new", /Stopped: the existing DAG file is not replaced without your choice/],
  ])("words a blocked plan by its target state (%s)", (state, text) => {
    renderWithProviders(<SourcePlanSummary plan={{ ...HAND_BLOCKED, target_state: state } as SourcePlan} />);
    expect(screen.getByText(text)).toBeTruthy();
    expect(screen.queryByTestId("source-overwrite-permission")).toBeNull();
  });

  it("never breaks on a malformed plan", () => {
    const junk = {
      action: "something-new",
      target_state: 7,
      warnings: [1, null, "ok"],
      operations: "nope",
      refusals: [null, { code: "brand-new", message: "x" }, { code: 5, message: { nested: true } }],
      options: "overwrite",
      target_file: 12,
      policy: "future",
      diff: { not: "a string" },
    } as unknown as SourcePlan;
    renderWithProviders(<SourcePlanSummary plan={junk} />);
    expect(screen.getByTestId("source-plan")).toBeTruthy();
    expect(screen.getByText("brand-new")).toBeTruthy();
    renderWithProviders(<SourcePlanSummary plan={null} />);
    renderWithProviders(<SourcePlanSummary plan={{ ...junk, action: "blocked", target_state: "hand_written" }} />);
    expect(screen.getByTestId("source-overwrite-permission")).toBeTruthy();
  });
});

describe("SourcePreview", () => {
  afterEach(() => vi.unstubAllGlobals());
  const spec: DagSpec = { dag_id: "hand", nodes: [], edges: [] } as DagSpec;

  it("asks the server for the plan of the document", async () => {
    apiClient.setConfig({ baseUrl: "http://autoair.test/api", throwOnError: true });
    const fetch = vi.fn(async (req: Request) => {
      expect(req.url).toContain("/specs/hand/source-preview");
      expect(JSON.parse(await req.text())).toMatchObject({ spec: { dag_id: "hand" }, environment: null });
      return Response.json(PLAN);
    });
    vi.stubGlobal("fetch", fetch);
    renderWithProviders(<SourcePreview spec={spec} environment={null} />);
    expect(screen.getByTestId("source-preview-loading")).toBeTruthy();
    await waitFor(() => expect(screen.getByTestId("source-diff")).toBeTruthy());
  });

  it("shows a failed preview inline", async () => {
    apiClient.setConfig({ baseUrl: "http://autoair.test/api", throwOnError: true });
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => Response.json({ detail: "needs code:execute" }, { status: 403 })),
    );
    renderWithProviders(<SourcePreview spec={spec} environment={null} />);
    await waitFor(() => expect(screen.getByTestId("source-preview-error")).toBeTruthy());
  });

  it("does not ask for loader or YAML DAGs", () => {
    const fetch = vi.fn();
    vi.stubGlobal("fetch", fetch);
    renderWithProviders(<SourcePreview spec={{ ...spec, output_mode: "yaml" }} environment={null} />);
    expect(screen.getByText(/does not apply here/)).toBeTruthy();
    expect(fetch).not.toHaveBeenCalled();
  });
});
