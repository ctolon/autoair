import { Badge, Box, Button, HStack, RadioGroup, Stack, Text } from "@chakra-ui/react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { LuPlay, LuX } from "react-icons/lu";

import { decideTroubleshootAction } from "../api/gen/sdk.gen";
import type { ActionResult, AirflowAction, TroubleshootAnalysis } from "../api/gen/types.gen";
import { errorMessage } from "../api/setup";
import { useAccess } from "../lib/access";
import { toaster } from "../ui/toaster";

export type ActionsMode = "read_only" | "hitl" | "apply";
const ACTION_MODES: readonly ActionsMode[] = ["read_only", "hitl", "apply"];

/** The modes a user may pick: the server's (per-caller) mode and every stricter one. */
export function modesUpTo(max: ActionsMode | null | undefined): ActionsMode[] {
  const rank = ACTION_MODES.indexOf(max ?? "read_only");
  return ACTION_MODES.slice(0, Math.max(0, rank) + 1);
}

/** A requested mode, never more permissive than ``max``. */
export function clampMode(mode: ActionsMode | null | undefined, max: ActionsMode | null | undefined): ActionsMode {
  const allowed = modesUpTo(max);
  return mode && allowed.includes(mode) ? mode : (allowed[allowed.length - 1] ?? "read_only");
}

/** Whether the Execute button is offered for ``action`` (the server re-checks everything). */
export function canExecute(action: AirflowAction, mode: ActionsMode, canManage: boolean): boolean {
  return mode === "hitl" && canManage && (action.status === "proposed" || action.status === "failed");
}

type Translate = (key: string, options?: Record<string, unknown>) => string;

/**
 * A result in the user's language: the server's message code (`troubleshoot.result.<code>`
 * with its params) when this UI knows it, else the server's English text.
 */
export function resultText(t: Translate, result: ActionResult): string {
  const params = result.params ?? {};
  const fallback = result.message ?? "";
  if (result.code === "blocked" && typeof params.reason === "string")
    return t(`troubleshoot.blocked.${params.reason}`, { defaultValue: fallback });
  if (result.code) return t(`troubleshoot.result.${result.code}`, { ...params, defaultValue: fallback });
  return fallback;
}

/** The structured 409 of a second executor (`{code: "action_running" | ...}`). */
function busyCode(err: unknown): string | null {
  const detail = err && typeof err === "object" && "detail" in err ? (err as { detail: unknown }).detail : null;
  const code = detail && typeof detail === "object" ? (detail as { code?: unknown }).code : null;
  return typeof code === "string" && code.startsWith("action_") ? code : null;
}

type LogEntry = { action: AirflowAction; result: ActionResult };

/** Every execution result of an analysis, oldest first. */
export function actionLog(actions: readonly AirflowAction[] | null | undefined): LogEntry[] {
  const out: LogEntry[] = [];
  for (const action of actions ?? []) for (const result of action.results ?? []) out.push({ action, result });
  return out.sort((a, b) => (a.result.at ?? "").localeCompare(b.result.at ?? ""));
}

export function actionTarget(action: AirflowAction): string {
  const idx = action.map_index != null && action.map_index >= 0 ? `[${action.map_index}]` : "";
  if (action.task_id) return `${action.task_id}${idx}`;
  if (action.status === "instruction" && action.target) return action.target;
  return action.run_id ?? action.target ?? "";
}

const STATUS_PALETTE: Record<string, string> = {
  proposed: "blue",
  running: "purple",
  blocked: "orange",
  rejected: "gray",
  succeeded: "green",
  skipped: "gray",
  failed: "red",
};

