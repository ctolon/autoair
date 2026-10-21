/**
 * Schedule (timetable) helpers: defaults per schedule type, Airflow version gates, cron
 * parsing / validation / human readable descriptions, timezones and asset expressions.
 */
import type {
  AssetAliasLeaf,
  AssetCombination,
  AssetLeaf,
  AssetOrTimeScheduleSpec,
  AssetRef,
  AssetSchedule,
  PartitionedAssetSchedule,
  PresetSchedule,
} from "../api/gen/types.gen";
import type { DagSettings } from "./types";

export type Schedule = NonNullable<DagSettings["schedule"]>;
export type ScheduleType = Schedule["type"];
/** Time-based schedules (also the `time` part of assets_or_time / assets_and_time). */
export type TimeSchedule = AssetOrTimeScheduleSpec["time"];
type TimeScheduleType = TimeSchedule["type"];
/** Root asset condition of an asset-driven schedule. */
export type AssetExpr = AssetSchedule["expression"];
/** Any node of an asset condition tree. */
export type AssetExprNode = AssetLeaf | AssetAliasLeaf | AssetCombination;
type PartitionMapper = NonNullable<PartitionedAssetSchedule["mapper"]>;

/** Minimal translation function (i18next's `t` fits). */
export type Translate = (key: string, vars?: Record<string, unknown>) => string;

type ScheduleTypeInfo = {
  type: ScheduleType;
  group: "basic" | "time" | "assets" | "partitions";
  /** Minimum Airflow version (see autoair.codegen.features). */
  minAirflow?: string;
};

export const SCHEDULE_TYPES: ScheduleTypeInfo[] = [
  { type: "none", group: "basic" },
  { type: "preset", group: "basic" },
  { type: "cron", group: "time" },
  { type: "interval", group: "time" },
  { type: "multi_cron", group: "time" },
  { type: "events", group: "time" },
  { type: "assets", group: "assets" },
  { type: "assets_or_time", group: "assets" },
  { type: "assets_and_time", group: "assets", minAirflow: "3.4" },
  { type: "partitioned_cron", group: "partitions", minAirflow: "3.2" },
  { type: "partitioned_assets", group: "partitions", minAirflow: "3.2" },
];

export const TIME_SCHEDULE_TYPES: TimeScheduleType[] = ["preset", "cron", "interval", "multi_cron", "events"];

/** Airflow >= 3.4: `batch_asset_events` on asset schedules. */
export const BATCH_ASSET_EVENTS_MIN = "3.4";

export const PRESETS: PresetSchedule["preset"][] = [
  "@once",
  "@continuous",
  "@hourly",
  "@daily",
  "@weekly",
  "@monthly",
  "@quarterly",
  "@yearly",
];

export const PARTITION_MAPPERS: PartitionMapper[] = [
  "identity",
  "start_of_hour",
  "start_of_day",
  "start_of_week",
  "start_of_month",
  "start_of_quarter",
  "start_of_year",
];

/** Runs are driven (only) by asset events: no time based logical date. */
export const isAssetSchedule = (type: ScheduleType | null | undefined): boolean =>
  type === "assets" || type === "partitioned_assets";

export const isTimeScheduleType = (type: string): type is TimeScheduleType =>
  (TIME_SCHEDULE_TYPES as string[]).includes(type);

// --------------------------------------------------------------------------- assets

export const assetLeaf = (name = "", extra: Partial<AssetRef> = {}): AssetLeaf => ({
  op: "asset",
  asset: { name, ...extra },
});
export const aliasLeaf = (name = ""): AssetAliasLeaf => ({ op: "alias", name });
export const combine = (op: "all" | "any", items: AssetExprNode[]): AssetCombination => ({ op, items });

const defaultAssetExpr = (): AssetExpr => combine("all", [assetLeaf("my_asset")]);

/** Asset names (or URIs) and alias names referenced by an expression. */
export function collectAssetNames(expr: AssetExprNode | null | undefined): string[] {
  if (!expr) return [];
  if (expr.op === "asset") {
    const n = expr.asset.name || expr.asset.uri;
    return n ? [n] : [];
  }
  if (expr.op === "alias") return expr.name ? [expr.name] : [];
  return [...new Set(expr.items.flatMap(collectAssetNames))];
}

// --------------------------------------------------------------------------- defaults

