// @vitest-environment jsdom
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { getGitCredentialsQueryKey } from "../../api/gen/@tanstack/react-query.gen";
import { client as apiClient } from "../../api/gen/client.gen";
import type { GitCredentialChoice, RequestTokenInfo } from "../../api/gen/types.gen";
import { useBuilder } from "../../store/builder";
import { metaFixture, renderWithProviders, testQueryClient } from "../../test/render";
import { useLifecycle } from "../store";
import DeployDialog from "./DeployDialog";

const TOKEN = "ghp_secretTOKEN_for_one_deploy_123";

function tokenInfo(mode: RequestTokenInfo["mode"]): RequestTokenInfo {
  return {
    mode,
    header: "X-AutoAir-Git-Token",
    reason: null,
    hint: {
      provider: "github",
      token_type: "Fine-grained personal access token",
      scopes: ["contents:write", "pull_requests:write"],
      docs_url: "https://docs.github.com/",
    },
  };
}

function setup(mode: RequestTokenInfo["mode"]) {
  // Query keys include the client's base URL: configure it before seeding the cache.
  apiClient.setConfig({ baseUrl: "http://autoair.test/api", throwOnError: true });
  const meta = metaFixture({
    storage: { kind: "git", location: "github:acme/dags", writable: true, details: { mode: "pr" } },
    iam: {},
  });
  const client = testQueryClient(meta);
  const creds: GitCredentialChoice = { mode: "shared", request_token: tokenInfo(mode) } as GitCredentialChoice;
  client.setQueryData(getGitCredentialsQueryKey(), creds);
  const requests: Request[] = [];
  const fetch = vi.fn(async (req: Request) => {
    requests.push(req);
    if (req.url.includes("deploy-preview")) {
      return Response.json({
        target_kind: "git",
        commit_message: "AutoAir: update DAG",
        context: "",
        validation: "ok",
        commit_author: "service",
      });
    }
    if (req.method === "PUT") {
      return Response.json({
        deployed: true,
        status: "deployed",
        report: { ok: true, problems: [] },
        etag: "e1",
        message: "committed",
        branch: "main",
      });
    }
    return Response.json([]);
  });
  vi.stubGlobal("fetch", fetch);
  const view = renderWithProviders(<DeployDialog focus="message" />, { meta, client });
  return { ...view, requests };
}

const deploys = (requests: Request[]) => requests.filter((r) => r.method === "PUT");
const input = () => screen.getByTestId("git-token-input") as HTMLInputElement;

describe("DeployDialog git token", () => {
  beforeEach(() => {
    localStorage.clear();
    sessionStorage.clear();
    useBuilder.getState().reset("token_dag");
    useLifecycle.getState().openDialog({ kind: "deploy", focus: "message" });
  });
  afterEach(() => vi.unstubAllGlobals());

  it("is collapsed under 'Use my own token' when optional and toggles visibility", async () => {
    setup("optional");
    expect(screen.queryByTestId("git-token-input")).toBeNull();
    const own = screen.getByTestId("git-token-own").querySelector("label") ?? screen.getByTestId("git-token-own");
    if (!own) throw new Error("checkbox not found");
    fireEvent.click(own);
    await waitFor(() => expect(screen.getByTestId("git-token-input")).toBeTruthy());
    expect(input().type).toBe("password");
    expect(input().getAttribute("autocomplete")).toBe("off");
    expect(input().getAttribute("spellcheck")).toBe("false");
    // the provider hint: token kind and scopes
    expect(screen.getByText(/contents:write, pull_requests:write/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Show token" }));
    expect(input().type).toBe("text");
    fireEvent.click(screen.getByRole("button", { name: "Hide token" }));
    expect(input().type).toBe("password");
  });

  it("validates a required token before deploying", async () => {
    const { requests } = setup("required");
    expect(input()).toBeTruthy(); // always shown when required
    fireEvent.click(screen.getByRole("button", { name: /Deploy$/ }));
    await waitFor(() => expect(screen.getByText("This deploy target needs your own git token.")).toBeTruthy());
    expect(deploys(requests)).toHaveLength(0);
  });

  it("sends the token only as the header of that request and keeps it nowhere", async () => {
    const { requests, client } = setup("required");
    fireEvent.change(input(), { target: { value: TOKEN } });
    fireEvent.click(screen.getByRole("button", { name: /Deploy$/ }));
    await waitFor(() => expect(deploys(requests)).toHaveLength(1));
    const [put] = deploys(requests);
    if (!put) throw new Error("no deploy request");
    expect(put.headers.get("X-AutoAir-Git-Token")).toBe(TOKEN);
    expect(put.url).not.toContain(TOKEN);
    expect(await put.clone().text()).not.toContain(TOKEN);
    // other requests (the preview) never carry it
    for (const r of requests.filter((x) => x !== put)) expect(r.headers.get("X-AutoAir-Git-Token")).toBeNull();
    await waitFor(() => expect(useLifecycle.getState().dialog).toBeNull());

    const everywhere = JSON.stringify({
      local: { ...localStorage },
      session: { ...sessionStorage },
      builder: useBuilder.getState(),
      lifecycle: useLifecycle.getState(),
      queries: client
        .getQueryCache()
        .getAll()
        .map((q) => [q.queryKey, q.state.data]),
      mutations: client
        .getMutationCache()
        .getAll()
        .map((m) => m.state.variables),
      url: window.location.href,
    });
    expect(everywhere).not.toContain(TOKEN);
  });
});
