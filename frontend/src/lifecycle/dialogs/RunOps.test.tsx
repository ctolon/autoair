// @vitest-environment jsdom
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  getAirflowCapabilitiesQueryKey,
  getDagStatusQueryKey,
  listDagBundlesQueryKey,
  listImportErrorsQueryKey,
} from "../../api/gen/@tanstack/react-query.gen";
import { client as apiClient } from "../../api/gen/client.gen";
import type { AirflowCapabilities } from "../../api/gen/types.gen";
import { useBuilder } from "../../store/builder";
import { renderWithProviders, testQueryClient } from "../../test/render";
import { NO_CAPABILITIES } from "../capabilities";
import AirflowTab from "../panels/AirflowTab";
import BackfillDialog from "./BackfillDialog";
import ClearRunDialog from "./ClearRunDialog";

const ALL: AirflowCapabilities = {
  version: "3.4.0",
  source: "server",
  materialize: true,
  clear_run_on_latest_version: true,
  clear_only_new: true,
  keep_task_state: true,
  backfill_run_on_latest_version: true,
  scheduling_state: true,
};

function clientWith(caps: AirflowCapabilities) {
  const client = testQueryClient();
  client.setQueryData(getAirflowCapabilitiesQueryKey(), caps);
  return client;
}

function stubFetch() {
  const fetch = vi.fn(async (_req: Request) => Response.json({ task_instances: [], total_entries: 0 }));
  vi.stubGlobal("fetch", fetch);
  apiClient.setConfig({ baseUrl: "http://autoair.test/api", throwOnError: true });
  return fetch;
}

describe("run operations", () => {
  beforeEach(() => useBuilder.setState({ savedDagId: "etl" }));
  afterEach(() => vi.unstubAllGlobals());

  it("hides clear options the Airflow version lacks", () => {
    renderWithProviders(<ClearRunDialog runId="r1" />, { client: clientWith(NO_CAPABILITIES) });
    expect(screen.getByText("Only failed tasks")).toBeTruthy();
    expect(screen.queryByText("Only new tasks")).toBeNull();
    expect(screen.queryByText("Keep task state")).toBeNull();
    expect(screen.queryByText("DAG version")).toBeNull();
  });

  it("sends the 3.4 clear options when chosen", async () => {
    const fetch = stubFetch();
    renderWithProviders(<ClearRunDialog runId="r1" />, { client: clientWith(ALL) });
    const toggle = screen.getByText("Keep task state").closest("label");
    if (toggle) fireEvent.click(toggle);
    await waitFor(() =>
      expect((screen.getByRole("checkbox", { name: "Keep task state" }) as HTMLInputElement).checked).toBe(true),
    );
    fireEvent.change(screen.getByRole("combobox"), { target: { value: "latest" } });
    fireEvent.click(screen.getByRole("button", { name: /Preview/ }));
    await waitFor(() => expect(fetch).toHaveBeenCalled());
    expect(await fetch.mock.calls[0]?.[0].json()).toEqual({
      only_failed: true,
      dry_run: true,
      run_on_latest_version: true,
      keep_task_state: true,
    });
  });

  it("offers drain and latest version in the backfill only when supported", () => {
    renderWithProviders(<BackfillDialog />, { client: clientWith(NO_CAPABILITIES) });
    expect(screen.queryByText("Drain the DAG")).toBeNull();
    expect(screen.queryByText("DAG version")).toBeNull();
  });

  it("shows the backfill 3.4 options when supported", () => {
    renderWithProviders(<BackfillDialog />, { client: clientWith(ALL) });
    expect(screen.getByText("Drain the DAG")).toBeTruthy();
    expect(screen.getByText("DAG version")).toBeTruthy();
  });

  it.each([
    [NO_CAPABILITIES, false],
    [ALL, true],
  ])("shows the scheduling state and trigger endpoint of a deployed DAG", (caps, scheduling) => {
    const client = clientWith(caps);
    client.setQueryData(getDagStatusQueryKey({ path: { dag_id: "etl" } }), {
      dag_id: "etl",
      exists: true,
      scheduling_state: scheduling ? "active" : null,
      latest_runs: [],
    });
    client.setQueryData(listImportErrorsQueryKey({ query: { filename: "etl.py" } }), []);
    client.setQueryData(listDagBundlesQueryKey(), []);
    renderWithProviders(<AirflowTab />, { client });
    expect(!!screen.queryByTestId("scheduling-state")).toBe(scheduling);
    expect(screen.getByTestId("trigger-endpoint")).toBeTruthy();
    expect(screen.getByTestId("trigger-snippet").textContent).toContain("/api/v2/dags/etl/dagRuns");
  });
});
