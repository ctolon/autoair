import {
  LSPClient,
  LSPPlugin,
  type Transport,
  Workspace,
  type WorkspaceFile,
  languageServerExtensions,
} from "@codemirror/lsp-client";
import type { Extension, Text } from "@codemirror/state";
import { type EditorView, ViewPlugin } from "@codemirror/view";

import { lspServers, lspSession } from "../../api/gen/sdk.gen";
import { resolveApiBase } from "../../api/setup";
import { openPeek } from "./lspPeek";
import type { PythonLspContext } from "./types";

/**
 * IDE-grade language services (`features.ide_language_server`): the editor speaks LSP to a
 * per-session language server on the AutoAir host (basedpyright or pylsp, bash-language-server)
 * through `/api/lsp/ws/{language}`. One connection per language and page, shared by every
 * editor; documents use virtual `file:///autoair/ws/…` URIs that the server maps to its
 * workspace (a task body travels with its signature in the query, so the server analyses it
 * wrapped in the generated function).
 */
export type LspLanguage = "python" | "bash";

const ROOT = "file:///autoair/ws/";
const REQUEST_TIMEOUT = 15_000;
const MAX_RECONNECTS = 3;

let nextDocument = 1;

/** A unique virtual URI for one editor's document; task bodies carry their signature. */
export function documentUri(language: LspLanguage, context?: PythonLspContext | null, id = nextDocument++): string {
  if (language === "bash") return `${ROOT}command_${id}.sh`;
  const mode = context?.mode ?? "task_body";
  if (!context || mode === "module") return `${ROOT}module_${id}.py`;
  const query = new URLSearchParams({ mode: "task_body" });
  const args = context.arguments.filter((a) => /^\*{0,2}[A-Za-z_]\w*$/.test(a));
  if (args.length) query.set("args", args.join(","));
  if (context.usesLoopContext) query.set("loop", "1");
  if (/^[A-Za-z_]\w*$/.test(context.functionName)) query.set("fn", context.functionName);
  return `${ROOT}${encodeURIComponent(context.functionName || "task")}_${id}.py?${query.toString()}`;
}

/** WebSocket URL of a language server below the API base (relative bases use this page's host). */
export function socketUrl(apiBase: string, path: string, ticket: string, location: Location = window.location): string {
  const base = new URL(apiBase.replace(/\/$/, "") + path, location.href);
  base.protocol = base.protocol === "https:" ? "wss:" : "ws:";
  base.searchParams.set("ticket", ticket);
  return base.toString();
}

/** Messages sent before the socket opens are queued; a closed socket reports through `onClose`. */
class SocketTransport implements Transport {
  private handlers: ((value: string) => void)[] = [];
  private queue: string[] = [];
  constructor(
    private readonly socket: WebSocket,
    onClose: () => void,
  ) {
    socket.addEventListener("message", (e) => {
      if (typeof e.data === "string") for (const h of this.handlers) h(e.data);
    });
    socket.addEventListener("open", () => {
      for (const m of this.queue.splice(0)) socket.send(m);
    });
    socket.addEventListener("close", onClose);
  }
  send(message: string): void {
    // A closed socket drops messages: the client reconnects and re-opens its documents.
    if (this.socket.readyState === WebSocket.CONNECTING) this.queue.push(message);
    else if (this.socket.readyState === WebSocket.OPEN) this.socket.send(message);
  }
  subscribe(handler: (value: string) => void): void {
    this.handlers.push(handler);
  }
  unsubscribe(handler: (value: string) => void): void {
    this.handlers = this.handlers.filter((h) => h !== handler);
  }
  close(): void {
    this.socket.close();
  }
}

class EditorFile implements WorkspaceFile {
  constructor(
    readonly uri: string,
    readonly languageId: string,
    public version: number,
    public doc: Text,
    readonly view: EditorView,
  ) {}
  getView(): EditorView {
    return this.view;
  }
}

/**
 * One file per editor; definitions in library files open read-only in a peek panel. While
 * disconnected, edits accumulate in the editors (nothing is synced) and the files are opened
 * again — at their last synced version — when the client reconnects.
 */
