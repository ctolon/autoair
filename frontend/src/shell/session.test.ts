import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { type AuthConfig, startSessionWatcher } from "./session";

const config = (over: Partial<AuthConfig["sso"]> = {}): AuthConfig => ({
  mode: "standalone",
  auth: "bearer",
  login_enabled: true,
  sso: {
    enabled: true,
    cookie_name: "_token",
    airflow_login_url: "/api/auth/sso/login",
    refresh_margin_seconds: 300,
    session_check_seconds: 60,
    propagate_logout: true,
    ...over,
  },
  ui: {
    title: "AutoAir",
    logo_url: null,
    accent_color: null,
    banner_text: null,
    banner_color: "orange",
    default_language: null,
    default_timezone: null,
    date_locale: null,
  },
});

type Answer = { status: number; body?: unknown };

function mockFetch(answers: Record<string, Answer[]>) {
  const calls: { url: string; method: string; headers: Record<string, string> }[] = [];
  const fn = vi.fn((url: string, init?: RequestInit) => {
    const path = url.replace(/^\/api/, "");
    calls.push({ url: path, method: init?.method ?? "GET", headers: (init?.headers ?? {}) as Record<string, string> });
    const queue = answers[path] ?? [];
    const answer = (queue.length > 1 ? queue.shift() : queue[0]) ?? { status: 404 };
    return Promise.resolve(
      new Response(answer.body === undefined ? null : JSON.stringify(answer.body), { status: answer.status }),
    );
  });
  vi.stubGlobal("fetch", fn);
  return calls;
}

const session = (over: Record<string, unknown> = {}) => ({
  authenticated: true,
  source: "cookie",
  user: "alice",
  expires_at: null,
  expires_in: 400,
  verified: true,
  refreshable: true,
  ...over,
});

describe("session watcher", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.stubGlobal("window", { __AUTOAIR_CONFIG__: { apiBase: "/api" } });
    vi.stubGlobal("document", {
      visibilityState: "visible",
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    });
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("refreshes a cookie session before it expires and sends the CSRF header", async () => {
    const calls = mockFetch({
      "/auth/session": [{ status: 200, body: session() }],
      "/auth/refresh": [{ status: 200, body: session({ expires_in: 7200 }) }],
    });
    const onEnded = vi.fn();
    const stop = startSessionWatcher(config({ session_check_seconds: 0 }), { onEnded });
    await vi.advanceTimersByTimeAsync(1_000);
    expect(calls.filter((c) => c.url === "/auth/refresh")).toHaveLength(0);
    await vi.advanceTimersByTimeAsync(100_000); // 400 s - 300 s margin
    const refresh = calls.filter((c) => c.url === "/auth/refresh");
    expect(refresh).toHaveLength(1);
    expect(refresh[0]?.method).toBe("POST");
    expect(refresh[0]?.headers["X-AutoAir-CSRF"]).toBe("1");
    expect(onEnded).not.toHaveBeenCalled();
    stop();
  });

  it("signs out when Airflow ends the session (periodic check)", async () => {
    mockFetch({
      "/auth/session": [{ status: 200, body: session({ expires_in: 3600 }) }, { status: 401 }],
    });
    const onEnded = vi.fn();
    const stop = startSessionWatcher(config({ session_check_seconds: 30 }), { onEnded });
    await vi.advanceTimersByTimeAsync(1_000);
    expect(onEnded).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(30_000);
    expect(onEnded).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(120_000);
    expect(onEnded).toHaveBeenCalledTimes(1); // stopped after the first end
    stop();
  });

  it("warns before a bearer token expires (it cannot be refreshed)", async () => {
    mockFetch({
      "/auth/session": [{ status: 200, body: session({ source: "bearer", refreshable: false, expires_in: 600 }) }],
    });
    const onExpiring = vi.fn();
    const stop = startSessionWatcher(config({ session_check_seconds: 0 }), { onEnded: vi.fn(), onExpiring });
    await vi.advanceTimersByTimeAsync(299_000);
    expect(onExpiring).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(2_000);
    expect(onExpiring).toHaveBeenCalledWith(5);
    stop();
  });

  it("keeps the session when the server cannot be reached", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.reject(new Error("offline"))),
    );
    const onEnded = vi.fn();
    const stop = startSessionWatcher(config({ session_check_seconds: 10 }), { onEnded });
    await vi.advanceTimersByTimeAsync(60_000);
    expect(onEnded).not.toHaveBeenCalled();
    stop();
  });
});
