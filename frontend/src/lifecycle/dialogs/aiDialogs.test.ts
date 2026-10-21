import { describe, expect, it } from "vitest";

import type { UserLlmView } from "../../api/gen/types.gen";
import { isForbidden } from "./AiKnowledgeDialog";
import { formDirty, initialProvider } from "./AiModelDialog";
import { writerApplyTarget } from "./AiWriterDialog";

describe("writerApplyTarget", () => {
  const nodes = [
    { id: "a", type: "python_task" },
    { id: "b", type: "python_task" },
    { id: "c", type: "bash_task" },
  ];
  const ok = { ok: true, kind: "task_body" as const };

  it("applies to the node the run was started for, not the current selection", () => {
    // Started for "a"; the user has since selected "b": the result still targets "a".
    expect(writerApplyTarget(ok, "a", nodes)).toBe("a");
  });

  it("refuses when the start node is gone, changed type, or there was none", () => {
    expect(writerApplyTarget(ok, "gone", nodes)).toBeNull();
    expect(writerApplyTarget(ok, "c", nodes)).toBeNull();
    expect(writerApplyTarget(ok, null, nodes)).toBeNull();
    expect(writerApplyTarget({ ok: false, kind: "task_body" }, "a", nodes)).toBeNull();
    expect(writerApplyTarget({ ok: true, kind: "bash" }, "a", nodes)).toBeNull();
    expect(writerApplyTarget(null, "a", nodes)).toBeNull();
  });
});

describe("AiModelDialog helpers", () => {
  const conn: UserLlmView = { configured: true, provider: null, model: null, base_url: null, conn_id: "ai_my_openai" };

  it("keeps a connection-based config instead of picking the first provider", () => {
    expect(initialProvider(conn, ["openai", "anthropic"])).toBe("");
    expect(initialProvider({ ...conn, conn_id: null }, ["openai", "anthropic"])).toBe("openai");
    expect(initialProvider({ ...conn, provider: "anthropic" }, ["openai", "anthropic"])).toBe("anthropic");
  });

  it("detects unsaved edits so Test sends the form", () => {
    const form = { provider: "" as const, model: "", baseUrl: "", connId: "ai_my_openai", apiKey: "", clearKey: false };
    expect(formDirty(conn, form)).toBe(false);
    expect(formDirty(conn, { ...form, model: "gpt-x" })).toBe(true);
    expect(formDirty(conn, { ...form, apiKey: "sk-1" })).toBe(true);
    expect(formDirty(conn, { ...form, provider: "openai" })).toBe(true);
    expect(formDirty(conn, { ...form, connId: "  ai_my_openai " })).toBe(false);
  });
});

describe("AiKnowledgeDialog isForbidden", () => {
  it("uses the HTTP status, not the message text", () => {
    expect(isForbidden({ detail: "nope", status: 403 })).toBe(true);
    expect(isForbidden({ detail: "admin permission required", status: 500 })).toBe(false);
    expect(isForbidden({ detail: "you are not an admin" })).toBe(false);
    expect(isForbidden(null)).toBe(false);
  });
});
