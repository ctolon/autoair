// @vitest-environment jsdom
import { screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { client as apiClient } from "../../api/gen/client.gen";
import type { DagSpec } from "../../api/gen/types.gen";
import { useBuilder } from "../../store/builder";
import { renderWithProviders } from "../../test/render";
import CodeTab from "./CodeTab";

// CodeMirror is not needed to check what the view says about the file.
vi.mock("../components/CodeView", () => ({
  default: ({ value }: { value: string }) => <pre data-testid="code-view">{value}</pre>,
}));

const HAND = 'from airflow.sdk import DAG\n\n# tuned by hand\nwith DAG("hand", schedule=None):\n    pass\n';
const LIVE = { code: "# generated\n", checking: false, error: null };

function stubPreview(body: unknown) {
  apiClient.setConfig({ baseUrl: "http://autoair.test/api", throwOnError: true });
  const fetch = vi.fn(async () => Response.json(body));
  vi.stubGlobal("fetch", fetch);
  return fetch;
}

describe("CodeTab Python caption", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("says the generated file is generated", () => {
    const fetch = stubPreview({});
    useBuilder.getState().load({ dag_id: "gen", nodes: [], edges: [] } as DagSpec, true, { etag: "e1" });
    renderWithProviders(<CodeTab live={LIVE} />);
    expect(screen.getByTestId("python-caption").textContent).toBe("Generated DAG file (read-only).");
    expect(fetch).not.toHaveBeenCalled();
  });

  it("names the kept hand-written file as such", async () => {
    stubPreview({
      applicable: true,
      policy: "preserve",
      action: "unchanged",
      target_state: "hand_written",
      content: HAND,
    });
    const spec = { dag_id: "hand", nodes: [], edges: [], source: { origin: "python", code: HAND } } as DagSpec;
    useBuilder.getState().load(spec, true, { etag: "e1" });
    renderWithProviders(<CodeTab live={LIVE} />);
    await waitFor(() => expect(screen.getByTestId("code-view").textContent).toContain("# tuned by hand"), {
      timeout: 3000,
    });
    expect(screen.getByTestId("python-caption").textContent).toBe(
      "DAG file kept by AutoAir (hand-written, read-only).",
    );
  });

  it("says why a kept file cannot be shown and never calls generated code the kept file", async () => {
    stubPreview({ action: "refused", content: 7, message: "cannot apply" });
    const spec = { dag_id: "hand", nodes: [], edges: [], source: { origin: "python", code: HAND } } as DagSpec;
    useBuilder.getState().load(spec, true, { etag: "e1" });
    renderWithProviders(<CodeTab live={LIVE} />);
    await waitFor(() => expect(screen.getByText("cannot apply")).toBeTruthy(), { timeout: 3000 });
    expect(screen.queryByText(/kept by AutoAir/)).toBeNull();
  });
});
