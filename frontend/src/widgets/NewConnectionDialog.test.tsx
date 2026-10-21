// @vitest-environment jsdom
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { listConnectionHookMetaQueryKey, listConnectionTypesQueryKey } from "../api/gen/@tanstack/react-query.gen";
import { client as apiClient } from "../api/gen/client.gen";
import type { ConnectionHookMeta } from "../api/gen/types.gen";
import { renderWithProviders, testQueryClient } from "../test/render";
import { NewConnectionDialog } from "./NewConnectionDialog";

const SLACK: ConnectionHookMeta = {
  connection_type: "slack",
  hook_name: "Slack API",
  standard_fields: {
    url_schema: { hidden: true },
    host: { hidden: true },
    password: { hidden: false, title: "Slack API Token", placeholder: "xoxb" },
  },
  extra_fields: { timeout: { schema: { type: ["integer", "null"], title: "Timeout" } } },
};

function setup(metas: ConnectionHookMeta[] | null) {
  const client = testQueryClient();
  client.setQueryData(listConnectionTypesQueryKey(), []);
  if (metas) client.setQueryData(listConnectionHookMetaQueryKey(), metas);
  renderWithProviders(
    <NewConnectionDialog connTypes={["slack"]} suggestedId="slack_default" onCreated={() => {}} onClose={() => {}} />,
    { client },
  );
}

describe("NewConnectionDialog", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("relabels and hides standard fields and builds extra from typed fields", async () => {
    const fetch = vi.fn(async (_req: Request) =>
      Response.json({ connection_id: "slack_default", conn_type: "slack" }, { status: 201 }),
    );
    vi.stubGlobal("fetch", fetch);
    apiClient.setConfig({ baseUrl: "http://autoair.test/api", throwOnError: true });
    setup([SLACK]);
    expect(screen.getByText("Slack API Token")).toBeTruthy();
    expect(screen.queryByText("Host")).toBeNull();
    expect(screen.queryByText("Schema")).toBeNull();
    // Raw JSON is an advanced option when the type has typed extra fields.
    expect(screen.queryByText("Extra (JSON)")).toBeNull();

    const timeout = screen.getByLabelText(/Timeout/);
    fireEvent.change(timeout, { target: { value: "abc" } });
    expect(await screen.findByText("Not a valid integer value")).toBeTruthy();
    const create = screen.getByRole("button", { name: /Create/ });
    expect(create.hasAttribute("disabled")).toBe(true);

    fireEvent.change(timeout, { target: { value: "5" } });
    await waitFor(() => expect(create.hasAttribute("disabled")).toBe(false));
    fireEvent.click(create);
    await waitFor(() => expect(fetch).toHaveBeenCalled());
    const request = fetch.mock.calls[0]?.[0];
    const body = (await request?.json()) as Record<string, unknown>;
    expect(body).toEqual({ connection_id: "slack_default", conn_type: "slack", extra: '{"timeout":5}' });
  });

  it("falls back to the generic form with raw extra when Airflow has no hook metadata", () => {
    setup([]);
    expect(screen.getByText("Host")).toBeTruthy();
    expect(screen.getByText("Password")).toBeTruthy();
    expect(screen.getByText("Extra (JSON)")).toBeTruthy();
  });

  it("does not break on invalid raw JSON", async () => {
    setup(null);
    const raw = screen.getByPlaceholderText('{"key": "value"}');
    fireEvent.change(raw, { target: { value: "{oops" } });
    expect(await screen.findByText("Must be a JSON object")).toBeTruthy();
    expect(screen.getByRole("button", { name: /Create/ }).hasAttribute("disabled")).toBe(true);
  });
});
