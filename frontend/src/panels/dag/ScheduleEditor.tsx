import { Box, Button, Field, HStack, IconButton, Input, Spinner, Stack, Table, Text } from "@chakra-ui/react";
import { useMutation } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { LuCalendarClock, LuPlus, LuTrash2 } from "react-icons/lu";

import { previewNextRunsMutation } from "../../api/gen/@tanstack/react-query.gen";
import { errorMessage } from "../../api/setup";
import {
  BATCH_ASSET_EVENTS_MIN,
  CRON_FIELDS,
  CRON_PRESETS,
  PARTITION_MAPPERS,
  PRESETS,
  SCHEDULE_TYPES,
  type Schedule,
  type ScheduleType,
  TIME_SCHEDULE_TYPES,
  type TimeSchedule,
  type Translate,
  allTimezones,
  isValidTimezone,
  defaultSchedule,
  describeCron,
  hmsToSeconds,
  isAssetSchedule,
  isTimeScheduleType,
  joinCron,
  secondsToHms,
  splitCron,
  validateCron,
} from "../../lib/schedule";
import { useBuilder } from "../../store/builder";
import { useAirflowGate } from "../../widgets/airflowMeta";
import { AssetExprEditor } from "../../widgets/assets/AssetExprEditor";
import { Autocomplete } from "../../widgets/Autocomplete";
import {
  DateTimeField,
  FIELD_ATTR,
  Hint,
  NumberField,
  Panel,
  SelectField,
  SwitchField,
  TextField,
} from "../../widgets/fields";
import { VersionBadge } from "../../widgets/VersionBadge";

function useCronText(t: Translate) {
  return (expr: string) => {
    const err = validateCron(expr);
    return {
      error: err
        ? t(err.key, { ...err.vars, field: err.vars?.field ? t(`schedule.cron.fields.${err.vars.field}`) : "" })
        : undefined,
      description: err ? null : describeCron(expr, t),
    };
  };
}

/** IANA timezone picker (free text allowed). Schedules inherit the DAG timezone by default. */
export function TimezoneField({
  value,
  onChange,
  required,
  label,
  help,
  placeholder,
  field,
}: {
  value: string | null | undefined;
  onChange: (v: string | null) => void;
  field?: string | undefined;
  required?: boolean;
  label?: string;
  help?: string;
  placeholder?: string;
}) {
  const { t } = useTranslation("inspector");
  const zones = useMemo(() => allTimezones().map((z) => ({ value: z })), []);
  const dagTimezone = useBuilder((s) => s.dag.timezone);
  // Typing "Europe/Is…" passes through unknown zones: keep them local and commit valid ones only.
  const [draft, setDraft] = useState(value ?? "");
  const [prev, setPrev] = useState(value);
  if (prev !== value) {
    setPrev(value);
    if (value !== null && value !== undefined && value !== draft) setDraft(value);
  }
  const invalid = !!draft.trim() && !isValidTimezone(draft.trim());
  return (
    <Autocomplete
      label={label ?? t("schedule.timezone")}
      help={help ?? t("schedule.timezoneHelp")}
      error={invalid ? t("schedule.timezoneInvalid") : undefined}
      placeholder={
        placeholder ??
        (dagTimezone ? t("schedule.timezoneInherit", { tz: dagTimezone }) : t("schedule.timezonePlaceholder"))
      }
      required={required ?? false}
      field={field}
      value={draft}
      options={zones}
      onChange={(v) => {
        const text = (v ?? "").trim();
        setDraft(v ?? "");
        if (!text) onChange(null);
        else if (isValidTimezone(text)) onChange(text);
      }}
    />
  );
}

