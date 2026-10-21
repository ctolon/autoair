import { describe, expect, it } from "vitest";

import { SseParser, parseFrame } from "./aiStream";

describe("SseParser", () => {
  it("handles frames split at arbitrary chunk boundaries", () => {
    const text =
      'event: token\ndata: {"type":"token","text":"Hel"}\n\n' +
      'event: token\ndata: {"type":"token","text":"lo"}\n\n' +
      'event: done\ndata: {"type":"done"}\n\n';
    for (let size = 1; size < 20; size++) {
      const parser = new SseParser();
      const events = [];
      for (let i = 0; i < text.length; i += size) events.push(...parser.push(text.slice(i, i + size)));
      events.push(...parser.flush());
      expect(events.map((e) => e.type)).toEqual(["token", "token", "done"]);
      expect(events.flatMap((e) => (e.type === "token" ? [e.text] : [])).join("")).toBe("Hello");
    }
  });

  it("ignores keep-alive comments and joins multi-line data", () => {
    const parser = new SseParser();
    const events = parser.push(
      ': keep-alive\n\nevent: result\ndata: {"type":"result",\ndata: "kind":"agent","data":{"answer":"a"}}\n\n',
    );
    expect(events).toEqual([{ type: "result", kind: "agent", data: { answer: "a" } }]);
  });

  it("normalises CRLF, tolerates bad JSON and uses the event name as fallback type", () => {
    const parser = new SseParser();
    const events = parser.push('data: not json\r\n\r\nevent: status\r\ndata: {"stage":"write"}\r\n\r\n');
    expect(events).toEqual([{ type: "status", stage: "write" }]);
    expect(parseFrame(": only a comment")).toBeNull();
    expect(parseFrame("data: 42")).toBeNull();
  });

  it("flushes a trailing frame without a blank line", () => {
    const parser = new SseParser();
    expect(parser.push('data: {"type":"done"}')).toEqual([]);
    expect(parser.flush()).toEqual([{ type: "done" }]);
  });

  it("handles CRLF split across chunks (no phantom blank line, no lost frame)", () => {
    const text = 'data: {"type":"token","text":"a"}\r\n\r\ndata: {"type":"token","text":"b"}\r\n\r\n';
    for (let cut = 1; cut < text.length; cut++) {
      const parser = new SseParser();
      const events = [...parser.push(text.slice(0, cut)), ...parser.push(text.slice(cut)), ...parser.flush()];
      expect(events.flatMap((e) => (e.type === "token" ? [e.text] : []))).toEqual(["a", "b"]);
    }
    // "\r" at the end of one chunk + "\n" at the start of the next is one line break, so the
    // frame is not split into two after the first line.
    const parser = new SseParser();
    expect(parser.push("event: status\r")).toEqual([]);
    expect(parser.push('\ndata: {"stage":"plan"}\r\n\r')).toEqual([]);
    expect(parser.push("\n")).toEqual([{ type: "status", stage: "plan" }]);
  });

  it("treats a lone CR at the end of the stream as a line break", () => {
    const parser = new SseParser();
    expect(parser.push('data: {"type":"done"}\r')).toEqual([]);
    expect(parser.flush()).toEqual([{ type: "done" }]);
  });
});
