// @vitest-environment jsdom
import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import {
  listConnectionTypesQueryKey,
  listConnectionsQueryKey,
  listPoolsQueryKey,
} from "../api/gen/@tanstack/react-query.gen";
import { metaFixture, renderWithProviders, testQueryClient } from "../test/render";
import { ConnectionPicker, PoolPicker, missingFromMetastore } from "./AirflowPickers";

function renderPicker(value: string, secretsBackend: boolean, loaded = true) {
  const client = testQueryClient(metaFixture({ secrets_backend: secretsBackend }));
  if (loaded) client.setQueryData(listConnectionsQueryKey(), [{ connection_id: "pg_main", conn_type: "postgres" }]);
  client.setQueryData(listConnectionTypesQueryKey(), []);
  renderWithProviders(<ConnectionPicker label="Connection" value={value} onChange={() => {}} />, { client });
}

describe("missingFromMetastore", () => {
  it("only flags plain ids absent from a loaded list", () => {
    expect(missingFromMetastore("vault_conn", ["a"])).toBe(true);
    expect(missingFromMetastore("a", ["a"])).toBe(false);
    expect(missingFromMetastore("", ["a"])).toBe(false);
    expect(missingFromMetastore("{{ var.value.conn }}", ["a"])).toBe(false);
    expect(missingFromMetastore("vault_conn", undefined)).toBe(false);
  });
});

describe("ConnectionPicker", () => {
  it("accepts an id missing from the metastore with a non-blocking hint", () => {
    renderPicker("vault_conn", false);
    expect((screen.getByRole("combobox") as HTMLInputElement).value).toBe("vault_conn");
    expect(screen.getByTestId("metastore-hint").textContent).toContain("secrets backend");
  });

  it("shows no hint for known ids", () => {
    renderPicker("pg_main", false);
    expect(screen.queryByTestId("metastore-hint")).toBeNull();
  });

  it("suppresses the hint when a secrets backend is configured", () => {
    renderPicker("vault_conn", true);
    expect(screen.queryByTestId("metastore-hint")).toBeNull();
  });

  it("shows no hint when Airflow's list is unavailable", () => {
    renderPicker("vault_conn", false, false);
    expect(screen.queryByTestId("metastore-hint")).toBeNull();
  });
});

describe("PoolPicker", () => {
  it("hints at pools Airflow does not have", () => {
    const client = testQueryClient();
    client.setQueryData(listPoolsQueryKey(), [{ name: "default_pool", slots: 128 }]);
    renderWithProviders(<PoolPicker label="Pool" value="etl" onChange={() => {}} />, { client });
    expect(screen.getByTestId("metastore-hint").textContent).toContain("etl");
  });
});