/** Cron builder: preset picker, one input per field, raw expression and description. */
function CronBuilder({ value, onChange, label }: { value: string; onChange: (v: string) => void; label?: string }) {
  const { t } = useTranslation("inspector");
  const describe = useCronText(t);
  const { error, description } = describe(value);
  const fields = splitCron(value);
  const preset = CRON_PRESETS.find((p) => p.expression === value.trim())?.key ?? null;
  const isAlias = value.trim().startsWith("@");
  return (
    <Stack gap={2}>
      <SelectField
        help={t("schedule.cron.presetHelp")}
        label={t("schedule.cron.preset")}
        value={preset}
        emptyLabel={t("schedule.cron.custom")}
        options={CRON_PRESETS.map((p) => ({
          value: p.key,
          label: `${t(`schedule.cron.presets.${p.key}`)} (${p.expression})`,
        }))}
        onChange={(k) => {
          const p = CRON_PRESETS.find((x) => x.key === k);
          if (p) onChange(p.expression);
        }}
      />
      {!isAlias ? (
        <HStack gap={1} align="end" role="group" aria-label={t("schedule.cron.fieldsLabel")}>
          {CRON_FIELDS.map((f, i) => (
            <Field.Root key={f} flex="1" minW={0}>
              <Field.Label fontSize="2xs" color="fg.muted" truncate title={t(`schedule.cron.fields.${f}`)}>
                {t(`schedule.cron.fieldsShort.${f}`)}
              </Field.Label>
              <Input
                size="xs"
                fontFamily="mono"
                textAlign="center"
                aria-label={t(`schedule.cron.fields.${f}`)}
                value={fields[i]}
                onChange={(e) =>
                  onChange(joinCron(fields.map((x, j) => (j === i ? e.target.value.replace(/\s+/g, "") : x))))
                }
              />
            </Field.Root>
          ))}
        </HStack>
      ) : null}
      <TextField
        label={label ?? t("schedule.cron.expression")}
        mono
        required
        value={value}
        error={error}
        help={description ? `→ ${description}` : t("schedule.cron.syntaxHelp")}
        onChange={(v) => onChange(v ?? "")}
      />
    </Stack>
  );
}

function CronScheduleEditor({
  value,
  onChange,
}: {
  value: Extract<Schedule, { type: "cron" }>;
  onChange: (s: Schedule) => void;
}) {
  const { t } = useTranslation("inspector");
  const set = (patch: Partial<Extract<Schedule, { type: "cron" }>>) => onChange({ ...value, ...patch });
  return (
    <Stack gap={2}>
      <CronBuilder value={value.expression} onChange={(expression) => set({ expression })} />
      <TimezoneField value={value.timezone} onChange={(timezone) => set({ timezone })} />
      <SwitchField
        label={t("schedule.dataInterval")}
        help={value.data_interval ? t("schedule.dataIntervalOn") : t("schedule.dataIntervalOff")}
        checked={!!value.data_interval}
        onChange={(data_interval) =>
          onChange(
            data_interval
              ? { ...value, data_interval, interval_seconds: null, run_immediately: false }
              : { ...value, data_interval },
          )
        }
      />
      {!value.data_interval ? (
        <>
          <NumberField
            label={t("schedule.intervalSeconds")}
            help={t("schedule.intervalSecondsHelp")}
            min={0}
            value={value.interval_seconds}
            onChange={(interval_seconds) => set({ interval_seconds })}
          />
          <SwitchField
            label={t("schedule.runImmediately")}
            help={t("schedule.runImmediatelyHelp")}
            checked={!!value.run_immediately}
            onChange={(run_immediately) => set({ run_immediately })}
          />
        </>
      ) : null}
    </Stack>
  );
}

function IntervalEditor({
  value,
  onChange,
}: {
  value: Extract<Schedule, { type: "interval" }>;
  onChange: (s: Schedule) => void;
}) {
  const { t } = useTranslation("inspector");
  const hms = secondsToHms(value.seconds);
  const set = (patch: Partial<typeof hms>) =>
    onChange({ ...value, seconds: Math.max(1, hmsToSeconds({ ...hms, ...patch })) });
  return (
    <Stack gap={2}>
      <HStack align="start">
        <NumberField
          help={t("schedule.hoursHelp")}
          label={t("schedule.hours")}
          min={0}
          value={hms.h}
          onChange={(h) => set({ h: h ?? 0 })}
        />
        <NumberField
          help={t("schedule.minutesHelp")}
          label={t("schedule.minutes")}
          min={0}
          max={59}
          value={hms.m}
          onChange={(m) => set({ m: m ?? 0 })}
        />
        <NumberField
          help={t("schedule.secondsHelp")}
          label={t("schedule.seconds")}
          min={0}
          max={59}
          value={hms.s}
          onChange={(s) => set({ s: s ?? 0 })}
        />
      </HStack>
      <Hint>{t("schedule.intervalTotal", { seconds: value.seconds })}</Hint>
      <SwitchField
        label={t("schedule.dataInterval")}
        help={value.data_interval ? t("schedule.dataIntervalOn") : t("schedule.dataIntervalOff")}
        checked={!!value.data_interval}
        onChange={(data_interval) => onChange({ ...value, data_interval })}
      />
    </Stack>
  );
}

