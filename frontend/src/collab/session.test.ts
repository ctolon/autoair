// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";

import { collabServerUrl } from "./session";

const at = (href: string) => new URL(href) as unknown as Location;

describe("collabServerUrl", () => {
  it("derives the room server from the API base (plugin, standalone, TLS)", () => {
    expect(collabServerUrl("/autoair/api", at("http://airflow:8080/dags"))).toBe("ws://airflow:8080/autoair/collab");
    expect(collabServerUrl("/api", at("https://autoair.acme.io/ui/"))).toBe("wss://autoair.acme.io/collab");
    expect(collabServerUrl("/sub/autoair/api/", at("http://h/"))).toBe("ws://h/sub/autoair/collab");
  });
});

const tickets = vi.hoisted(() => ({ issued: 0, fail: false }));
const providers = vi.hoisted(() => [] as FakeProvider[]);

interface FakeProvider {
  params: Record<string, string>;
  connects: string[];
  emit: (name: string, ...args: unknown[]) => void;
}

vi.mock("../api/gen/sdk.gen", () => ({
  collabTicket: () => {
    if (tickets.fail) return Promise.reject(new Error("missing AutoAir permission 'spec:write'"));
    tickets.issued += 1;
    return Promise.resolve({ data: { ticket: `t${tickets.issued}`, expires_in: 60 } });
  },
}));

vi.mock("y-websocket", () => {
  class WebsocketProvider {
    params: Record<string, string>;
    connects: string[] = [];
    handlers = new Map<string, ((...args: unknown[]) => void)[]>();
    awareness = {
      setLocalStateField: () => undefined,
      setLocalState: () => undefined,
      on: () => undefined,
      getStates: () => new Map(),
      getLocalState: () => null,
    };
    constructor(
      _url: string,
      _room: string,
      _doc: unknown,
      opts: { params: Record<string, string>; connect?: boolean },
    ) {
      this.params = opts.params;
      providers.push(this as unknown as FakeProvider);
    }
    on(name: string, fn: (...args: unknown[]) => void) {
      this.handlers.set(name, [...(this.handlers.get(name) ?? []), fn]);
    }
    emit(name: string, ...args: unknown[]) {
      for (const fn of this.handlers.get(name) ?? []) fn(...args);
    }
    connect() {
      this.connects.push(this.params.ticket ?? "");
    }
    destroy() {}
  }
  return { WebsocketProvider };
});

describe("startCollab", () => {
  it("asks for a new single-use ticket before every reconnect", async () => {
    const { startCollab, stopCollab, useCollab } = await import("./session");
    await startCollab("demo");
    const provider = providers.at(-1);
    if (!provider) throw new Error("no provider");
    expect(provider.connects).toEqual(["t1"]);
    provider.emit("closed", { code: 4001, reason: "reauthenticate" }); // the session ended
    await vi.waitFor(() => expect(provider.connects).toEqual(["t1", "t2"]));
    tickets.fail = true; // permission revoked meanwhile
    provider.emit("closed", { code: 4001, reason: "reauthenticate" });
    await vi.waitFor(() => expect(useCollab.getState().status).toBe("error"));
    expect(provider.connects).toEqual(["t1", "t2"]);
    stopCollab();
    tickets.fail = false;
  });

  it("backs off between failed attempts", async () => {
    const { reconnectDelay } = await import("./session");
    expect([0, 1, 2, 3, 20].map(reconnectDelay)).toEqual([0, 200, 400, 800, 10_000]);
  });
});
