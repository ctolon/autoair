import { WebsocketProvider } from "y-websocket";
import * as Y from "yjs";
import { create } from "zustand";

import { collabTicket } from "../api/gen/sdk.gen";
import { errorMessage, resolveApiBase } from "../api/setup";
import { useBuilder } from "../store/builder";

import { type Binding, bind } from "./binding";

type CollabStatus = "off" | "connecting" | "connected" | "error";

interface PeerState {
  clientId: number;
  name: string;
  color: string;
  selection: string[];
  cursor: { x: number; y: number } | null;
}

interface CollabState {
  status: CollabStatus;
  error: string | null;
  room: string | null;
  peers: PeerState[];
  canUndo: boolean;
  canRedo: boolean;
}

/** Observable collaboration state for the UI (the session itself lives outside React). */
export const useCollab = create<CollabState>()(() => ({
  status: "off",
  error: null,
  room: null,
  peers: [],
  canUndo: false,
  canRedo: false,
}));

const COLORS = ["#e11d48", "#2563eb", "#16a34a", "#d97706", "#7c3aed", "#0891b2", "#db2777", "#4d7c0f"];
const NAME_KEY = "autoair.collab.name";

export function displayName(): string {
  try {
    const stored = localStorage.getItem(NAME_KEY);
    if (stored) return stored;
    const name = `User-${Math.random().toString(36).slice(2, 6)}`;
    localStorage.setItem(NAME_KEY, name);
    return name;
  } catch {
    return "Guest";
  }
}

export function setDisplayName(name: string): void {
  try {
    localStorage.setItem(NAME_KEY, name);
  } catch {
    /* storage unavailable */
  }
  session?.provider.awareness.setLocalStateField("user", { name, color: session.color });
}

/** `/autoair/api` → `ws(s)://host/autoair/collab` (same origin as the API). */
export function collabServerUrl(apiBase = resolveApiBase(), origin = window.location): string {
  const path = apiBase.replace(/\/api\/?$/, "") + "/collab";
  const proto = origin.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${origin.host}${path.startsWith("/") ? "" : "/"}${path}`;
}

interface Session {
  room: string;
  doc: Y.Doc;
  provider: WebsocketProvider;
  binding: Binding | null;
  color: string;
}

let session: Session | null = null;

function publishPeers(s: Session) {
  const peers: PeerState[] = [];
  s.provider.awareness.getStates().forEach((state, clientId) => {
    if (clientId === s.doc.clientID || !state.user) return;
    const user = state.user as { name: string; color: string };
    peers.push({
      clientId,
      name: user.name,
      color: user.color,
      selection: (state.selection as string[] | undefined) ?? [],
      cursor: (state.cursor as PeerState["cursor"] | undefined) ?? null,
    });
  });
  useCollab.setState({ peers });
}

function publishUndo(s: Session) {
  const um = s.binding?.undoManager;
  useCollab.setState({ canUndo: !!um?.canUndo(), canRedo: !!um?.canRedo() });
}

/** A short-lived ticket from the (authenticated) API; needs edit permission on the DAG. */
async function fetchTicket(room: string): Promise<string> {
  const { data } = await collabTicket({ path: { dag_id: room }, throwOnError: true });
  return data.ticket;
}

/** Joins the room of the current DAG. Edits, selections and cursors are shared live. */
export async function startCollab(room = useBuilder.getState().dag.dag_id): Promise<void> {
  if (session) stopCollab();
  useCollab.setState({ status: "connecting", room, peers: [], error: null });
  let ticket: string;
  try {
    ticket = await fetchTicket(room);
  } catch (err) {
    if (useCollab.getState().room === room) useCollab.setState({ status: "error", error: errorMessage(err) });
    return;
  }
  if (useCollab.getState().status !== "connecting" || useCollab.getState().room !== room) return; // left meanwhile
  const doc = new Y.Doc();
  const provider = new WebsocketProvider(collabServerUrl(), room, doc, {
    params: { ticket },
    maxBackoffTime: 10_000,
  });
  // Tickets expire (only the handshake checks them): keep a fresh one for reconnects.
  const refresh = setInterval(() => {
    fetchTicket(room)
      .then((t) => (provider.params = { ticket: t }))
      .catch(() => undefined);
  }, 4 * 60_000);
  doc.on("destroy", () => clearInterval(refresh));
  const color = COLORS[doc.clientID % COLORS.length] ?? "#2563eb";
  const s: Session = { room, doc, provider, binding: null, color };
  session = s;

  provider.awareness.setLocalStateField("user", { name: displayName(), color });
  provider.awareness.setLocalStateField("selection", []);
  provider.awareness.on("change", ({ added }: { added: number[] }) => {
    // The relay does not replay presence to newcomers: re-announce ours when someone joins.
    if (added.some((id) => id !== doc.clientID)) {
      const local = provider.awareness.getLocalState();
      if (local) provider.awareness.setLocalState({ ...local });
    }
    publishPeers(s);
  });
  provider.on("status", ({ status }: { status: string }) => {
    if (session !== s) return;
    if (status === "connected") useCollab.setState({ status: s.binding ? "connected" : "connecting" });
    else if (status === "disconnected") useCollab.setState({ status: "connecting" });
  });
  provider.on("connection-error", () => {
    if (session === s && !s.binding) useCollab.setState({ status: "error" });
  });
  provider.on("sync", (synced: boolean) => {
    if (!synced || session !== s || s.binding) return;
    s.binding = bind(doc, useBuilder, () => stopCollab());
    s.binding.undoManager.on("stack-item-added", () => publishUndo(s));
    s.binding.undoManager.on("stack-item-popped", () => publishUndo(s));
    useCollab.setState({ status: "connected" });
  });

  // Share this tab's selection.
  const unsubscribe = useBuilder.subscribe((st, prev) => {
    if (st.nodes === prev.nodes && st.selectedId === prev.selectedId) return;
    const selection = st.nodes.filter((n) => n.selected).map((n) => n.id);
    if (st.selectedId && !selection.includes(st.selectedId)) selection.push(st.selectedId);
    const current = (provider.awareness.getLocalState()?.selection as string[] | undefined) ?? [];
    if (current.join() !== selection.join()) provider.awareness.setLocalStateField("selection", selection);
  });
  doc.on("destroy", unsubscribe);
}

export function stopCollab(): void {
  const s = session;
  session = null;
  if (!s) {
    useCollab.setState({ status: "off", room: null, peers: [], canUndo: false, canRedo: false, error: null });
    return;
  }
  s.binding?.destroy();
  s.provider.awareness.setLocalState(null);
  s.provider.destroy();
  s.doc.destroy();
  useCollab.setState({ status: "off", room: null, peers: [], canUndo: false, canRedo: false, error: null });
}

export const collabActive = (): boolean => !!session?.binding;

/** Undo/redo of this tab's own changes only (never a collaborator's). */
export function collabUndo(): void {
  session?.binding?.undoManager.undo();
}

export function collabRedo(): void {
  session?.binding?.undoManager.redo();
}

let lastCursor = 0;
/** Pointer position in flow coordinates (throttled), or null when it leaves the canvas. */
export function shareCursor(pos: { x: number; y: number } | null): void {
  if (!session) return;
  const now = Date.now();
  if (pos && now - lastCursor < 50) return;
  lastCursor = now;
  session.provider.awareness.setLocalStateField("cursor", pos);
}