function MultiCronEditor({
  value,
  onChange,
}: {
  value: Extract<Schedule, { type: "multi_cron" }>;
  onChange: (s: Schedule) => void;
}) {
  const { t } = useTranslation("inspector");
  const describe = useCronText(t);
  const exprs = value.expressions;
  const setExprs = (expressions: string[]) => onChange({ ...value, expressions });
  return (
    <Stack gap={2}>
      <Text fontSize="xs" fontWeight="medium">
        {t("schedule.multiCron")}
      </Text>
      {exprs.map((e, i) => {
        const { error, description } = describe(e);
        return (
          <HStack key={i} align="start" gap={1}>
            <Box flex="1">
              <TextField
                label={t("schedule.cron.expressionN", { n: i + 1 })}
                mono
                value={e}
                error={error}
                help={description ?? undefined}
                onChange={(v) => setExprs(exprs.map((x, j) => (j === i ? (v ?? "") : x)))}
              />
            </Box>
            <IconButton
              mt={5}
              aria-label={t("schedule.removeExpression", { n: i + 1 })}
              size="xs"
              variant="ghost"
              disabled={exprs.length <= 1}
              onClick={() => setExprs(exprs.filter((_, j) => j !== i))}
            >
              <LuTrash2 />
            </IconButton>
          </HStack>
        );
      })}
      <Button size="xs" variant="outline" alignSelf="start" onClick={() => setExprs([...exprs, "0 18 * * *"])}>
        <LuPlus /> {t("schedule.addExpression")}
      </Button>
      <TimezoneField value={value.timezone} onChange={(timezone) => onChange({ ...value, timezone })} />
    </Stack>
  );
}

function EventsEditor({
  value,
  onChange,
}: {
  value: Extract<Schedule, { type: "events" }>;
  onChange: (s: Schedule) => void;
}) {
  const { t } = useTranslation("inspector");
  const dates = value.dates;
  const setDates = (d: string[]) => onChange({ ...value, dates: d });
  return (
    <Stack gap={2}>
      <Text fontSize="xs" fontWeight="medium">
        {t("schedule.events")}
      </Text>
      {dates.map((d, i) => (
        <HStack key={i} align="end" gap={1}>
          <Box flex="1">
            <DateTimeField
              help={t("schedule.eventNHelp")}
              label={t("schedule.eventN", { n: i + 1 })}
              value={d}
              onChange={(v) => setDates(dates.map((x, j) => (j === i ? (v ?? x) : x)))}
            />
          </Box>
          <IconButton
            aria-label={t("schedule.removeEvent", { n: i + 1 })}
            size="xs"
            variant="ghost"
            disabled={dates.length <= 1}
            onClick={() => setDates(dates.filter((_, j) => j !== i))}
          >
            <LuTrash2 />
          </IconButton>
        </HStack>
      ))}
      <Button
        size="xs"
        variant="outline"
        alignSelf="start"
        onClick={() => {
          const last = dates[dates.length - 1];
          const base = last ? new Date(last) : new Date();
          const next = new Date((Number.isNaN(base.getTime()) ? Date.now() : base.getTime()) + 86_400_000);
          setDates([...dates, next.toISOString().replace(/\.\d+Z$/, "Z")]);
        }}
      >
        <LuPlus /> {t("schedule.addEvent")}
      </Button>
      <SwitchField
        label={t("schedule.restrictToEvents")}
        help={t("schedule.restrictToEventsHelp")}
        checked={!!value.restrict_to_events}
        onChange={(restrict_to_events) => onChange({ ...value, restrict_to_events })}
      />
      <TextField
        help={t("schedule.eventsDescriptionHelp")}
        label={t("schedule.eventsDescription")}
        value={value.description}
        onChange={(description) => onChange({ ...value, description })}
      />
    </Stack>
  );
}

const asTime = (s: Schedule): TimeSchedule | null => (isTimeScheduleType(s.type) ? (s as TimeSchedule) : null);

