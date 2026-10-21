// @vitest-environment jsdom
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { client as apiClient } from "../../api/gen/client.gen";
import type { ConflictInfo, DagSpec } from "../../api/gen/types.gen";
import { useBuilder } from "../../store/builder";
import { metaFixture, renderWithProviders } from "../../test/render";
import { deploy } from "../actions";
import { useLifecycle } from "../store";
import ConflictDialog from "./ConflictDialog";

const SPEC = { dag_id: "kept", nodes: [], edges: [] } as unknown as DagSpec;
const KEPT: ConflictInfo = { ejected_file: "/dags/kept.py", hand_written: true, force_allowed: true };
const FORCE_ON = metaFixture({ allow_force_overwrite: true });

function stubFetch(body: Record<string, unknown>) {
  apiClient.setConfig({ baseUrl: "http://autoair.test/api", throwOnError: true });
  const requests: Request[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (req: Request) => {
      requests.push(req);
      return Response.json(body);
    }),
  );
  return requests;
}

const DEPLOYED = { deployed: true, status: "deployed", report: { ok: true, problems: [] }, etag: "e2", message: "ok" };

describe("ConflictDialog (a DAG file AutoAir would not overwrite on its own)", () => {
  beforeEach(() => {
    useBuilder.getState().load(SPEC, true, { etag: "e1" });
    useLifecycle.getState().closeDialog();
  });
  afterEach(() => vi.unstubAllGlobals());

  it("offers the forced overwrite of the kept file where the server accepts it", async () => {
    const requests = stubFetch(DEPLOYED);
    renderWithProviders(<ConflictDialog conflict={KEPT} request={{}} spec={SPEC} />, { meta: FORCE_ON });
    expect(screen.getByText("This DAG keeps a hand-written file")).toBeTruthy();
    fireEvent.click(screen.getByTestId("conflict-force"));
    await waitFor(() => expect(requests.some((r) => r.method === "PUT")).toBe(true));
    const put = requests.find((r) => r.method === "PUT");
    if (!put) throw new Error("no deploy");
    expect(JSON.parse(await put.clone().text())).toMatchObject({ force: true });
  });

  it("shows no force button while overwrites are off (preserve.allow_force_overwrite)", () => {
    stubFetch(DEPLOYED);
    renderWithProviders(<ConflictDialog conflict={KEPT} request={{}} spec={SPEC} />);
    expect(screen.queryByTestId("conflict-force")).toBeNull();
    expect(screen.getByText(/AutoAir does not overwrite this file here/)).toBeTruthy();
  });

  it("shows no force button when the server would refuse it for this user", () => {
    stubFetch(DEPLOYED);
    const conflict = { ...KEPT, force_allowed: false };
    renderWithProviders(<ConflictDialog conflict={conflict} request={{}} spec={SPEC} />, { meta: FORCE_ON });
    expect(screen.queryByTestId("conflict-force")).toBeNull();
  });

  it("reports a refused kept file with the server's message instead of opening the dialog", async () => {
    stubFetch({
      deployed: false,
      status: "ejected",
      report: { ok: true, problems: [] },
      message: "/dags/kept.py is the hand-written DAG file AutoAir keeps for this DAG",
      conflict: { ...KEPT, force_allowed: false },
    });
    expect(await deploy({ spec: SPEC })).toBe(false);
    expect(useLifecycle.getState().dialog).toBeNull();
  });

  it("opens the dialog when the forced overwrite would be accepted", async () => {
    stubFetch({
      deployed: false,
      status: "ejected",
      report: { ok: true, problems: [] },
      message: "changed",
      conflict: KEPT,
    });
    expect(await deploy({ spec: SPEC })).toBe(false);
    expect(useLifecycle.getState().dialog?.kind).toBe("conflict");
  });
});
