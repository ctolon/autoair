import { authHeaders, resolveApiBase } from "../../api/setup";
import type { Usage } from "../../api/gen/types.gen";

/** Events of the AI streaming endpoints (`text/event-stream`, see autoair.ai.events). */
export type AiStreamEvent =
  | { type: "status"; stage: string; message?: string; ts?: number }
  | { type: "token"; text: string; ts?: number }
  | { type: "tool_call"; id: string; name: string; arguments?: Record<string, unknown>; ts?: number }
  | { type: "tool_result"; id: string; name: string; ok: boolean; preview?: string; duration_ms?: number; ts?: number }
  | { type: "guardrail"; guardrail: string; action: string; detail?: string; ts?: number }
  | { type: "usage"; usage: Usage; ts?: number }
  | { type: "result"; kind: string; data: unknown; ts?: number }
  | { type: "error"; message: string; status?: number; ts?: number }
  | { type: "done"; cancelled?: boolean; ts?: number };

export class AiStreamError extends Error {
  status: number;
  detail: unknown;
  constructor(message: string, status: number, detail?: unknown) {
    super(message);
    this.status = status;
    this.detail = detail ?? message;
  }
}

/**
 * Incremental SSE frame parser: feed it chunks (frames may be split anywhere), it returns
 * the complete frames' decoded `data` payloads. Comment lines (keep-alives) are ignored;
 * multi-line `data:` fields are joined with newlines.
 */
export class SseParser {
  private buffer = "";
  /** A trailing `\r` held back: it may be the first half of a `\r\n` split across chunks. */
  private pendingCr = false;

  push(chunk: string): AiStreamEvent[] {
    let raw = (this.pendingCr ? "\r" : "") + chunk;
    this.pendingCr = raw.endsWith("\r");
    if (this.pendingCr) raw = raw.slice(0, -1);
    this.buffer += raw.replace(/\r\n?/g, "\n");
    const out: AiStreamEvent[] = [];
    let end = this.buffer.indexOf("\n\n");
    while (end >= 0) {
      const frame = this.buffer.slice(0, end);
      this.buffer = this.buffer.slice(end + 2);
      const event = parseFrame(frame);
      if (event) out.push(event);
      end = this.buffer.indexOf("\n\n");
    }
    return out;
  }

  /** Whatever is left when the stream ends (a final frame without a blank line). */
  flush(): AiStreamEvent[] {
    const rest = this.buffer + (this.pendingCr ? "\n" : "");
    this.buffer = "";
    this.pendingCr = false;
    const event = rest.trim() ? parseFrame(rest) : null;
    return event ? [event] : [];
  }
}

export function parseFrame(frame: string): AiStreamEvent | null {
  let name = "";
  const data: string[] = [];
  for (const line of frame.split("\n")) {
    if (!line || line.startsWith(":")) continue;
    const colon = line.indexOf(":");
    const field = colon < 0 ? line : line.slice(0, colon);
    let value = colon < 0 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") name = value;
    else if (field === "data") data.push(value);
  }
  if (!data.length) return null;
  try {
    const parsed: unknown = JSON.parse(data.join("\n"));
    if (!parsed || typeof parsed !== "object") return null;
    const event = parsed as { type?: unknown };
    if (typeof event.type !== "string") {
      if (!name) return null;
      return { ...(parsed as object), type: name } as AiStreamEvent;
    }
    return parsed as AiStreamEvent;
  } catch {
    return null; // a malformed frame never breaks the stream
  }
}

async function errorDetail(resp: Response): Promise<{ message: string; detail: unknown }> {
  try {
    const body = (await resp.json()) as { detail?: unknown };
    const detail = body?.detail ?? body;
    return { message: typeof detail === "string" ? detail : JSON.stringify(detail), detail };
  } catch {
    return { message: `${resp.status} ${resp.statusText}`, detail: resp.statusText };
  }
}

/**
 * POST `body` to an AI streaming endpoint (path relative to the API base, e.g.
 * `/ai/ask/stream`) and call `onEvent` for every event. Resolves when the stream ends;
 * rejects with an {@link AiStreamError} (FastAPI `detail`) when the request fails before
 * the stream starts, and with an `AbortError` when `signal` is aborted.
 */
export async function streamAi(
  path: string,
  body: unknown,
  { onEvent, signal }: { onEvent: (event: AiStreamEvent) => void; signal?: AbortSignal },
): Promise<void> {
  // bearer token + the CSRF marker cookie sessions need (docs/sso.md)
  const headers = authHeaders({ "Content-Type": "application/json", Accept: "text/event-stream" });
  const resp = await fetch(`${resolveApiBase()}${path}`, {
    method: "POST",
    headers,
    body: JSON.stringify(body),
    credentials: "include",
    signal: signal ?? null,
  });
  if (!resp.ok) {
    const { message, detail } = await errorDetail(resp);
    throw new AiStreamError(message, resp.status, detail);
  }
  if (!resp.body) throw new AiStreamError("the server sent no stream", resp.status);
  const reader = resp.body.pipeThrough(new TextDecoderStream()).getReader();
  const parser = new SseParser();
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      for (const event of parser.push(value)) onEvent(event);
    }
    for (const event of parser.flush()) onEvent(event);
  } finally {
    reader.releaseLock();
  }
}