class AutoAirWorkspace extends Workspace {
  files: EditorFile[] = [];
  live = false;
  override connected() {
    this.live = true;
    for (const file of this.files) this.client.didOpen(file);
  }
  override disconnected() {
    this.live = false;
  }
  syncFiles() {
    const result: ReturnType<Workspace["syncFiles"]>[number][] = [];
    if (!this.live) return result;
    for (const file of this.files) {
      const plugin = LSPPlugin.get(file.view);
      if (!plugin || plugin.unsyncedChanges.empty) continue;
      result.push({ changes: plugin.unsyncedChanges, file, prevDoc: file.doc });
      file.doc = file.view.state.doc;
      file.version++;
      plugin.clear();
    }
    return result;
  }
  openFile(uri: string, languageId: string, view: EditorView) {
    if (this.getFile(uri)) return;
    const file = new EditorFile(uri, languageId, 0, view.state.doc, view);
    this.files.push(file);
    if (this.live) this.client.didOpen(file);
  }
  closeFile(uri: string) {
    const file = this.getFile(uri);
    if (!file) return;
    this.files = this.files.filter((f) => f !== file);
    if (this.live) this.client.didClose(uri);
  }
  override displayFile(uri: string): Promise<EditorView | null> {
    const open = this.getFile(uri);
    if (open) return Promise.resolve(open.getView());
    return this.client
      .request<{ uri: string }, string | null>("autoair/readFile", { uri })
      .then((text) => (text == null ? null : openPeek(uri, text)))
      .catch(() => null);
  }
}

type Connection = { client: LSPClient; transport: SocketTransport | null; failures: number; connecting: boolean };
const connections = new Map<LspLanguage, Connection>();

async function connect(language: LspLanguage, conn: Connection): Promise<void> {
  if (conn.transport || conn.connecting || conn.failures >= MAX_RECONNECTS) return;
  conn.connecting = true;
  try {
    const { data } = await lspSession({ body: { language }, throwOnError: true });
    const socket = new WebSocket(socketUrl(resolveApiBase(), data.path, data.ticket));
    const transport = new SocketTransport(socket, () => {
      if (conn.transport !== transport) return;
      conn.transport = null;
      conn.client.disconnect();
    });
    conn.transport = transport;
    socket.addEventListener("open", () => {
      conn.failures = 0;
    });
    socket.addEventListener("error", () => {
      conn.failures++;
    });
    conn.client.connect(transport);
  } catch {
    conn.failures++;
  } finally {
    conn.connecting = false;
  }
}

/** The shared client of `language` (connected lazily; reconnects after an idle close). */
function languageClient(language: LspLanguage): LSPClient {
  let conn = connections.get(language);
  if (!conn) {
    const client = new LSPClient({
      rootUri: ROOT,
      timeout: REQUEST_TIMEOUT,
      workspace: (c) => new AutoAirWorkspace(c),
      extensions: languageServerExtensions(),
    });
    conn = { client, transport: null, failures: 0, connecting: false };
    connections.set(language, conn);
  }
  void connect(language, conn);
  return conn.client;
}

/** Reconnects (after an idle timeout) when the user works in an editor again. */
function keepAlive(language: LspLanguage): Extension {
  return ViewPlugin.define(() => ({
    update(u) {
      if (!u.docChanged && !u.focusChanged) return;
      const conn = connections.get(language);
      if (conn && !conn.transport) void connect(language, conn);
    },
  }));
}

/**
 * Editor extension: completion (with resolve), hover, signature help, diagnostics,
 * go-to-definition (F12; library files open in a peek panel), find references (Shift-F12),
 * rename (F2) and formatting (Shift-Alt-F) from the language server.
 */
export function languageServerExtension(language: LspLanguage, uri: string): Extension {
  const client = languageClient(language);
  return [client.plugin(uri, language === "bash" ? "shellscript" : "python"), keepAlive(language)];
}

let status: Promise<Record<LspLanguage, boolean>> | null = null;

/** Which language servers the server has installed (asked once per page). */
export function serverAvailability(): Promise<Record<LspLanguage, boolean>> {
  status ??= lspServers({ throwOnError: true })
    .then(({ data }) => ({ python: data.python.available, bash: data.bash.available }))
    .catch(() => ({ python: false, bash: false }));
  return status;
}
