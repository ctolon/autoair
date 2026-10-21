import { Badge, Box, HStack, Spinner, Stack, Text } from "@chakra-ui/react";
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { LuCircleCheck, LuCircleX, LuShieldAlert } from "react-icons/lu";

import type { Usage } from "../../api/gen/types.gen";
import { MarkdownPreview } from "../../widgets/markdown";
import { AiStreamError, type AiStreamEvent, streamAi } from "../lib/aiStream";
import { UsageLine } from "./AiFindings";

export type ToolRun = {
  id: string;
  name: string;
  args: string;
  state: "running" | "ok" | "failed";
  preview?: string;
  durationMs?: number;
};

export type AiStreamState = {
  running: boolean;
  text: string;
  stage: string | null;
  stageMessage: string | null;
  stages: string[];
  tools: ToolRun[];
  guardrails: string[];
  usage: Usage | null;
  result: { kind: string; data: unknown } | null;
  error: string | null;
  errorStatus: number | null;
  cancelled: boolean;
  /** The stream ended without a `done` event (connection dropped, proxy timeout, …). */
  interrupted: boolean;
};

const INITIAL: AiStreamState = {
  running: false,
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

function argsPreview(args: Record<string, unknown> | undefined): string {
  try {
    const text = JSON.stringify(args ?? {});
    return text.length > 120 ? `${text.slice(0, 117)}…` : text;
  } catch {
    return "";
  }
}

/** Pure reducer (exported for tests): applies one stream event to the view state. */
export function applyEvent(s: AiStreamState, e: AiStreamEvent): AiStreamState {
  switch (e.type) {
    case "status":
      return {
        ...s,
        // A new stage is a new model turn: keep its text in its own paragraph.
        text: s.text && !s.text.endsWith("\n\n") ? `${s.text}\n\n` : s.text,
        stage: e.stage,
        stageMessage: e.message ?? null,
        stages: s.stages.includes(e.stage) ? s.stages : [...s.stages, e.stage],
      };
    case "token":
      return { ...s, text: s.text + (typeof e.text === "string" ? e.text : "") };
    case "tool_call":
      return {
        ...s,
        tools: [
          ...s.tools,
          { id: String(e.id), name: String(e.name), args: argsPreview(e.arguments), state: "running" },
        ],
      };
    case "tool_result": {
      let matched = false;
      const tools = s.tools.map((t) => {
        if (matched || t.id !== e.id || t.state !== "running") return t;
        matched = true;
        return {
          ...t,
          state: e.ok ? ("ok" as const) : ("failed" as const),
          preview: e.preview,
          durationMs: e.duration_ms,
        };
      });
      return { ...s, tools };
    }
    case "guardrail":
      return { ...s, guardrails: [...new Set([...s.guardrails, `${e.guardrail}: ${e.action}`])] };
    case "usage":
      return { ...s, usage: e.usage ?? s.usage };
    case "result":
      return { ...s, result: { kind: e.kind, data: e.data } };
    case "error":
      return { ...s, error: e.message || "error", errorStatus: e.status ?? null };
    case "done":
      return { ...s, running: false, cancelled: !!e.cancelled };
    default:
      return s;
  }
}

/**
 * State once the stream body has ended (exported for tests): without a `done` event and
 * without an error the stream was cut off, so it is flagged as interrupted.
 */
export function streamEnded(s: AiStreamState, sawDone: boolean): AiStreamState {
  if (sawDone || s.error || s.cancelled) return { ...s, running: false };
  return { ...s, running: false, interrupted: true };
}

/** Runs an AI streaming request; aborts it on `cancel()` and when the component unmounts. */
export function useAiStream() {
  const [state, setState] = useState<AiStreamState>(INITIAL);
  const controller = useRef<AbortController | null>(null);

  const cancel = useCallback(() => {
    if (!controller.current) return;
    controller.current.abort();
    controller.current = null;
    setState((s) => ({ ...s, running: false, cancelled: true }));
  }, []);

  const start = useCallback(async (path: string, body: unknown) => {
    controller.current?.abort();
    const ctrl = new AbortController();
    controller.current = ctrl;
    setState({ ...INITIAL, running: true });
    let sawDone = false;
    try {
      await streamAi(path, body, {
        signal: ctrl.signal,
        onEvent: (event) => {
          if (event.type === "done") sawDone = true;
          if (controller.current === ctrl) setState((s) => applyEvent(s, event));
        },
      });
      if (controller.current === ctrl && !ctrl.signal.aborted) setState((s) => streamEnded(s, sawDone));
    } catch (err) {
      if (ctrl.signal.aborted) return;
      const status = err instanceof AiStreamError ? err.status : null;
      const message = err instanceof Error ? err.message : String(err);
      setState((s) => ({ ...s, error: message, errorStatus: status }));
    } finally {
      if (controller.current === ctrl) {
        controller.current = null;
        setState((s) => ({ ...s, running: false }));
      }
    }
  }, []);

  useEffect(() => () => controller.current?.abort(), []);
  return { state, start, cancel };
}

/**
 * The polite live-region text (exported for tests): the current stage while running and
 * the final state afterwards. Streamed text is deliberately excluded.
 */
export function streamAnnouncement(state: AiStreamState, stageLabel: string, t: (key: string) => string): string {
  if (state.running) return stageLabel;
  if (state.cancelled) return t("ai.stream.cancelled");
  if (state.interrupted) return t("ai.stream.interrupted");
  if (state.error) return "";
  if (state.result || state.text) return t("ai.stream.finished");
  return "";
}

/** Live progress of a stream: stage, tool calls, guardrail notes and streamed text. */
export function AiStreamProgress({
  state,
  showText = true,
  markdown = true,
  showUsage = true,
}: {
  state: AiStreamState;
  showText?: boolean;
  markdown?: boolean;
  /** Off when the final result renders its own usage line. */
  showUsage?: boolean;
}) {
  const { t } = useTranslation("lifecycle");
  const stageLabel = state.stage ? t(`ai.stream.stage.${state.stage}`, { defaultValue: state.stage }) : null;
  return (
    <Stack gap={2} data-testid="ai-stream">
      {/* Screen readers hear status changes only, never every streamed token. */}
      <Text srOnly aria-live="polite" aria-atomic="true" data-testid="ai-stream-status">
        {streamAnnouncement(state, stageLabel ?? t("ai.thinking"), t)}
      </Text>
      {state.running ? (
        <HStack gap={2} color="fg.muted" fontSize="sm">
          <Spinner size="xs" />
          <Text>{stageLabel ?? t("ai.thinking")}</Text>
          {state.stageMessage && state.stageMessage !== stageLabel ? (
            <Text fontSize="xs" color="fg.subtle" truncate>
              {state.stageMessage}
            </Text>
          ) : null}
        </HStack>
      ) : null}
      {state.tools.length ? (
        <Stack gap={0.5} borderWidth="1px" rounded="md" p={2} bg="bg.subtle" maxH="160px" overflowY="auto">
          <Text fontSize="2xs" fontWeight="semibold" color="fg.muted" textTransform="uppercase">
            {t("ai.stream.tools", { count: state.tools.length })}
          </Text>
          {state.tools.map((tool, i) => (
            <HStack key={`${tool.id}-${i}`} gap={1.5} fontSize="xs" minW={0}>
              {tool.state === "running" ? (
                <Spinner size="xs" color="blue.500" />
              ) : tool.state === "ok" ? (
                <Box color="green.500" flexShrink={0}>
                  <LuCircleCheck />
                </Box>
              ) : (
                <Box color="red.500" flexShrink={0}>
                  <LuCircleX />
                </Box>
              )}
              <Text fontFamily="mono" fontWeight="semibold" flexShrink={0}>
                {tool.name}
              </Text>
              <Text fontFamily="mono" color="fg.muted" truncate flex="1" minW={0} title={tool.args}>
                {tool.args}
              </Text>
              {tool.durationMs != null ? (
                <Text color="fg.subtle" flexShrink={0}>
                  {tool.durationMs} ms
                </Text>
              ) : null}
            </HStack>
          ))}
        </Stack>
      ) : null}
      {state.guardrails.length ? (
        <HStack gap={1} wrap="wrap">
          <Box color="orange.500">
            <LuShieldAlert />
          </Box>
          {state.guardrails.map((g) => (
            <Badge key={g} size="xs" colorPalette="orange" variant="subtle">
              {g}
            </Badge>
          ))}
        </HStack>
      ) : null}
      {showText && state.text ? (
        <Box fontSize="sm" borderLeftWidth="2px" borderColor="blue.400" pl={3} data-testid="ai-stream-text">
          {markdown ? <MarkdownPreview text={state.text} /> : <Text whiteSpace="pre-wrap">{state.text}</Text>}
        </Box>
      ) : null}
      {state.cancelled ? (
        <Text fontSize="xs" color="fg.muted">
          {t("ai.stream.cancelled")}
        </Text>
      ) : null}
      {state.interrupted ? (
        <Box borderWidth="1px" borderColor="orange.400" bg="orange.subtle" rounded="md" p={2}>
          <Text fontSize="sm" data-testid="ai-stream-interrupted">
            {t("ai.stream.interrupted")}
          </Text>
        </Box>
      ) : null}
      {state.error ? (
        <Box borderWidth="1px" borderColor="red.400" bg="red.subtle" rounded="md" p={2} role="alert">
          <Text fontSize="sm" color="fg.error">
            {state.error}
          </Text>
          {state.errorStatus === 428 ? (
            <Text fontSize="xs" color="fg.muted" mt={1}>
              {t("ai.myModel.needed")}
            </Text>
          ) : null}
        </Box>
      ) : null}
      {showUsage && state.usage && !state.running ? <UsageLine usage={state.usage} /> : null}
    </Stack>
  );
}
