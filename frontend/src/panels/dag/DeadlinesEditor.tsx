import { Box, Button, Field, HStack, IconButton, Input, NativeSelect, Stack, Text } from "@chakra-ui/react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { LuPlus, LuTimer, LuTrash2 } from "react-icons/lu";

import type { DeadlineSpec } from "../../api/gen/types.gen";
import { useAirflowGate } from "../../widgets/airflowMeta";
import { CallbackRefEditor } from "../../widgets/CallbacksEditor";
import {
  DateTimeField,
  FIELD_ATTR,
  Hint,
  NumberField,
  SelectField,
  SwitchField,
  TextField,
} from "../../widgets/fields";
import { VersionBadge } from "../../widgets/VersionBadge";

type Reference = NonNullable<DeadlineSpec["reference"]>;
const REFERENCES: Reference[] = ["dagrun_queued_at", "dagrun_logical_date", "fixed_datetime", "average_runtime"];

const UNITS = [
  { unit: "d", seconds: 86_400 },
  { unit: "h", seconds: 3_600 },
  { unit: "min", seconds: 60 },
  { unit: "s", seconds: 1 },
] as const;
type Unit = (typeof UNITS)[number]["unit"];

/** Largest unit that represents `seconds` exactly. */
function bestUnit(seconds: number): Unit {
  const abs = Math.abs(seconds);
  return UNITS.find((u) => abs !== 0 && abs % u.seconds === 0)?.unit ?? "min";
}
const unitSeconds = (u: Unit) => UNITS.find((x) => x.unit === u)?.seconds ?? 60;

const DEADLINES_MIN_AIRFLOW = "3.1";

function SignedInterval({
  value,
  onChange,
  disabled,
}: {
  value: number;
  onChange: (v: number) => void;
  disabled: boolean;
}) {
  const { t } = useTranslation("inspector");
  const [unit, setUnit] = useState<Unit>(() => bestUnit(value));
  const size = unitSeconds(unit);
  const before = value < 0;
  const amount = Math.abs(value) / size;
  const label = t("dag.deadlines.interval");
  return (
    <Field.Root disabled={disabled}>
      <Field.Label fontSize="xs">{label}</Field.Label>
      <HStack gap={1} w="100%">
        <Input
          size="sm"
          type="number"
          min={0}
          aria-label={label}
          value={Number.isFinite(amount) ? amount : ""}
          onChange={(e) => {
            const n = Math.abs(Number(e.target.value) || 0);
            onChange(Math.round(n * size) * (before ? -1 : 1));
          }}
        />
        <NativeSelect.Root size="sm" w="7rem" flexShrink={0}>
          <NativeSelect.Field
            aria-label={t("dag.deadlines.unit")}
            value={unit}
            onChange={(e) => {
              const next = e.target.value as Unit;
              setUnit(next);
              // keep the displayed amount, change its unit
              onChange(Math.round(amount * unitSeconds(next)) * (before ? -1 : 1));
            }}
          >
            {UNITS.map((u) => (
              <option key={u.unit} value={u.unit}>
                {t(`dag.deadlines.units.${u.unit}`)}
              </option>
            ))}
          </NativeSelect.Field>
          <NativeSelect.Indicator />
        </NativeSelect.Root>
        <NativeSelect.Root size="sm" w="7.5rem" flexShrink={0}>
          <NativeSelect.Field
            aria-label={t("dag.deadlines.direction")}
            value={before ? "before" : "after"}
            onChange={(e) => onChange(Math.abs(value) * (e.target.value === "before" ? -1 : 1))}
          >
            <option value="after">{t("dag.deadlines.after")}</option>
            <option value="before">{t("dag.deadlines.before")}</option>
          </NativeSelect.Field>
          <NativeSelect.Indicator />
        </NativeSelect.Root>
      </HStack>
    </Field.Root>
  );
}

