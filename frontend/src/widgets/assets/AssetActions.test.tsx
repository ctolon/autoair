// @vitest-environment jsdom
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { getAirflowCapabilitiesQueryKey, listAssetsQueryKey } from "../../api/gen/@tanstack/react-query.gen";
import { client as apiClient } from "../../api/gen/client.gen";
import type { AirflowCapabilities, MetaInfo } from "../../api/gen/types.gen";
import { NO_CAPABILITIES } from "../../lifecycle/capabilities";
import { metaFixture, renderWithProviders, testQueryClient } from "../../test/render";
import { AssetRefEditor } from "./AssetRefEditor";

function setup({ caps = {}, meta = metaFixture() }: { caps?: Partial<AirflowCapabilities>; meta?: MetaInfo } = {}) {
  const client = testQueryClient(meta);
  client.setQueryData(listAssetsQueryKey(), [{ id: 7, name: "orders", uri: "s3://b/orders", group: "asset" }]);
  client.setQueryData(getAirflowCapabilitiesQueryKey(), { ...NO_CAPABILITIES, ...caps });
  renderWithProviders(<AssetRefEditor value={{ name: "orders" }} onChange={() => {}} allowWatchers={false} />, {
    client,
  });
}

describe("AssetActions", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("sends a test event with extra JSON", async () => {
    const fetch = vi.fn(async (_req: Request) => Response.json({ id: 1 }));
    vi.stubGlobal("fetch", fetch);
    apiClient.setConfig({ baseUrl: "http://autoair.test/api", throwOnError: true });
    setup();
    expect(screen.queryByRole("button", { name: /Materialize/ })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: /Send test event/ }));
    fireEvent.click(screen.getByRole("button", { name: /^Send$/ }));
    await waitFor(() => expect(fetch).toHaveBeenCalled());
    const request = fetch.mock.calls[0]?.[0];
    expect(request?.url).toBe("http://autoair.test/api/airflow/assets/7/events");
    expect(await request?.json()).toEqual({ extra: {} });
  });

  it("offers materialize when the Airflow version supports it", () => {
    setup({ caps: { materialize: true } });
    expect(screen.getByRole("button", { name: /Materialize/ })).toBeTruthy();
  });

  it("is disabled without run:trigger", () => {
    setup({ caps: { materialize: true }, meta: metaFixture({ iam: { permissions: ["spec:read"] } }) });
    expect(screen.getByRole("button", { name: /Send test event/ }).hasAttribute("disabled")).toBe(true);
    expect(screen.getByRole("button", { name: /Materialize/ }).hasAttribute("disabled")).toBe(true);
  });

  it("is hidden when runs are switched off and for assets Airflow does not know", () => {
    setup({ meta: metaFixture({ features: { runs: false } }) });
    expect(screen.queryByTestId("asset-actions")).toBeNull();
  });
});
