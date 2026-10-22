import { Badge, Box, Button, Field, HStack, Stack, Text, Textarea } from "@chakra-ui/react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { LuCheck, LuShieldQuestion, LuX } from "react-icons/lu";

import { getToolCallArguments } from "../api/gen/sdk.gen";
import type { PendingToolCall, ToolApprovalView, ToolDecision } from "../api/gen/types.gen";

const KIND_PALETTE: Record<string, string> = { write: "orange", execute: "red" };

const STATUS_PALETTE: Record<string, string> = {
  pending: "orange",
  resuming: "blue",
  resolved: "green",
  expired: "gray",
  cancelled: "gray",
  failed: "red",
};

/** Characters of the card's (server-cut) arguments shown; the full view shows everything. */
const MAX_SHOWN_CHARS = 20_000;

/** Tool arguments as display text: any JSON value (or anything else) renders, never throws. */
export function formatArguments(value: unknown, limit: number = MAX_SHOWN_CHARS): string {
  if (value === undefined || value === null) return "";
  if (typeof value === "string") return value.slice(0, limit);
  let text: string;
  try {
    text = JSON.stringify(value, null, 2) ?? String(value);
  } catch {
    text = String(value);
  }
  return text.slice(0, limit);
}

export type Choice = { approved: boolean; reason: string };

/** The decisions to send: one per pending call, or null while a call is undecided. */
export function decisionsFor(
  calls: PendingToolCall[] | undefined,
  choices: Record<string, Choice | undefined>,
  maxReason = 1_000,
): ToolDecision[] | null {
  const list = calls ?? [];
  if (!list.length) return null;
  const out: ToolDecision[] = [];
  for (const call of list) {
    const choice = choices[call.id];
    if (!choice) return null;
    const reason = choice.reason.trim().slice(0, maxReason);
    out.push({
      id: call.id,
      approved: choice.approved,
      // An approval is bound to the exact arguments shown: the server refuses it otherwise.
      ...(choice.approved && call.arguments_sha256 ? { arguments_sha256: call.arguments_sha256 } : {}),
      ...(!choice.approved && reason ? { reason } : {}),
    });
  }
  return out;
}

/** The status to show: a pending approval past its expiry is shown as expired (the server
 * refuses it anyway). */
export function shownStatus(approval: ToolApprovalView | undefined | null, now = Date.now()): string {
  const status = approval?.status ?? "pending";
  const expires = Date.parse(approval?.expires_at ?? "");
  return status === "pending" && !Number.isNaN(expires) && expires <= now ? "expired" : String(status);
}

/** Whether the approval still waits for this user's decision. */
export function isOpen(approval: ToolApprovalView | undefined | null, now = Date.now()): boolean {
  return !!approval && shownStatus(approval, now) === "pending";
}

function relativeTime(iso: string | undefined, locale: string): string {
  if (!iso) return "";
  const at = Date.parse(iso);
  if (Number.isNaN(at)) return "";
  const minutes = Math.round((at - Date.now()) / 60_000);
  try {
    const rtf = new Intl.RelativeTimeFormat(locale, { numeric: "auto" });
    return Math.abs(minutes) >= 90 ? rtf.format(Math.round(minutes / 60), "hour") : rtf.format(minutes, "minute");
  } catch {
    return new Date(at).toLocaleString();
  }
}

type FullArguments = { state: "loading" } | { state: "error" } | { state: "done"; text: string; matches: boolean };