function DeadlineRow({
  value,
  onChange,
  onRemove,
  index,
  disabled,
}: {
  value: DeadlineSpec;
  onChange: (v: DeadlineSpec) => void;
  onRemove: () => void;
  index: number;
  disabled: boolean;
}) {
  const { t } = useTranslation("inspector");
  const reference = value.reference ?? "dagrun_queued_at";
  const set = (patch: Partial<DeadlineSpec>) => onChange({ ...value, ...patch });
  const field = `deadlines.${index}`;
  return (
    <Box borderWidth="1px" rounded="md" p={2} {...{ [FIELD_ATTR]: field }}>
      <HStack justify="space-between" mb={1}>
        <HStack gap={1}>
          <LuTimer aria-hidden />
          <Text fontSize="xs" fontWeight="semibold">
            {value.name || t("dag.deadlines.unnamed", { n: index + 1 })}
          </Text>
        </HStack>
        <IconButton
          aria-label={t("common.removeItem", { item: value.name || index + 1 })}
          size="2xs"
          variant="ghost"
          colorPalette="red"
          onClick={onRemove}
        >
          <LuTrash2 />
        </IconButton>
      </HStack>
      <Stack gap={2}>
        <TextField
          help={t("dag.deadlines.nameHelp")}
          label={t("dag.deadlines.name")}
          disabled={disabled}
          value={value.name}
          onChange={(v) => set({ name: v })}
        />
        <SelectField
          label={t("dag.deadlines.reference")}
          disabled={disabled}
          value={reference}
          help={t(`dag.deadlines.referenceHelp.${reference}`)}
          options={REFERENCES.map((r) => ({ value: r, label: t(`dag.deadlines.references.${r}`) }))}
          onChange={(r) => {
            const next: DeadlineSpec = { ...value, reference: r ?? "dagrun_queued_at" };
            if (r !== "fixed_datetime") delete next.fixed_datetime;
            if (r !== "average_runtime") delete next.average_max_runs;
            onChange(next);
          }}
        />
        {reference === "fixed_datetime" ? (
          <DateTimeField
            help={t("dag.deadlines.fixedDatetimeHelp")}
            label={t("dag.deadlines.fixedDatetime")}
            required
            disabled={disabled}
            field={`${field}.fixed_datetime`}
            value={value.fixed_datetime}
            onChange={(v) => set({ fixed_datetime: v })}
          />
        ) : null}
        {reference === "average_runtime" ? (
          <NumberField
            label={t("dag.deadlines.averageMaxRuns")}
            help={t("dag.deadlines.averageMaxRunsHelp")}
            min={1}
            disabled={disabled}
            value={value.average_max_runs}
            onChange={(v) => set({ average_max_runs: v })}
          />
        ) : null}
        <SignedInterval
          value={value.interval_seconds}
          disabled={disabled}
          onChange={(interval_seconds) => set({ interval_seconds })}
        />
        <Hint>{describeDeadline(value, t)}</Hint>
        <CallbackRefEditor
          label={t("dag.deadlines.callback")}
          field={`${field}.callback`}
          value={value.callback}
          onChange={(callback) => set({ callback })}
        />
        <SwitchField
          label={t("dag.deadlines.synchronous")}
          help={t("dag.deadlines.synchronousHelp")}
          disabled={disabled}
          checked={!!value.synchronous}
          onChange={(synchronous) => set({ synchronous })}
        />
      </Stack>
    </Box>
  );
}

type T = (key: string, vars?: Record<string, unknown>) => string;

function describeDeadline(d: DeadlineSpec, t: T): string {
  const secs = Math.abs(d.interval_seconds);
  const unit = bestUnit(secs);
  const amount = secs / unitSeconds(unit);
  return t(d.interval_seconds < 0 ? "dag.deadlines.summaryBefore" : "dag.deadlines.summaryAfter", {
    amount,
    unit: t(`dag.deadlines.units.${unit}`),
    reference: t(`dag.deadlines.references.${d.reference ?? "dagrun_queued_at"}`),
  });
}

/** DAG `deadline=[DeadlineAlert(...)]` editor (Airflow ≥ 3.1). */
export function DeadlinesEditor({
  value,
  onChange,
}: {
  value: DeadlineSpec[] | null | undefined;
  onChange: (v: DeadlineSpec[]) => void;
}) {
  const { t } = useTranslation("inspector");
  const { supports } = useAirflowGate();
  const ok = supports(DEADLINES_MIN_AIRFLOW);
  const list = value ?? [];
  return (
    <Stack gap={2} {...{ [FIELD_ATTR]: "deadlines" }}>
      <HStack justify="space-between">
        <Text fontSize="sm" fontWeight="semibold">
          {t("dag.deadlines.title")}
        </Text>
        <VersionBadge min={DEADLINES_MIN_AIRFLOW} />
      </HStack>
      <Hint>{t("dag.deadlines.help")}</Hint>
      {!ok ? <Hint tone="warning">{t("dag.deadlines.unsupported")}</Hint> : null}
      {list.map((d, i) => (
        <DeadlineRow
          key={i}
          index={i}
          value={d}
          disabled={!ok}
          onChange={(v) => onChange(list.map((x, j) => (j === i ? v : x)))}
          onRemove={() => onChange(list.filter((_, j) => j !== i))}
        />
      ))}
      <Button
        size="xs"
        variant="outline"
        alignSelf="start"
        disabled={!ok}
        onClick={() =>
          onChange([
            ...list,
            { reference: "dagrun_queued_at", interval_seconds: 3600, callback: { notifier: "", params: {} } },
          ])
        }
      >
        <LuPlus /> {t("dag.deadlines.add")}
      </Button>
    </Stack>
  );
}
