import { describe, expect, it } from "vitest";

import type { AiStreamEvent } from "../lib/aiStream";
import { type AiStreamState, applyEvent, streamAnnouncement, streamEnded } from "./AiStream";

const initial: AiStreamState = {
  running: true,
  text: "",
  stage: null,
  stageMessage: null,
  stages: [],
  tools: [],
  guardrails: [],
  usage: null,
  result: null,
  error: null,
  errorStatus: null,
  cancelled: false,
  interrupted: false,
};

describe("applyEvent", () => {
  it("tracks tokens, stages, tool calls, guardrails and the result", () => {
    const events: AiStreamEvent[] = [
      { type: "status", stage: "research", message: "Gathering" },
      { type: "token", text: "Hel" },
      { type: "token", text: "lo" },
      { type: "tool_call", id: "1", name: "search_docs", arguments: { query: "s3" } },
      { type: "tool_call", id: "2", name: "read_source" },
      { type: "tool_result", id: "1", name: "search_docs", ok: true, duration_ms: 12 },
      { type: "tool_result", id: "2", name: "read_source", ok: false },
      { type: "guardrail", guardrail: "secrets", action: "redacted" },
      { type: "guardrail", guardrail: "secrets", action: "redacted" },
      { type: "status", stage: "research" },
      { type: "result", kind: "agent", data: { answer: "a" } },
      { type: "done" },
    ];
    const s = events.reduce(applyEvent, initial);
    expect(s.text).toBe("Hello\n\n");
    expect(s.stages).toEqual(["research"]);
    expect(s.tools.map((t) => t.state)).toEqual(["ok", "failed"]);
    expect(s.tools[0]?.durationMs).toBe(12);
    expect(s.tools[0]?.args).toBe('{"query":"s3"}');
    expect(s.guardrails).toEqual(["secrets: redacted"]);
    expect(s.result).toEqual({ kind: "agent", data: { answer: "a" } });
    expect(s.running).toBe(false);
  });

  it("ignores malformed or unknown events", () => {
    const odd = [
      { type: "token" },
      { type: "tool_result", id: "missing", ok: true },
      { type: "mystery" },
      { type: "error", message: "boom", status: 428 },
    ] as unknown as AiStreamEvent[];
    const s = odd.reduce(applyEvent, initial);
    expect(s.text).toBe("");
    expect(s.tools).toEqual([]);
    expect(s.error).toBe("boom");
    expect(s.errorStatus).toBe(428);
  });

  it("flags a stream that ended without a done event as interrupted", () => {
    const partial = applyEvent(initial, { type: "token", text: "Hal" });
    const cut = streamEnded(partial, false);
    expect(cut.interrupted).toBe(true);
    expect(cut.running).toBe(false);
    expect(cut.text).toBe("Hal");
    const finished = streamEnded(applyEvent(partial, { type: "done" }), true);
    expect(finished.interrupted).toBe(false);
    // An error / a user cancel is reported as such, not as an interruption.
    expect(streamEnded({ ...partial, error: "boom" }, false).interrupted).toBe(false);
    expect(streamEnded({ ...partial, cancelled: true }, false).interrupted).toBe(false);
  });
});

describe("streamAnnouncement", () => {
  const t = (key: string) => key;
  it("announces status changes, never the streamed text", () => {
    const running = applyEvent(applyEvent(initial, { type: "status", stage: "plan" }), {
      type: "token",
      text: "secret tokens",
    });
    expect(streamAnnouncement(running, "Planning", t)).toBe("Planning");
    const more = applyEvent(running, { type: "token", text: " more" });
    expect(streamAnnouncement(more, "Planning", t)).toBe("Planning");
    expect(streamAnnouncement({ ...more, running: false }, "", t)).toBe("ai.stream.finished");
    expect(streamAnnouncement({ ...more, running: false, interrupted: true }, "", t)).toBe("ai.stream.interrupted");
    expect(streamAnnouncement({ ...more, running: false, cancelled: true }, "", t)).toBe("ai.stream.cancelled");
    for (const s of [running, more]) expect(streamAnnouncement(s, "Planning", t)).not.toContain("tokens");
  });
});
