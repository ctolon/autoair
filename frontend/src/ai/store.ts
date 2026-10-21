import { create } from "zustand";

import { getAssistantThread } from "../api/gen/sdk.gen";
import type { ChatMessage, ChatThread, ThreadSummary } from "../api/gen/types.gen";
import { errorMessage } from "../api/setup";
import { type AiStreamState, applyEvent, streamEnded } from "../lifecycle/components/AiStream";
import { AiStreamError, streamAi } from "../lifecycle/lib/aiStream";

export type AssistantMode = "developer" | "admin";

/** Result of `POST /ai/assistant/chat/stream` (kind `assistant_turn`). */
type ChatTurnResult = { thread: ThreadSummary; user_message: ChatMessage; message: ChatMessage };

type PendingTurn = {
  userText: string;
  regenerate: boolean;
  stream: AiStreamState;
};

const EMPTY_STREAM: AiStreamState = {
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

export type TroubleshootTarget = {
  dagId: string;
  runId: string;
  taskId?: string | null;
  mapIndex?: number;
  tryNumber?: number | null;
};

type ChatState = {
  open: boolean;
  expanded: boolean;
  threadsOpen: boolean;
  mode: AssistantMode;
  threadId: string | null;
  messages: ChatMessage[];
  title: string | null;
  loading: boolean;
  pending: PendingTurn | null;
  error: string | null;
  draft: string;
  /** Bumped when the thread list changed (new turn, rename, delete). */
  threadsVersion: number;
  troubleshoot: TroubleshootTarget | null;
  upgradeOpen: boolean;

  openPanel: (opts?: { mode?: AssistantMode; prompt?: string }) => void;
  close: () => void;
  toggle: () => void;
  setExpanded: (v: boolean) => void;
  setThreadsOpen: (v: boolean) => void;
  setMode: (mode: AssistantMode) => void;
  setDraft: (text: string) => void;
  newChat: () => void;
  selectThread: (id: string, mode?: AssistantMode) => Promise<void>;
  reload: () => Promise<void>;
  send: (text: string, ctx: SendContext) => Promise<void>;
  regenerate: (ctx: SendContext) => Promise<void>;
  stop: () => void;
  replaceMessage: (message: ChatMessage) => void;
  threadsChanged: () => void;
  openTroubleshoot: (target: TroubleshootTarget) => void;
  closeTroubleshoot: () => void;
  setUpgradeOpen: (v: boolean) => void;
};

export type SendContext = {
  /** The DAG open in the editor (developer mode). */
  spec?: unknown;
  model?: string | null;
};

let controller: AbortController | null = null;

/** Messages after a finished turn: the stored user + assistant message replace the pending ones. */
export function mergeTurn(messages: ChatMessage[], result: ChatTurnResult, regenerate: boolean): ChatMessage[] {
  const base = [...messages];
  if (regenerate) {
    while (base.length && base[base.length - 1]?.role === "assistant") base.pop();
    return [...base, result.message];
  }
  return [...base, result.user_message, result.message];
}

export const useAiChat = create<ChatState>()((set, get) => {
  async function run(body: Record<string, unknown>, userText: string, regenerate: boolean) {
    controller?.abort();
    const ctrl = new AbortController();
    controller = ctrl;
    set({ pending: { userText, regenerate, stream: { ...EMPTY_STREAM } }, error: null });
    let sawDone = false;
    let turn: ChatTurnResult | null = null;
    try {
      await streamAi("/ai/assistant/chat/stream", body, {
        signal: ctrl.signal,
        onEvent: (event) => {
          if (controller !== ctrl) return;
          if (event.type === "done") sawDone = true;
          if (event.type === "status" && event.stage === "thread" && event.message && !get().threadId) {
            set({ threadId: event.message });
          }
          if (event.type === "result" && event.kind === "assistant_turn") turn = event.data as ChatTurnResult;
          set((s) => (s.pending ? { pending: { ...s.pending, stream: applyEvent(s.pending.stream, event) } } : {}));
        },
      });
      if (controller !== ctrl) return;
      const finished = turn as ChatTurnResult | null;
      const pending = get().pending;
      const ended = pending ? streamEnded(pending.stream, sawDone) : null;
      if (finished) {
        set((s) => ({
          messages: mergeTurn(s.messages, finished, regenerate),
          threadId: finished.thread.id,
          title: finished.thread.title,
          pending: null,
          threadsVersion: s.threadsVersion + 1,
        }));
      } else {
        set({ pending: null, error: ended?.error ?? (ended?.interrupted ? "interrupted" : null) });
        await get().reload();
      }
    } catch (err) {
      if (ctrl.signal.aborted) return;
      const message = err instanceof AiStreamError ? err.message : errorMessage(err);
      set({ pending: null, error: message, draft: regenerate ? get().draft : userText });
    } finally {
      if (controller === ctrl) controller = null;
    }
  }

  return {
    open: false,
    expanded: false,
    threadsOpen: false,
    mode: "developer",
    threadId: null,
    messages: [],
    title: null,
    loading: false,
    pending: null,
    error: null,
    draft: "",
    threadsVersion: 0,
    troubleshoot: null,
    upgradeOpen: false,

    openPanel: (opts) => {
      const mode = opts?.mode ?? get().mode;
      if (mode !== get().mode) get().setMode(mode);
      set({ open: true, ...(opts?.prompt ? { draft: opts.prompt } : {}) });
    },
    close: () => set({ open: false }),
    toggle: () => set((s) => ({ open: !s.open })),
    setExpanded: (expanded) => set({ expanded }),
    setThreadsOpen: (threadsOpen) => set({ threadsOpen }),
    setMode: (mode) => {
      if (mode === get().mode) return;
      get().stop();
      set({ mode, threadId: null, messages: [], title: null, error: null });
    },
    setDraft: (draft) => set({ draft }),
    newChat: () => {
      get().stop();
      set({ threadId: null, messages: [], title: null, error: null, threadsOpen: false });
    },
    selectThread: async (id, mode) => {
      get().stop();
      set({ threadId: id, loading: true, error: null, threadsOpen: false, ...(mode ? { mode } : {}) });
      try {
        const { data } = await getAssistantThread({ path: { thread_id: id }, throwOnError: true });
        if (get().threadId !== id) return;
        set({ messages: data.messages ?? [], title: data.title ?? null, mode: data.mode, loading: false });
      } catch (err) {
        set({ loading: false, error: errorMessage(err), messages: [] });
      }
    },
    reload: async () => {
      const id = get().threadId;
      if (!id) return;
      try {
        const { data } = await getAssistantThread({ path: { thread_id: id }, throwOnError: true });
        if (get().threadId === id) set({ messages: (data as ChatThread).messages ?? [], title: data.title ?? null });
      } catch {
        /* the thread may not exist yet (request refused before it was stored) */
      }
    },
    send: async (text, ctx) => {
      const message = text.trim();
      if (!message || get().pending) return;
      const { mode, threadId } = get();
      set({ draft: "" });
      await run(
        {
          mode,
          thread_id: threadId,
          message,
          ...(mode === "developer" && ctx.spec ? { spec: ctx.spec } : {}),
          ...(ctx.model ? { model: ctx.model } : {}),
        },
        message,
        false,
      );
    },
    regenerate: async (ctx) => {
      const { mode, threadId, messages } = get();
      if (!threadId || get().pending) return;
      const lastUser = [...messages].reverse().find((m) => m.role === "user");
      if (!lastUser) return;
      set((s) => {
        const kept = [...s.messages];
        while (kept.length && kept[kept.length - 1]?.role === "assistant") kept.pop();
        // The user message stays visible; the pending turn shows only the new answer.
        return { messages: kept.slice(0, -1) };
      });
      await run(
        {
          mode,
          thread_id: threadId,
          regenerate: true,
          ...(mode === "developer" && ctx.spec ? { spec: ctx.spec } : {}),
          ...(ctx.model ? { model: ctx.model } : {}),
        },
        lastUser.content,
        false,
      );
      // The server replaced the answer: show exactly what is stored.
      await get().reload();
    },
    stop: () => {
      if (!controller) return;
      controller.abort();
      controller = null;
      const pending = get().pending;
      set({ pending: null });
      if (pending) {
        // The server stores the cancelled turn; show it once it is written.
        setTimeout(() => void get().reload(), 400);
      }
    },
    replaceMessage: (message) => set((s) => ({ messages: s.messages.map((m) => (m.id === message.id ? message : m)) })),
    threadsChanged: () => set((s) => ({ threadsVersion: s.threadsVersion + 1 })),
    openTroubleshoot: (troubleshoot) => set({ troubleshoot }),
    closeTroubleshoot: () => set({ troubleshoot: null }),
    setUpgradeOpen: (upgradeOpen) => set({ upgradeOpen }),
  };
});
