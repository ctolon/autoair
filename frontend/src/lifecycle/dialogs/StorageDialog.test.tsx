// @vitest-environment jsdom
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { client as apiClient } from "../../api/gen/client.gen";
import type { DeployTargetInfo, StorageSettings } from "../../api/gen/types.gen";
import { metaFixture, renderWithProviders } from "../../test/render";
import StorageDialog from "./StorageDialog";

function info(allowed: StorageSettings["mode"][] = ["local", "git", "object"]): DeployTargetInfo {
  const candidates = [
    {
      id: "local",
      kind: "local",
      title: "DAG folder",
      description: "/dags",
      settings: { mode: "local" },
    },
    {
      id: "bundle:s3-dags",
      kind: "object-bundle",
      title: "Airflow DAG bundle “s3-dags”",
      description: "",
      conn_id: "aws_dags",
      object_url: "s3://acme/dags",
      settings: { mode: "object", object_url: "s3://acme/dags" },
    },
    {
      id: "object",
      kind: "object",
      title: "Object storage",
      description: "",
      settings: { mode: "object", storage_options: { client_kwargs: { region_name: "eu-west-1" } } },
    },
  ] as DeployTargetInfo["candidates"];
  return {
    settings: { mode: "local" },
    source: "settings",
    status: { kind: "local", location: "/dags", writable: true, details: {} },
    candidates: candidates.filter((c) => allowed.includes(c.settings.mode ?? "local")),
    policy: { allowed_targets: allowed },
  } as DeployTargetInfo;
}

function stubFetch(payload: DeployTargetInfo) {
  apiClient.setConfig({ baseUrl: "http://autoair.test/api", throwOnError: true });
  const sent: { method: string; body: StorageSettings }[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (req: Request) => {
      if (req.url.includes("/deploy-target/test")) {
        sent.push({ method: "test", body: (await req.json()) as StorageSettings });
        return Response.json({ ok: true, message: "connected: s3://acme/dags" });
      }
      if (req.url.includes("/deploy-target") && req.method === "PUT") {
        sent.push({ method: "PUT", body: (await req.json()) as StorageSettings });
        return Response.json(payload);
      }
      if (req.url.includes("/deploy-target")) return Response.json(payload);
      if (req.url.endsWith("/meta")) return Response.json(metaFixture());
      return Response.json([]);
    }),
  );
  return sent;
}

const testButton = () => screen.getByRole("button", { name: /Test/ });
const saveButton = () => screen.getByRole("button", { name: /Save & use/ });

describe("StorageDialog object storage", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("lists object storage next to local and selects a discovered S3 bundle", async () => {
    const sent = stubFetch(info());
    renderWithProviders(<StorageDialog />);
    const bundle = await screen.findByTestId("deploy-candidate-object-bundle");
    expect(screen.getByTestId("deploy-candidate-object")).toBeTruthy();
    expect(screen.getByTestId("deploy-candidate-local")).toBeTruthy();
    expect(bundle.textContent).toContain("s3://acme/dags");

    fireEvent.click(bundle);
    const url = screen.getByTestId("object-url") as HTMLInputElement;
    expect(url.value).toBe("s3://acme/dags");
    expect(screen.getByText(/reads with connection aws_dags/)).toBeTruthy();
    fireEvent.click(saveButton());
    await waitFor(() => expect(sent.some((s) => s.method === "PUT")).toBe(true));
    const put = sent.find((s) => s.method === "PUT");
    expect(put?.body).toMatchObject({ mode: "object", object_url: "s3://acme/dags", storage_options: {} });
  });

  it("keeps the panel usable with invalid URL and options, and sends parsed options", async () => {
    const sent = stubFetch(info());
    renderWithProviders(<StorageDialog />);
    fireEvent.click(await screen.findByTestId("deploy-candidate-object"));
    const options = screen.getByTestId("object-options") as HTMLTextAreaElement;
    expect(JSON.parse(options.value)).toEqual({ client_kwargs: { region_name: "eu-west-1" } });

    // No URL yet: an inline problem, the buttons are disabled.
    expect(screen.getByText("Enter the object storage URL.")).toBeTruthy();
    expect((testButton() as HTMLButtonElement).disabled).toBe(true);

    const url = screen.getByTestId("object-url");
    fireEvent.change(url, { target: { value: "not a url" } });
    expect(screen.getByText(/Not a URL/)).toBeTruthy();
    fireEvent.change(url, { target: { value: "s3://acme/dags" } });
    expect((testButton() as HTMLButtonElement).disabled).toBe(false);

    for (const [text, problem] of [
      ['{"client_kwargs": ', "Not valid JSON."],
      ["[1, 2]", /Must be a JSON object/],
      ["null", /Must be a JSON object/],
    ] as const) {
      fireEvent.change(options, { target: { value: text } });
      expect(screen.getByText(problem)).toBeTruthy();
      expect((testButton() as HTMLButtonElement).disabled).toBe(true);
      expect((saveButton() as HTMLButtonElement).disabled).toBe(true);
    }

    fireEvent.change(options, { target: { value: '{"anon": false}' } });
    fireEvent.click(testButton());
    await waitFor(() => expect(sent.some((s) => s.method === "test")).toBe(true));
    expect(sent[0]?.body).toMatchObject({
      mode: "object",
      object_url: "s3://acme/dags",
      storage_options: { anon: false },
    });
    expect(await screen.findByText("connected: s3://acme/dags")).toBeTruthy();
  });

  it("offers no object storage when the policy does not allow it", async () => {
    stubFetch(info(["local", "git"]));
    renderWithProviders(<StorageDialog />);
    await screen.findByTestId("deploy-candidate-local");
    expect(screen.queryByTestId("deploy-candidate-object")).toBeNull();
    expect(screen.queryByTestId("deploy-candidate-object-bundle")).toBeNull();
  });
});