/** Setup: which Airflow-action mode this analysis runs in (at most the server's). */
export function ActionsModePicker({
  max,
  value,
  onChange,
}: {
  max: ActionsMode;
  value: ActionsMode;
  onChange: (m: ActionsMode) => void;
}) {
  const { t } = useTranslation("ai");
  const modes = modesUpTo(max);
  return (
    <Box data-testid="troubleshoot-actions-picker">
      <Text fontSize="sm" fontWeight="semibold" mb={1}>
        {t("troubleshoot.actionsMode")}
      </Text>
      <Text fontSize="xs" color="fg.muted" mb={2}>
        {t("troubleshoot.actionsModeHelp")}
      </Text>
      <RadioGroup.Root
        size="sm"
        value={value}
        onValueChange={(e) => e.value && onChange(clampMode(e.value as ActionsMode, max))}
      >
        <HStack gap={4} wrap="wrap">
          {ACTION_MODES.map((m) => (
            <RadioGroup.Item key={m} value={m} disabled={!modes.includes(m)} data-testid={`actions-mode-${m}`}>
              <RadioGroup.ItemHiddenInput />
              <RadioGroup.ItemIndicator />
              <RadioGroup.ItemText fontSize="sm">{t(`troubleshoot.modes.${m}`)}</RadioGroup.ItemText>
            </RadioGroup.Item>
          ))}
        </HStack>
      </RadioGroup.Root>
    </Box>
  );
}

function ActionRow({
  analysisId,
  action,
  mode,
  onUpdate,
}: {
  analysisId: string | undefined;
  action: AirflowAction;
  mode: ActionsMode;
  onUpdate: (a: TroubleshootAnalysis) => void;
}) {
  const { t } = useTranslation("ai");
  const access = useAccess();
  const [busy, setBusy] = useState<"execute" | "reject" | null>(null);
  const status = action.status ?? "instruction";
  const last = action.results?.length ? action.results[action.results.length - 1] : null;
  const kind = t(`troubleshoot.action.${action.action ?? "other"}`, { defaultValue: action.action ?? "" });
  const target = actionTarget(action);
  const decide = async (decision: "execute" | "reject") => {
    if (!analysisId || !action.id) return;
    setBusy(decision);
    try {
      const { data } = await decideTroubleshootAction({
        path: { analysis_id: analysisId, action_id: action.id },
        body: { decision },
        throwOnError: true,
      });
      onUpdate(data);
      if (decision === "execute") {
        const done = data.airflow_actions?.find((a) => a.id === action.id);
        const s = done?.status ?? "failed";
        toaster.create({
          type: s === "succeeded" ? "success" : s === "failed" ? "error" : "info",
          title: t("troubleshoot.executed", { status: t(`troubleshoot.actionStatus.${s}`) }),
          description: (() => {
            const result = done?.results?.at(-1);
            return result ? resultText(t, result) : undefined;
          })(),
        });
      }
    } catch (err) {
      const busy = busyCode(err);
      toaster.create({
        type: busy ? "info" : "error",
        title: busy
          ? t(`troubleshoot.busy.${busy}`, { defaultValue: errorMessage(err) })
          : t("troubleshoot.executeFailed"),
        description: busy ? undefined : errorMessage(err),
      });
    } finally {
      setBusy(null);
    }
  };
  const flags = [
    action.include_downstream ? t("troubleshoot.downstream") : null,
    action.include_upstream ? t("troubleshoot.upstream") : null,
    action.only_failed ? t("troubleshoot.onlyFailed") : null,
  ].filter(Boolean);
  return (
    <Box
      borderWidth={status === "instruction" ? undefined : "1px"}
      rounded="md"
      p={status === "instruction" ? 0 : 2}
      data-testid="troubleshoot-action"
      data-status={status}
    >
      <HStack gap={2} align="start" fontSize="sm" justify="space-between" wrap="wrap">
        <HStack gap={2} align="start" minW={0} flex="1">
          <Badge size="xs" variant="outline" flexShrink={0}>
            {kind}
          </Badge>
          <Text minW={0} wordBreak="break-word">
            {target ? (
              <Text as="span" fontFamily="mono" fontSize="xs">
                {target}
                {flags.length ? ` (${flags.join(", ")})` : ""}:{" "}
              </Text>
            ) : null}
            {action.instructions}
          </Text>
        </HStack>
        <HStack gap={1.5} flexShrink={0}>
          {status !== "instruction" ? (
            <Badge size="xs" colorPalette={STATUS_PALETTE[status] ?? "gray"} data-testid="troubleshoot-action-status">
              {t(`troubleshoot.actionStatus.${status}`)}
            </Badge>
          ) : null}
          {canExecute(action, mode, access.can("run:manage")) ? (
            <>
              <Button
                size="2xs"
                variant="ghost"
                loading={busy === "reject"}
                disabled={busy !== null}
                onClick={() => void decide("reject")}
                data-testid="troubleshoot-action-dismiss"
              >
                <LuX /> {t("troubleshoot.dismiss")}
              </Button>
              <Button
                size="2xs"
                colorPalette="blue"
                loading={busy === "execute"}
                disabled={busy !== null}
                title={t("troubleshoot.executeConfirm", { action: kind, target })}
                onClick={() => void decide("execute")}
                data-testid="troubleshoot-action-execute"
              >
                <LuPlay /> {t("troubleshoot.execute")}
              </Button>
            </>
          ) : null}
        </HStack>
      </HStack>
      {action.blocked && status === "blocked" ? (
        <Text fontSize="2xs" color="fg.warning" mt={1}>
          {t(`troubleshoot.blocked.${action.blocked}`)}
        </Text>
      ) : null}
      {last ? (
        <Text fontSize="2xs" color={last.status === "failed" ? "fg.error" : "fg.muted"} mt={1}>
          {resultText(t, last)}
        </Text>
      ) : null}
    </Box>
  );
}

