import { Badge, Box, Button, HStack, IconButton, Spinner, Stack, Text } from "@chakra-ui/react";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { LuArrowDown, LuArrowUp, LuBell, LuPlus, LuTrash2 } from "react-icons/lu";

import { getNotifierOptions, listNotifiersOptions } from "../api/gen/@tanstack/react-query.gen";
import type { CallbackRef, Callbacks } from "../api/gen/types.gen";
import { errorMessage } from "../api/setup";
import { type JsonSchema, withoutKey } from "../lib/formSchema";
import { Autocomplete, type AutocompleteOption } from "./Autocomplete";
import { FIELD_ATTR, Hint } from "./fields";
import { ParamsForm } from "./ParamsForm";

type CallbackEvent = keyof Callbacks;
export const TASK_CALLBACK_EVENTS: CallbackEvent[] = [
  "on_success",
  "on_failure",
  "on_retry",
  "on_execute",
  "on_skipped",
];
export const DAG_CALLBACK_EVENTS: CallbackEvent[] = ["on_success", "on_failure"];

function useNotifierOptions(): { options: AutocompleteOption[]; loading: boolean } {
  const { t } = useTranslation("inspector");
  const list = useQuery({ ...listNotifiersOptions(), staleTime: 300_000 });
  const options = (list.data ?? []).map((n) => ({
    value: n.fqn,
    label: n.class_name,
    description:
      n.builder_supported === false
        ? `${n.provider} · ${n.unsupported_reason ?? t("callbacks.unsupported")}`
        : `${n.provider}${n.summary ? ` · ${n.summary}` : ""}`,
    disabled: n.builder_supported === false,
  }));
  return { options, loading: list.isPending };
}

/** One notifier instance: class picker + its constructor params. */
export function CallbackRefEditor({
  value,
  onChange,
  label,
  field,
}: {
  value: CallbackRef;
  onChange: (v: CallbackRef) => void;
  label?: string;
  field?: string;
}) {
  const { t } = useTranslation("inspector");
  const { options } = useNotifierOptions();
  const detail = useQuery({
    ...getNotifierOptions({ path: { fqn: value.notifier } }),
    enabled: !!value.notifier && options.some((o) => o.value === value.notifier),
    staleTime: 300_000,
  });
  return (
    <Stack gap={2} {...(field ? { [FIELD_ATTR]: field } : {})}>
      <Autocomplete
        label={label ?? t("callbacks.notifier")}
        value={value.notifier}
        mono
        options={options}
        placeholder="airflow.providers.slack.notifications.slack.SlackNotifier"
        onChange={(v) => onChange({ notifier: v ?? "", params: value.params ?? {} })}
        onSelect={(o) => onChange({ notifier: o.value, params: {} })}
      />
      {detail.isFetching && !detail.data ? <Spinner size="xs" /> : null}
      {detail.isError ? <Hint tone="error">{errorMessage(detail.error)}</Hint> : null}
      {detail.data?.summary ? <Hint>{detail.data.summary}</Hint> : null}
      {detail.data ? (
        <ParamsForm
          schema={detail.data.params_schema as JsonSchema}
          value={value.params ?? {}}
          onChange={(params) => onChange({ ...value, params })}
          fieldPrefix={field ? `${field}.params` : undefined}
          allowXcom={false}
          connectionTypes={detail.data.connection_types ?? []}
        />
      ) : null}
    </Stack>
  );
}

function EventList({
  event,
  refs,
  onChange,
  field,
}: {
  event: CallbackEvent;
  refs: CallbackRef[];
  onChange: (v: CallbackRef[]) => void;
  field?: string | undefined;
}) {
  const { t } = useTranslation("inspector");
  const move = (i: number, d: -1 | 1) => {
    const next = [...refs];
    const [item] = next.splice(i, 1);
    if (!item) return;
    next.splice(i + d, 0, item);
    onChange(next);
  };
  const eventLabel = t(`callbacks.events.${event}`);
  return (
    <Box borderWidth="1px" rounded="md" p={2} {...(field ? { [FIELD_ATTR]: field } : {})}>
      <HStack justify="space-between" mb={refs.length ? 2 : 0}>
        <HStack gap={1}>
          <LuBell aria-hidden />
          <Text fontSize="xs" fontWeight="semibold">
            {eventLabel}
          </Text>
          {refs.length ? <Badge size="xs">{refs.length}</Badge> : null}
        </HStack>
        <Button
          size="2xs"
          variant="outline"
          onClick={() => onChange([...refs, { notifier: "", params: {} }])}
          aria-label={t("callbacks.addFor", { event: eventLabel })}
        >
          <LuPlus /> {t("callbacks.add")}
        </Button>
      </HStack>
      <Stack gap={3}>
        {refs.map((ref, i) => (
          <Box key={i} borderLeftWidth="2px" borderColor="border.emphasized" pl={2}>
            <HStack justify="end" gap={0}>
              <IconButton
                aria-label={t("common.moveUp")}
                size="2xs"
                variant="ghost"
                disabled={i === 0}
                onClick={() => move(i, -1)}
              >
                <LuArrowUp />
              </IconButton>
              <IconButton
                aria-label={t("common.moveDown")}
                size="2xs"
                variant="ghost"
                disabled={i === refs.length - 1}
                onClick={() => move(i, 1)}
              >
                <LuArrowDown />
              </IconButton>
              <IconButton
                aria-label={t("common.removeItem", { item: ref.notifier.split(".").pop() || i + 1 })}
                size="2xs"
                variant="ghost"
                colorPalette="red"
                onClick={() => onChange(refs.filter((_, j) => j !== i))}
              >
                <LuTrash2 />
              </IconButton>
            </HStack>
            <CallbackRefEditor value={ref} onChange={(v) => onChange(refs.map((r, j) => (j === i ? v : r)))} />
          </Box>
        ))}
      </Stack>
    </Box>
  );
}

/** Notifier callbacks per event (task: 5 events, DAG: success / failure). */
export function CallbacksEditor({
  value,
  onChange,
  events,
  fieldPrefix = "callbacks",
}: {
  value: Callbacks | null | undefined;
  onChange: (v: Callbacks) => void;
  events: CallbackEvent[];
  fieldPrefix?: string;
}) {
  const { t } = useTranslation("inspector");
  const cb: Callbacks = value ?? {};
  const set = (event: CallbackEvent, refs: CallbackRef[]) => {
    onChange(refs.length ? { ...cb, [event]: refs } : withoutKey(cb, event));
  };
  return (
    <Stack gap={2}>
      <Hint>{t("callbacks.help")}</Hint>
      {events.map((event) => (
        <EventList
          key={event}
          event={event}
          field={`${fieldPrefix}.${event}`}
          refs={cb[event] ?? []}
          onChange={(refs) => set(event, refs)}
        />
      ))}
    </Stack>
  );
}
