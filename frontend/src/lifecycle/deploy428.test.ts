import { beforeEach, describe, expect, it, vi } from "vitest";

const deploySpec = vi.fn();
const getSpec = vi.fn();

vi.mock("../api/gen/sdk.gen", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  deploySpec: (...args: unknown[]) => deploySpec(...args),
  getSpec: (...args: unknown[]) => getSpec(...args),
}));

import { useBuilder } from "../store/builder";
import { deploy } from "./actions";
import { useLifecycle } from "./store";

describe("deploy over an existing DAG this document never loaded", () => {
  beforeEach(() => {
    deploySpec.mockReset();
    getSpec.mockReset();
    useBuilder.getState().reset("taken");
    useLifecycle.setState({ dialog: null } as never);
  });

  it("opens the conflict dialog with the stored spec instead of failing", async () => {
    deploySpec.mockResolvedValue({
      response: { status: 428 },
      error: { detail: "DAG 'taken' already exists" },
      data: undefined,
    });
    const stored = { dag_id: "taken", nodes: [], edges: [] };
    getSpec.mockResolvedValue({ data: { spec: stored, etag: "e-now" } });

    expect(await deploy()).toBe(false);

    expect(deploySpec.mock.calls[0]?.[0].body.base_etag).toBeNull();
    const dialog = useLifecycle.getState().dialog as { kind: string; conflict: Record<string, unknown> };
    expect(dialog.kind).toBe("conflict");
    expect(dialog.conflict).toEqual({ current_etag: "e-now", current_spec: stored });
  });
});