/** The Airflow actions of a report: instructions, approvals (hitl) or the action log (apply). */
export function AirflowActions({
  analysis,
  onUpdate,
}: {
  analysis: TroubleshootAnalysis;
  onUpdate: (a: TroubleshootAnalysis) => void;
}) {
  const { t } = useTranslation("ai");
  const actions = analysis.airflow_actions ?? [];
  const mode: ActionsMode = analysis.actions_mode ?? "read_only";
  const log = actionLog(actions);
  if (!actions.length) return null;
  return (
    <Box data-testid="troubleshoot-actions">
      <HStack gap={2} mb={1}>
        <Text fontSize="sm" fontWeight="semibold">
          {t("troubleshoot.airflowActions")}
        </Text>
        <Badge
          size="xs"
          colorPalette={mode === "apply" ? "purple" : mode === "hitl" ? "blue" : "gray"}
          data-testid="troubleshoot-actions-mode"
          data-mode={mode}
        >
          {t(`troubleshoot.modeBadge.${mode}`)}
        </Badge>
      </HStack>
      <Stack gap={1.5}>
        {actions.map((a, i) => (
          <ActionRow key={a.id || i} analysisId={analysis.id} action={a} mode={mode} onUpdate={onUpdate} />
        ))}
      </Stack>
      <Text fontSize="2xs" color="fg.subtle" mt={1}>
        {t(`troubleshoot.modeHelp.${mode}`)}
      </Text>
      {log.length ? (
        <Box mt={2} data-testid="troubleshoot-action-log">
          <Text fontSize="xs" fontWeight="semibold" mb={1}>
            {t("troubleshoot.actionLog")}
          </Text>
          <Stack gap={0.5} fontSize="2xs" fontFamily="mono">
            {log.map(({ action, result }, i) => (
              <HStack key={i} gap={2} wrap="wrap">
                <Text color="fg.muted">{(result.at ?? "").replace("T", " ").slice(0, 19)}</Text>
                <Badge size="xs" colorPalette={STATUS_PALETTE[result.status] ?? "gray"}>
                  {t(`troubleshoot.actionStatus.${result.status}`)}
                </Badge>
                <Text>
                  {action.action} {actionTarget(action)}
                </Text>
                <Text color="fg.muted" wordBreak="break-word">
                  {resultText(t, result)}
                  {result.automatic ? ` (${t("troubleshoot.actionAuto")})` : ""}
                </Text>
              </HStack>
            ))}
          </Stack>
        </Box>
      ) : null}
    </Box>
  );
}