function TypeFields({ value, onChange }: { value: Schedule; onChange: (s: Schedule) => void }) {
  const { t } = useTranslation("inspector");
  const { supports } = useAirflowGate();
  switch (value.type) {
    case "none":
      return <Hint>{t("schedule.noneHelp")}</Hint>;
    case "preset":
      return (
        <SelectField
          help={t("schedule.presetHelp")}
          label={t("schedule.preset")}
          value={value.preset}
          options={PRESETS.map((p) => ({ value: p, label: `${p} — ${t(`schedule.presets.${p.slice(1)}`)}` }))}
          onChange={(p) => onChange({ type: "preset", preset: p ?? "@daily" })}
        />
      );
    case "cron":
      return <CronScheduleEditor value={value} onChange={onChange} />;
    case "interval":
      return <IntervalEditor value={value} onChange={onChange} />;
    case "multi_cron":
      return <MultiCronEditor value={value} onChange={onChange} />;
    case "events":
      return <EventsEditor value={value} onChange={onChange} />;
    case "assets": {
      const batchOk = supports(BATCH_ASSET_EVENTS_MIN);
      return (
        <Stack gap={2}>
          <AssetExprEditor value={value.expression} onChange={(expression) => onChange({ ...value, expression })} />
          <HStack justify="space-between" align="start">
            <SwitchField
              label={t("schedule.batchAssetEvents")}
              help={t("schedule.batchAssetEventsHelp")}
              disabled={!batchOk && value.batch_asset_events == null}
              checked={!!value.batch_asset_events}
              onChange={(on) => onChange({ ...value, batch_asset_events: on ? true : null })}
            />
            <VersionBadge min={BATCH_ASSET_EVENTS_MIN} />
          </HStack>
        </Stack>
      );
    }
    case "assets_or_time":
    case "assets_and_time":
      return (
        <Stack gap={2}>
          <Hint>
            {value.type === "assets_or_time" ? t("schedule.assetsOrTimeHelp") : t("schedule.assetsAndTimeHelp")}
          </Hint>
          <AssetExprEditor value={value.expression} onChange={(expression) => onChange({ ...value, expression })} />
          <Panel title={t("schedule.timePart")}>
            <ScheduleEditor
              timeOnly
              preview={false}
              value={value.time}
              onChange={(s) => {
                const time = asTime(s);
                if (time) onChange({ ...value, time });
              }}
            />
          </Panel>
        </Stack>
      );
    case "partitioned_cron":
      return (
        <Stack gap={2}>
          <CronBuilder value={value.expression} onChange={(expression) => onChange({ ...value, expression })} />
          <TimezoneField
            required
            value={value.timezone}
            onChange={(tz) => onChange({ ...value, timezone: tz ?? "UTC" })}
          />
          <TextField
            label={t("schedule.keyFormat")}
            help={t("schedule.keyFormatHelp")}
            mono
            placeholder="%Y-%m-%dT%H:%M:%S"
            value={value.key_format}
            onChange={(key_format) => onChange({ ...value, key_format })}
          />
        </Stack>
      );
    case "partitioned_assets":
      return (
        <Stack gap={2}>
          <AssetExprEditor value={value.expression} onChange={(expression) => onChange({ ...value, expression })} />
          <SelectField
            label={t("schedule.mapper")}
            help={t("schedule.mapperHelp")}
            value={value.mapper ?? "identity"}
            options={PARTITION_MAPPERS.map((m) => ({ value: m, label: t(`schedule.mappers.${m}`) }))}
            onChange={(mapper) => onChange({ ...value, mapper: mapper ?? "identity" })}
          />
          <TimezoneField value={value.timezone} onChange={(timezone) => onChange({ ...value, timezone })} />
        </Stack>
      );
  }
}

/** Editor for every DAG schedule (timetable) type, with live next-run preview. */
export function ScheduleEditor({
  value,
  onChange,
  field,
  preview = true,
  timeOnly = false,
}: {
  value: Schedule | null | undefined;
  onChange: (s: Schedule) => void;
  field?: string;
  preview?: boolean;
  /** Restrict to time based types (the `time` part of asset + time schedules). */
  timeOnly?: boolean;
}) {
  const { t } = useTranslation("inspector");
  const { supports } = useAirflowGate();
  const schedule: Schedule = value ?? { type: "none" };
  const types = timeOnly
    ? SCHEDULE_TYPES.filter((s) => (TIME_SCHEDULE_TYPES as string[]).includes(s.type))
    : SCHEDULE_TYPES;
  const info = SCHEDULE_TYPES.find((s) => s.type === schedule.type);
  return (
    <Stack gap={2} {...(field ? { [FIELD_ATTR]: field } : {})}>
      <HStack align="end">
        <Box flex="1">
          <SelectField<ScheduleType>
            label={timeOnly ? t("schedule.timeType") : t("schedule.type")}
            value={schedule.type}
            options={types.map((s) => ({
              value: s.type,
              label: `${t(`schedule.types.${s.type}`)}${s.minAirflow && !supports(s.minAirflow) ? ` (${t("common.requiresAirflow", { version: s.minAirflow })})` : ""}`,
              disabled: !!s.minAirflow && !supports(s.minAirflow) && s.type !== schedule.type,
            }))}
            onChange={(type) => type && type !== schedule.type && onChange(defaultSchedule(type, schedule))}
          />
        </Box>
        {info?.minAirflow ? (
          <Box pb={2}>
            <VersionBadge min={info.minAirflow} />
          </Box>
        ) : null}
      </HStack>
      {schedule.type !== "none" ? <Hint>{t(`schedule.typeHelp.${schedule.type}`)}</Hint> : null}
      <TypeFields value={schedule} onChange={onChange} />
      {preview && !timeOnly ? <NextRunsPreview schedule={schedule} /> : null}
    </Stack>
  );
}