function CallView({
  approvalId,
  call,
  decision,
  choice,
  open,
  busy,
  onChoice,
}: {
  approvalId: string;
  call: PendingToolCall;
  decision: ToolDecision | undefined;
  choice: Choice | undefined;
  open: boolean;
  busy: boolean;
  onChoice: (c: Choice) => void;
}) {
  const { t } = useTranslation("ai");
  const [full, setFull] = useState<FullArguments | null>(null);
  const args = full?.state === "done" ? full.text : formatArguments(call.arguments);
  const kind = call.kind ?? "write";
  const omitted = typeof call.omitted_chars === "number" && call.omitted_chars > 0 ? call.omitted_chars : 0;
  const loadFull = async () => {
    setFull({ state: "loading" });
    try {
      const { data } = await getToolCallArguments({
        path: { approval_id: approvalId, call_id: call.id },
        throwOnError: true,
      });
      setFull({
        state: "done",
        text: formatArguments(data.arguments, Number.MAX_SAFE_INTEGER),
        matches: !call.arguments_sha256 || data.arguments_sha256 === call.arguments_sha256,
      });
    } catch {
      setFull({ state: "error" });
    }
  };
  return (
    <Box borderWidth="1px" rounded="md" p={2} bg="bg" data-testid="tool-approval-call">
      <HStack gap={2} wrap="wrap" justify="space-between">
        <HStack gap={2} minW={0} wrap="wrap">
          <Text fontFamily="mono" fontSize="xs" fontWeight="semibold" wordBreak="break-all">
            {String(call.tool ?? "")}
          </Text>
          <Badge size="xs" variant="subtle" colorPalette={KIND_PALETTE[String(kind)] ?? "gray"}>
            {t(`approval.kind.${kind}`, { defaultValue: String(kind) })}
          </Badge>
        </HStack>
        {decision ? (
          <Badge
            size="xs"
            colorPalette={decision.approved ? "green" : "red"}
            variant="surface"
            data-testid="tool-approval-decision"
          >
            {decision.approved ? <LuCheck /> : <LuX />}
            {decision.approved ? t("approval.approved") : t("approval.denied")}
          </Badge>
        ) : null}
      </HStack>
      {call.description ? (
        <Text fontSize="2xs" color="fg.muted" mt={0.5} lineClamp={2} title={call.description}>
          {call.description}
        </Text>
      ) : null}
      <Box
        as="pre"
        mt={1}
        p={1.5}
        bg="bg.subtle"
        rounded="sm"
        fontSize="2xs"
        fontFamily="mono"
        maxH={full?.state === "done" ? "80" : "40"}
        overflow="auto"
        whiteSpace="pre-wrap"
        wordBreak="break-all"
        data-testid="tool-approval-arguments"
      >
        {args || t("approval.noArguments")}
      </Box>
      {call.truncated ? (
        <Stack gap={0.5} mt={0.5} data-testid="tool-approval-truncated">
          <Text fontSize="2xs" color={full?.state === "done" ? "fg.subtle" : "fg.warning"}>
            {full?.state === "done"
              ? t("approval.truncated")
              : omitted
                ? t("approval.truncatedMore", { count: omitted })
                : t("approval.truncated")}
          </Text>
          <HStack gap={1}>
            {full?.state === "done" ? (
              <Button size="2xs" variant="ghost" onClick={() => setFull(null)} data-testid="tool-approval-show-short">
                {t("approval.showShort")}
              </Button>
            ) : (
              <Button
                size="2xs"
                variant="outline"
                loading={full?.state === "loading"}
                loadingText={t("approval.loadingFull")}
                onClick={() => void loadFull()}
                data-testid="tool-approval-show-full"
              >
                {t("approval.showFull")}
              </Button>
            )}
          </HStack>
          {full?.state === "error" ? (
            <Text fontSize="2xs" color="fg.error" role="alert">
              {t("approval.fullFailed")}
            </Text>
          ) : null}
          {full?.state === "done" && !full.matches ? (
            <Text fontSize="2xs" color="fg.error" role="alert" data-testid="tool-approval-mismatch">
              {t("approval.fullMismatch")}
            </Text>
          ) : null}
        </Stack>
      ) : null}
      {decision && !decision.approved && decision.reason ? (
        <Text fontSize="2xs" color="fg.muted" mt={1}>
          {t("approval.reasonShown", { reason: decision.reason })}
        </Text>
      ) : null}
      {open ? (
        <Stack gap={1} mt={2}>
          <HStack gap={1}>
            <Button
              size="2xs"
              variant={choice?.approved === true ? "solid" : "outline"}
              colorPalette="green"
              disabled={busy}
              aria-pressed={choice?.approved === true}
              onClick={() => onChoice({ approved: true, reason: choice?.reason ?? "" })}
              data-testid="tool-approval-approve"
            >
              <LuCheck /> {t("approval.approve")}
            </Button>
            <Button
              size="2xs"
              variant={choice?.approved === false ? "solid" : "outline"}
              colorPalette="red"
              disabled={busy}
              aria-pressed={choice?.approved === false}
              onClick={() => onChoice({ approved: false, reason: choice?.reason ?? "" })}
              data-testid="tool-approval-deny"
            >
              <LuX /> {t("approval.deny")}
            </Button>
          </HStack>
          {choice?.approved === false ? (
            <Field.Root>
              <Field.Label fontSize="2xs">{t("approval.reason")}</Field.Label>
              <Textarea
                size="xs"
                rows={2}
                value={choice.reason}
                disabled={busy}
                maxLength={1_000}
                onChange={(e) => onChoice({ approved: false, reason: e.target.value })}
                data-testid="tool-approval-reason"
              />
            </Field.Root>
          ) : null}
        </Stack>
      ) : null}
    </Box>
  );
}

