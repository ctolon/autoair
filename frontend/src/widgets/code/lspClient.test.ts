import { describe, expect, it } from "vitest";

import { documentUri, socketUrl } from "./lspClient";
import { peekTitle } from "./lspPeek";
import { serverBoost } from "./pythonLsp";

describe("language server client helpers", () => {
  it("builds virtual document URIs carrying a task body's signature", () => {
    const uri = documentUri(
      "python",
      { functionName: "extract", arguments: ["ti", "rows", "bad name", "**ctx"], usesLoopContext: true },
      7,
    );
    const url = new URL(uri);
    expect(url.protocol).toBe("file:");
    expect(url.pathname).toBe("/autoair/ws/extract_7.py");
    expect(url.searchParams.get("mode")).toBe("task_body");
    expect(url.searchParams.get("args")).toBe("ti,rows,**ctx");
    expect(url.searchParams.get("loop")).toBe("1");
    expect(url.searchParams.get("fn")).toBe("extract");
    expect(documentUri("python", { functionName: "", arguments: [], usesLoopContext: false, mode: "module" }, 2)).toBe(
      "file:///autoair/ws/module_2.py",
    );
    expect(documentUri("bash", null, 3)).toBe("file:///autoair/ws/command_3.sh");
  });

  it("derives the WebSocket URL from the API base", () => {
    const loc = new URL("https://airflow.example/plugin/dag-builder") as unknown as Location;
    expect(socketUrl("/autoair/api", "/lsp/ws/python", "a.b", loc)).toBe(
      "wss://airflow.example/autoair/api/lsp/ws/python?ticket=a.b",
    );
    const local = new URL("http://localhost:5173/") as unknown as Location;
    expect(socketUrl("/api/", "/lsp/ws/bash", "t", local)).toBe("ws://localhost:5173/api/lsp/ws/bash?ticket=t");
  });

  it("titles peeks by their import path", () => {
    expect(peekTitle("file:///venv/lib/python3.12/site-packages/airflow/sdk/__init__.py")).toBe(
      "airflow/sdk/__init__.py",
    );
    expect(peekTitle("file:///usr/lib/python3.12/json/__init__.py")).toBe("json/__init__.py");
  });

  it("keeps the server's completion ranking", () => {
    expect(serverBoost(0)).toBeGreaterThan(serverBoost(1));
    expect(serverBoost(500)).toBe(-99);
  });
});