const assetExprOf = (prev: Schedule | null | undefined): AssetExpr | null =>
  prev && "expression" in prev && typeof prev.expression === "object" ? prev.expression : null;

const timeOf = (prev: Schedule | null | undefined): TimeSchedule | null => {
  if (!prev) return null;
  if (prev.type === "assets_or_time" || prev.type === "assets_and_time") return prev.time;
  if (isTimeScheduleType(prev.type)) return prev as TimeSchedule;
  return null;
};

const cronOf = (prev: Schedule | null | undefined): string | null => {
  if (!prev) return null;
  if ((prev.type === "cron" || prev.type === "partitioned_cron") && prev.expression) return prev.expression;
  if (prev.type === "multi_cron") return prev.expressions[0] ?? null;
  return null;
};

const tzOf = (prev: Schedule | null | undefined): string | null =>
  prev && "timezone" in prev && typeof prev.timezone === "string" ? prev.timezone : null;

/**
 * A valid schedule of `type`. When `prev` is given, compatible parts (asset condition,
 * time schedule, cron expression, timezone) are carried over.
 */
export function defaultSchedule(type: ScheduleType, prev?: Schedule | null): Schedule {
  const cron = cronOf(prev) ?? "0 6 * * *";
  const tz = tzOf(prev);
  switch (type) {
    case "none":
      return { type };
    case "preset":
      return { type, preset: "@daily" };
    case "cron":
      return tz ? { type, expression: cron, timezone: tz } : { type, expression: cron };
    case "interval":
      return { type, seconds: 3600 };
    case "multi_cron":
      return tz ? { type, expressions: [cron], timezone: tz } : { type, expressions: [cron] };
    case "events":
      return { type, dates: [new Date(Date.UTC(new Date().getUTCFullYear() + 1, 0, 1)).toISOString()] };
    case "assets":
      return { type, expression: assetExprOf(prev) ?? defaultAssetExpr() };
    case "assets_or_time":
    case "assets_and_time":
      return {
        type,
        expression: assetExprOf(prev) ?? defaultAssetExpr(),
        time: timeOf(prev) ?? { type: "cron", expression: cron },
      };
    case "partitioned_cron":
      return { type, expression: cron, timezone: tz ?? "UTC" };
    case "partitioned_assets":
      return { type, expression: assetExprOf(prev) ?? defaultAssetExpr(), mapper: "identity" };
  }
}

// --------------------------------------------------------------------------- interval

type Hms = { h: number; m: number; s: number };

export function secondsToHms(total: number): Hms {
  const t = Math.max(0, Math.floor(total));
  return { h: Math.floor(t / 3600), m: Math.floor((t % 3600) / 60), s: t % 60 };
}

export const hmsToSeconds = ({ h, m, s }: Hms): number => h * 3600 + m * 60 + s;

// --------------------------------------------------------------------------- cron

export const CRON_PRESETS: { key: string; expression: string }[] = [
  { key: "everyMinute", expression: "* * * * *" },
  { key: "every5Minutes", expression: "*/5 * * * *" },
  { key: "every15Minutes", expression: "*/15 * * * *" },
  { key: "hourly", expression: "0 * * * *" },
  { key: "every6Hours", expression: "0 */6 * * *" },
  { key: "dailyMidnight", expression: "0 0 * * *" },
  { key: "daily6", expression: "0 6 * * *" },
  { key: "weekdays9", expression: "0 9 * * 1-5" },
  { key: "weeklyMonday", expression: "0 6 * * 1" },
  { key: "monthlyFirst", expression: "0 6 1 * *" },
  { key: "quarterly", expression: "0 6 1 */3 *" },
  { key: "yearly", expression: "0 0 1 1 *" },
];

export const CRON_FIELDS = ["minute", "hour", "dayOfMonth", "month", "dayOfWeek"] as const;
type CronFieldName = (typeof CRON_FIELDS)[number];

const CRON_ALIASES: Record<string, string> = {
  "@yearly": "0 0 1 1 *",
  "@annually": "0 0 1 1 *",
  "@monthly": "0 0 1 * *",
  "@weekly": "0 0 * * 0",
  "@daily": "0 0 * * *",
  "@midnight": "0 0 * * *",
  "@hourly": "0 * * * *",
};

const MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"];
const DAYS = ["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"];

