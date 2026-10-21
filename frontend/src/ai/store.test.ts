import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ChatMessage } from "../api/gen/types.gen";
import { mergeTurn, useAiChat } from "./store";

const msg = (role: "user" | "assistant", content: string, id = content): ChatMessage => ({ id, role, content });

describe("mergeTurn", () => {
  it("appends the stored user message and answer", () => {
    const out = mergeTurn(
      [msg("user", "a"), msg("assistant", "b")],
      {
        thread: { id: "t", mode: "developer", title: "x", created_at: "", updated_at: "" },
        user_message: msg("user", "c"),
        message: msg("assistant", "d"),
      },
      false,
    );
    expect(out.map((m) => m.content)).toEqual(["a", "b", "c", "d"]);
  });

  it("replaces the last answer on regenerate", () => {
    const out = mergeTurn(
      [msg("user", "a"), msg("assistant", "b")],
      {
        thread: { id: "t", mode: "developer", title: "x", created_at: "", updated_at: "" },
        user_message: msg("user", "a"),
        message: msg("assistant", "b2"),
      },
      true,
    );
    expect(out.map((m) => m.content)).toEqual(["a", "b2"]);
  });
});

function stubBrowser() {
  vi.stubGlobal("window", { __AUTOAIR_CONFIG__: { apiBase: "/api" } });
  vi.stubGlobal("document", { baseURI: "http://localhost/", cookie: "" });
  vi.stubGlobal("sessionStorage", { getItem: () => null, setItem: () => undefined, removeItem: () => undefined });
}

describe("chat store", () => {
  beforeEach(stubBrowser);
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
    useAiChat.setState({ open: false, mode: "developer", threadId: null, messages: [], pending: null, error: null });
  });

  it("switching assistants starts a new conversation", () => {
    useAiChat.setState({ threadId: "abc", messages: [msg("user", "x")] });
    useAiChat.getState().setMode("admin");
    const s = useAiChat.getState();
    expect(s.mode).toBe("admin");
    expect(s.threadId).toBeNull();
    expect(s.messages).toEqual([]);
  });

  it("opens with a mode and a prefilled prompt", () => {
    useAiChat.getState().openPanel({ mode: "admin", prompt: "Why?" });
    expect(useAiChat.getState()).toMatchObject({ open: true, mode: "admin", draft: "Why?" });
    useAiChat.getState().toggle();
    expect(useAiChat.getState().open).toBe(false);
  });

  it("streams a turn and stores the result", async () => {
    const frames = [
      { type: "status", stage: "thread", message: "t1" },
      { type: "token", text: "Hello" },
      {
        type: "result",
        kind: "assistant_turn",
        data: {
          thread: { id: "t1", mode: "developer", title: "Hi", created_at: "", updated_at: "" },
          user_message: msg("user", "Hi", "u1"),
          message: msg("assistant", "Hello", "a1"),
        },
      },
      { type: "done" },
    ];
    const body = frames.map((f) => `event: ${f.type}\ndata: ${JSON.stringify(f)}\n\n`).join("");
    const fetchMock = vi.fn(async (_url: string, init: RequestInit) => {
      expect(JSON.parse(String(init.body))).toMatchObject({ mode: "developer", message: "Hi", thread_id: null });
      return new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" } });
    });
    vi.stubGlobal("fetch", fetchMock);
    await useAiChat.getState().send("Hi", {});
    const s = useAiChat.getState();
    expect(s.pending).toBeNull();
    expect(s.threadId).toBe("t1");
    expect(s.title).toBe("Hi");
    expect(s.messages.map((m) => m.id)).toEqual(["u1", "a1"]);
    vi.unstubAllGlobals();
  });

  it("keeps the draft and shows the error when the request is refused", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify({ detail: "the message is empty" }), { status: 422 })),
    );
    await useAiChat.getState().send("x", {});
    expect(useAiChat.getState()).toMatchObject({ pending: null, error: "the message is empty", draft: "x" });
    vi.unstubAllGlobals();
  });
});
