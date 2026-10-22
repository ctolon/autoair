// @vitest-environment jsdom
import { fireEvent, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { client as apiClient } from "../../api/gen/client.gen";
import type { DagSpec } from "../../api/gen/types.gen";
import { useBuilder } from "../../store/builder";
import { metaFixture, renderWithProviders } from "../../test/render";
import TestTaskDialog from "./TestTaskDialog";

const SPEC = { dag_id: "d", nodes: [], edges: [] } as unknown as DagSpec;

function stubFetch() {
  apiClient.setConfig({ baseUrl: "http://autoair.test/api", throwOnError: true });
  const modes: unknown[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (req: Request) => {
      if (req.url.includes("test-dag")) {
        modes.push(((await req.json()) as { mode: string }).mode);
        return Response.json({ state: "success", mode: "memory", tasks: [], log: "" });
      }
      return Response.json([]);
    }),
  );
  return modes;
}

const modeSelect = () => screen.getByLabelText("Mode") as HTMLSelectElement;
const dbOption = () => screen.getByRole("option", { name: "dag.test() (writes a run)" }) as HTMLOptionElement;

describe("DAG test mode", () => {
  beforeEach(() => useBuilder.getState().load(SPEC, true, { etag: "e1" }));
  afterEach(() => vi.unstubAllGlobals());

  it("disables db with the reason when the server turned it off", () => {
    stubFetch();
    renderWithProviders(<TestTaskDialog nodeId={null} whole />, {
      meta: metaFixture({ test_runs: { on_validator: true, db_mode_unavailable: "disabled" } }),
    });
    expect(dbOption().disabled).toBe(true);
    expect(screen.getByText(/runner\.allow_db_mode/)).toBeTruthy();
    expect(screen.getByText(/isolated validator service/)).toBeTruthy();
  });

  it("explains that db is for administrators", () => {
    stubFetch();
    renderWithProviders(<TestTaskDialog nodeId={null} whole />, {
      meta: metaFixture({ test_runs: { on_validator: false, db_mode_unavailable: "admin_only" } }),
    });
    expect(dbOption().disabled).toBe(true);
    expect(screen.getByText(/admin:config/)).toBeTruthy();
  });

  it("offers db when the caller may use it", async () => {
    const modes = stubFetch();
    renderWithProviders(<TestTaskDialog nodeId={null} whole />, {
      meta: metaFixture({ test_runs: { on_validator: false, db_mode_unavailable: null } }),
    });
    expect(dbOption().disabled).toBe(false);
    fireEvent.change(modeSelect(), { target: { value: "db" } });
    fireEvent.click(screen.getByRole("button", { name: /Run DAG/ }));
    await vi.waitFor(() => expect(modes).toEqual(["db"]));
  });

  it("leaves db to the server when it sends no test_runs info (older server)", () => {
    stubFetch();
    renderWithProviders(<TestTaskDialog nodeId={null} whole />, { meta: metaFixture() });
    expect(dbOption().disabled).toBe(false);
  });
});