const RANGES: Record<CronFieldName, { min: number; max: number; names?: string[]; offset?: number }> = {
  minute: { min: 0, max: 59 },
  hour: { min: 0, max: 23 },
  dayOfMonth: { min: 1, max: 31 },
  month: { min: 1, max: 12, names: MONTHS, offset: 1 },
  dayOfWeek: { min: 0, max: 7, names: DAYS, offset: 0 },
};

/** Splits a 5-field cron expression (missing fields are "*"). */
export function splitCron(expr: string): [string, string, string, string, string] {
  const parts = expr.trim().split(/\s+/);
  const f = (i: number) => parts[i] || "*";
  return [f(0), f(1), f(2), f(3), f(4)];
}

export const joinCron = (fields: readonly string[]): string => fields.map((f) => f.trim() || "*").join(" ");

type CronError = { key: string; vars?: Record<string, string | number> };

function parseValue(raw: string, field: CronFieldName): number | null {
  const r = RANGES[field];
  if (/^\d+$/.test(raw)) return Number(raw);
  const idx = r.names?.indexOf(raw.toUpperCase()) ?? -1;
  return idx >= 0 ? idx + (r.offset ?? 0) : null;
}

function validateField(raw: string, field: CronFieldName): CronError | null {
  const r = RANGES[field];
  const bad = (value: string): CronError => ({ key: "schedule.cron.errors.badValue", vars: { field, value } });
  if (!raw) return bad(raw);
  for (const item of raw.split(",")) {
    if (field === "dayOfMonth" && /^(L|\d{1,2}W|LW)$/i.test(item)) continue;
    if (field === "dayOfWeek" && /^(\w{1,3})(#[1-5]|L)$/i.test(item)) {
      const base = /^(\w{1,3})/.exec(item)?.[1] ?? "";
      const v = parseValue(base, field);
      if (v === null || v < r.min || v > r.max) return bad(item);
      continue;
    }
    const [range, step, ...rest] = item.split("/");
    if (rest.length || range === undefined || range === "") return bad(item);
    if (step !== undefined && (!/^\d+$/.test(step) || Number(step) < 1)) {
      return { key: "schedule.cron.errors.badStep", vars: { field, value: item } };
    }
    if (range === "*" || range === "?") continue;
    const bounds = range.split("-");
    if (bounds.length > 2) return bad(item);
    const nums = bounds.map((b) => parseValue(b, field));
    for (const n of nums) {
      if (n === null) return bad(item);
      if (n < r.min || n > r.max) {
        return { key: "schedule.cron.errors.outOfRange", vars: { field, value: item, min: r.min, max: r.max } };
      }
    }
    if (nums.length === 2 && (nums[0] as number) > (nums[1] as number) && field !== "dayOfWeek") {
      return { key: "schedule.cron.errors.badRange", vars: { field, value: item } };
    }
  }
  return null;
}

/** Validates a cron expression (5 fields, or an @-alias). Returns null when valid. */
export function validateCron(expr: string | null | undefined): CronError | null {
  const text = (expr ?? "").trim();
  if (!text) return { key: "schedule.cron.errors.empty" };
  if (text.startsWith("@")) {
    return text in CRON_ALIASES ? null : { key: "schedule.cron.errors.unknownAlias", vars: { value: text } };
  }
  const parts = text.split(/\s+/);
  if (parts.length !== 5 && parts.length !== 6) {
    return { key: "schedule.cron.errors.fieldCount", vars: { count: parts.length } };
  }
  for (let i = 0; i < 5; i++) {
    const err = validateField(parts[i] as string, CRON_FIELDS[i] as CronFieldName);
    if (err) return err;
  }
  if (parts.length === 6 && validateField(parts[5] as string, "minute")) {
    return { key: "schedule.cron.errors.badValue", vars: { field: "seconds", value: parts[5] as string } };
  }
  return null;
}

const pad = (n: number) => String(n).padStart(2, "0");

function nameOf(raw: string, field: CronFieldName, t: Translate): string {
  const v = parseValue(raw, field);
  if (v === null) return raw;
  if (field === "dayOfWeek") return t(`schedule.cron.dayNames.${DAYS[v % 7]?.toLowerCase()}`);
  if (field === "month") return t(`schedule.cron.monthNames.${MONTHS[v - 1]?.toLowerCase()}`);
  return String(v);
}

/** Describes one non-"*" field: "every 2", "Mon through Fri", "1, 15"… */
function describeList(raw: string, field: CronFieldName, t: Translate): string {
  const items = raw.split(",").map((item) => {
    const [range = "*", step] = item.split("/");
    let base: string;
    if (range === "*" || range === "?") base = "";
    else if (range.includes("-")) {
      const [a = "", b = ""] = range.split("-");
      base = t("schedule.cron.through", { from: nameOf(a, field, t), to: nameOf(b, field, t) });
    } else base = nameOf(range, field, t);
    if (step) {
      const every = t(`schedule.cron.everyN.${field}`, { count: Number(step) });
      return base ? t("schedule.cron.stepFrom", { every, range: base }) : every;
    }
    return base;
  });
  return items.join(", ");
}

/** Human readable description of a cron expression (null when invalid). */
export function describeCron(expr: string | null | undefined, t: Translate): string | null {
  const text = (expr ?? "").trim();
  if (validateCron(text)) return null;
  if (text.startsWith("@")) return t(`schedule.cron.alias.${text.slice(1)}`);
  const [min, hour, dom, mon, dow] = splitCron(text);
  const num = /^\d+$/;
  const parts: string[] = [];

  // time of day
  if (num.test(min) && num.test(hour)) {
    parts.push(t("schedule.cron.atTime", { time: `${pad(Number(hour))}:${pad(Number(min))}` }));
  } else if (min === "*" && hour === "*") {
    parts.push(t("schedule.cron.everyMinute"));
  } else if (num.test(min) && hour === "*") {
    parts.push(t("schedule.cron.everyHourAt", { minute: Number(min) }));
  } else {
    const minuteText =
      min === "*"
        ? t("schedule.cron.everyMinute")
        : /^\*\/\d+$/.test(min)
          ? describeList(min, "minute", t)
          : t("schedule.cron.atMinutes", { minutes: describeList(min, "minute", t) });
    parts.push(minuteText);
    if (hour !== "*") {
      parts.push(
        /^\*\/\d+$/.test(hour)
          ? describeList(hour, "hour", t)
          : t("schedule.cron.duringHours", { hours: describeList(hour, "hour", t) }),
      );
    }
  }

  // day
  const domSet = dom !== "*" && dom !== "?";
  const dowSet = dow !== "*" && dow !== "?";
  if (domSet && dowSet) {
    parts.push(
      t("schedule.cron.domOrDow", {
        days: describeList(dom, "dayOfMonth", t),
        weekdays: describeList(dow, "dayOfWeek", t),
      }),
    );
  } else if (domSet) {
    parts.push(t("schedule.cron.onDom", { days: describeList(dom, "dayOfMonth", t) }));
  } else if (dowSet) {
    parts.push(t("schedule.cron.onDow", { days: describeList(dow, "dayOfWeek", t) }));
  } else if (num.test(min) && num.test(hour)) {
    parts.push(t("schedule.cron.everyDay"));
  }

  if (mon !== "*") parts.push(t("schedule.cron.inMonths", { months: describeList(mon, "month", t) }));
  const text2 = parts.join(", ");
  return text2.charAt(0).toUpperCase() + text2.slice(1);
}

// --------------------------------------------------------------------------- timezones

const COMMON_TIMEZONES = [
  "UTC",
  "Europe/Istanbul",
  "Europe/London",
  "Europe/Berlin",
  "Europe/Paris",
  "Europe/Amsterdam",
  "Europe/Madrid",
  "Europe/Moscow",
  "America/New_York",
  "America/Chicago",
  "America/Denver",
  "America/Los_Angeles",
  "America/Sao_Paulo",
  "Asia/Dubai",
  "Asia/Kolkata",
  "Asia/Singapore",
  "Asia/Shanghai",
  "Asia/Tokyo",
  "Australia/Sydney",
  "Pacific/Auckland",
];

/** True for a timezone `Intl` accepts (an unknown one makes `Intl.DateTimeFormat` throw). */
export function isValidTimezone(tz: string | null | undefined): tz is string {
  if (!tz) return false;
  try {
    new Intl.DateTimeFormat("en", { timeZone: tz });
    return true;
  } catch {
    return false;
  }
}

/** Every IANA timezone known to the browser (common ones first). */
export function allTimezones(): string[] {
  let all: string[];
  try {
    const intl = Intl as unknown as { supportedValuesOf?: (key: string) => string[] };
    all = intl.supportedValuesOf?.("timeZone") ?? [];
  } catch {
    all = [];
  }
  return [...new Set([...COMMON_TIMEZONES, ...all])];
}