const utcText = (d: Date) => `${d.toISOString().slice(0, 16).replace("T", " ")} UTC`;

/** Next run times computed by Airflow's own timetable (debounced). */
function NextRunsPreview({ schedule }: { schedule: Schedule | null | undefined }) {
  const { t, i18n } = useTranslation("inspector");
  const { mutate, data, error, isPending, reset } = useMutation(previewNextRunsMutation());
  const s: Schedule = schedule ?? { type: "none" };
  const skip = s.type === "none" || isAssetSchedule(s.type);
  const key = JSON.stringify(s);
  useEffect(() => {
    if (skip) {
      reset();
      return;
    }
    const timer = setTimeout(() => {
      const spec = useBuilder.getState().toSpec();
      mutate({ body: { spec: { ...spec, schedule: JSON.parse(key) as Schedule }, count: 5 } });
    }, 600);
    return () => clearTimeout(timer);
  }, [key, skip, mutate, reset]);

  const dagTimezone = useBuilder((s) => s.dag.timezone);
  const fmt = useMemo(
    () =>
      // dateStyle/timeStyle cannot be combined with timeZoneName (TypeError).
      new Intl.DateTimeFormat(i18n.language, {
        ...(isValidTimezone(dagTimezone) ? { timeZone: dagTimezone } : {}),
        year: "numeric",
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
        timeZoneName: "short",
      }),
    [i18n.language, dagTimezone],
  );

  let body;
  if (skip) {
    body = <Hint>{s.type === "none" ? t("schedule.next.manual") : t("schedule.next.assetDriven")}</Hint>;
  } else if (isPending && !data) {
    body = <Spinner size="xs" />;
  } else if (error) {
    body = <Hint tone="error">{errorMessage(error)}</Hint>;
  } else if (data?.error) {
    body = <Hint tone="error">{data.error}</Hint>;
  } else if (data && !data.runs.length) {
    body = <Hint>{t("schedule.next.none")}</Hint>;
  } else if (data) {
    body = (
      <Table.Root size="sm" variant="line">
        <Table.Header>
          <Table.Row>
            <Table.ColumnHeader fontSize="2xs">{t("schedule.next.local")}</Table.ColumnHeader>
            <Table.ColumnHeader fontSize="2xs">{t("schedule.next.utc")}</Table.ColumnHeader>
          </Table.Row>
        </Table.Header>
        <Table.Body>
          {data.runs.map((r) => {
            const d = new Date(r);
            const valid = !Number.isNaN(d.getTime());
            return (
              <Table.Row key={r}>
                <Table.Cell fontSize="xs">{valid ? fmt.format(d) : r}</Table.Cell>
                <Table.Cell fontSize="xs" fontFamily="mono" color="fg.muted">
                  {valid ? utcText(d) : ""}
                </Table.Cell>
              </Table.Row>
            );
          })}
        </Table.Body>
      </Table.Root>
    );
  }
  return (
    <Box borderWidth="1px" rounded="md" p={2} aria-live="polite">
      <HStack gap={1} mb={1}>
        <LuCalendarClock aria-hidden />
        <Text fontSize="xs" fontWeight="semibold">
          {t("schedule.next.title")}
        </Text>
        {isPending && data ? <Spinner size="xs" /> : null}
      </HStack>
      {data?.summary && !skip ? (
        <Text fontSize="xs" color="fg.muted" mb={1}>
          {data.summary}
        </Text>
      ) : null}
      {body}
    </Box>
  );
}
