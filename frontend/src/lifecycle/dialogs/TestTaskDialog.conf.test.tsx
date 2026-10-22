// @vitest-environment jsdom
import { fireEvent, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { client as apiClient } from "../../api/gen/client.gen";
import type { DagSpec } from "../../api/gen/types.gen";
import { useBuilder } from "../../store/builder";
import { metaFixture, renderWithProviders } from "../../test/render";
import TestTaskDialog from "./TestTaskDialog";

// A plain textarea stands in for CodeMirror (see widgets/fields.test.tsx).
vi.mock("../../widgets/code", () => ({
  CodeEditor: ({
    value,
    onChange,
    ariaLabel,
  }: {
    value: string;
    onChange: (v: string) => void;
    ariaLabel?: string;
  }) => <textarea aria-label={ariaLabel} value={value} onChange={(e) => onChange(e.target.value)} />,
}));

const SPEC = {
  dag_id: "d",
  nodes: [{ kind: "python_task", id: "t", task_id: "t", function_name: "t", code: "return 1" }],
  edges: [],
} as unknown as DagSpec;

function stubFetch() {
  apiClient.setConfig({ baseUrl: "http://autoair.test/api", throwOnError: true });
  const sent: { url: string; conf: unknown }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (req: Request) => {
      if (req.url.includes("/runner/")) {
        sent.push({ url: req.url, conf: ((await req.json()) as { conf: unknown }).conf });
        return Response.json({ state: "success", mode: "memory", tasks: [], log: "", task_id: "t" });
      }
      return Response.json([]);
    }),
  );
  return sent;
}

const confBox = () => screen.getByLabelText("conf / params (JSON)") as HTMLTextAreaElement;
const runButton = (name: RegExp) => screen.getByRole("button", { name }) as HTMLButtonElement;

describe("test run conf", () => {
  beforeEach(() => useBuilder.getState().load(SPEC, true, { etag: "e1" }));
  afterEach(() => vi.unstubAllGlobals());

  it.each([
    ["dag", /Run DAG/],
    ["task", /Run task/],
  ] as const)("disables %s runs while the conf JSON is invalid", async (tab, name) => {
    const sent = stubFetch();
    renderWithProviders(<TestTaskDialog nodeId="t" whole={tab === "dag"} />, {
      meta: metaFixture({ test_runs: { on_validator: false, db_mode_unavailable: null } }),
    });
    expect(runButton(name).disabled).toBe(false);

    fireEvent.change(confBox(), { target: { value: '{"who": ' } });
    expect(runButton(name).disabled).toBe(true);
    expect(screen.getByText(/Not valid JSON/)).toBeTruthy();
    expect(screen.getByRole("alert").textContent).toMatch(/conf JSON is not valid/);
    fireEvent.click(runButton(name));

    fireEvent.change(confBox(), { target: { value: "[1]" } }); // valid JSON, not an object
    expect(runButton(name).disabled).toBe(true);

    fireEvent.change(confBox(), { target: { value: '{"who": "x"}' } });
    expect(runButton(name).disabled).toBe(false);
    expect(screen.queryByRole("alert")).toBeNull();
    fireEvent.click(runButton(name));
    await vi.waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0]?.conf).toEqual({ who: "x" });
  });
});

describe("isolation notice", () => {
  beforeEach(() => useBuilder.getState().load(SPEC, true, { etag: "e1" }));
  afterEach(() => vi.unstubAllGlobals());

  it.each([
    [{ where: "local", process_isolated: false }, true],
    [{ where: "local", process_isolated: true }, false],
    [{ where: "validator", process_isolated: null }, false],
  ] as const)("result %o shows the notice: %s", async (fields, shown) => {
    apiClient.setConfig({ baseUrl: "http://autoair.test/api", throwOnError: true });
    vi.stubGlobal(
      "fetch",
      vi.fn(async (req: Request) =>
        req.url.includes("test-dag")
          ? Response.json({ state: "success", mode: "memory", tasks: [], log: "", ...fields })
          : Response.json([]),
      ),
    );
    renderWithProviders(<TestTaskDialog nodeId={null} whole />, {
      meta: metaFixture({ test_runs: { on_validator: false, db_mode_unavailable: null } }),
    });
    fireEvent.click(runButton(/Run DAG/));
    await screen.findByText("success");
    expect(!!screen.queryByTestId("test-run-isolation-notice")).toBe(shown);
    if (shown) expect(screen.getByText(/validator\.url/)).toBeTruthy();
    // the run is not blocked: the button stays usable
    expect(runButton(/Run DAG/).disabled).toBe(false);
  });
});