/** A paused agent run: the tool calls waiting for the user's approval, and the outcome. */
export function ToolApprovalCard({
  approval,
  busy = false,
  onSubmit,
}: {
  approval: ToolApprovalView;
  busy?: boolean;
  onSubmit?: (decisions: ToolDecision[]) => void;
}) {
  const { t, i18n } = useTranslation("ai");
  const [choices, setChoices] = useState<Record<string, Choice | undefined>>({});
  const calls = Array.isArray(approval.calls) ? approval.calls.filter((c) => c && typeof c.id === "string") : [];
  const status = shownStatus(approval);
  const open = isOpen(approval) && !!onSubmit;
  const decisions = new Map((Array.isArray(approval.decisions) ? approval.decisions : []).map((d) => [d.id, d]));
  const ready = decisionsFor(calls, choices);
  const setAll = (approved: boolean) =>
    setChoices(Object.fromEntries(calls.map((c) => [c.id, { approved, reason: choices[c.id]?.reason ?? "" }])));
  return (
    <Box
      borderWidth="1px"
      borderColor={status === "pending" ? "orange.muted" : "border"}
      bg={status === "pending" ? "orange.subtle" : "bg.subtle"}
      rounded="md"
      p={2.5}
      data-testid="tool-approval-card"
      data-status={status}
    >
      <HStack gap={2} justify="space-between" wrap="wrap" mb={1}>
        <HStack gap={1.5}>
          <Box color={status === "pending" ? "orange.fg" : "fg.muted"}>
            <LuShieldQuestion />
          </Box>
          <Text fontSize="sm" fontWeight="semibold">
            {t("approval.title", { count: calls.length })}
          </Text>
        </HStack>
        <Badge size="xs" colorPalette={STATUS_PALETTE[status] ?? "gray"} data-testid="tool-approval-status">
          {t(`approval.status.${status}`, { defaultValue: String(status) })}
        </Badge>
      </HStack>
      {status === "pending" ? (
        <Text fontSize="xs" color="fg.muted" mb={2}>
          {t("approval.help")}
          {approval.expires_at
            ? ` ${t("approval.expires", { time: relativeTime(approval.expires_at, i18n.language) })}`
            : ""}
        </Text>
      ) : null}
      {status === "expired" ? (
        <Text fontSize="xs" color="fg.muted" mb={2}>
          {t("approval.expiredHelp")}
        </Text>
      ) : null}
      {status === "cancelled" ? (
        <Text fontSize="xs" color="fg.muted" mb={2}>
          {t("approval.cancelledHelp")}
        </Text>
      ) : null}
      {status === "failed" ? (
        <Text fontSize="xs" color="fg.error" mb={2} role="alert">
          {t("approval.failedHelp", { error: approval.error ?? "" })}
        </Text>
      ) : null}
      <Stack gap={2}>
        {calls.map((call) => (
          <CallView
            key={call.id}
            approvalId={approval.id}
            call={call}
            decision={decisions.get(call.id)}
            choice={choices[call.id]}
            open={open}
            busy={busy}
            onChoice={(c) => setChoices((s) => ({ ...s, [call.id]: c }))}
          />
        ))}
      </Stack>
      {approval.decided_by && status !== "pending" ? (
        <Text fontSize="2xs" color="fg.subtle" mt={1.5}>
          {t("approval.decidedBy", { user: approval.decided_by })}
        </Text>
      ) : null}
      {open ? (
        <HStack gap={1} mt={2} justify="flex-end" wrap="wrap">
          {calls.length > 1 ? (
            <>
              <Button size="xs" variant="ghost" disabled={busy} onClick={() => setAll(true)}>
                {t("approval.approveAll")}
              </Button>
              <Button size="xs" variant="ghost" disabled={busy} onClick={() => setAll(false)}>
                {t("approval.denyAll")}
              </Button>
            </>
          ) : null}
          <Button
            size="xs"
            colorPalette="blue"
            disabled={busy || !ready}
            loading={busy}
            onClick={() => ready && onSubmit?.(ready)}
            data-testid="tool-approval-submit"
          >
            {t("approval.submit")}
          </Button>
        </HStack>
      ) : null}
    </Box>
  );
}
