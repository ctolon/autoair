import i18next from "i18next";
import { describe, expect, it } from "vitest";

import en from "../i18n/locales/en/inspector.json";
import {
  SCHEDULE_TYPES,
  type Translate,
  assetLeaf,
  aliasLeaf,
  collectAssetNames,
  combine,
  defaultSchedule,
  describeCron,
  hmsToSeconds,
  joinCron,
  secondsToHms,
  splitCron,
  validateCron,
} from "./schedule";

const i18n = i18next.createInstance();
void i18n.init({
  lng: "en",
  resources: { en: { inspector: en } },
  defaultNS: "inspector",
  interpolation: { escapeValue: false },
  initAsync: false,
});
const t: Translate = (key, vars) => i18n.t(key, vars ?? {});

describe("validateCron", () => {
  it.each(["0 6 * * 1-5", "*/15 * * * *", "0 0 1 JAN MON", "0 9-17/2 * * MON-FRI", "0 0 L * *", "@daily"])(
    "accepts %s",
    (expr) => expect(validateCron(expr)).toBeNull(),
  );

  it.each([
    ["", "schedule.cron.errors.empty"],
    ["0 6 * *", "schedule.cron.errors.fieldCount"],
    ["61 * * * *", "schedule.cron.errors.outOfRange"],
    ["a * * * *", "schedule.cron.errors.badValue"],
    ["*/0 * * * *", "schedule.cron.errors.badStep"],
    ["0 10-2 * * *", "schedule.cron.errors.badRange"],
    ["@sometimes", "schedule.cron.errors.unknownAlias"],
  ])("rejects %j", (expr, key) => expect(validateCron(expr)?.key).toBe(key));
});

describe("describeCron", () => {
  it.each([
    ["0 6 * * *", "At 06:00, every day"],
    ["0 9 * * 1-5", "At 09:00, on Monday through Friday"],
    ["*/15 * * * *", "Every 15 minutes"],
    ["30 * * * *", "At minute 30 of every hour"],
    ["0 6 1 * *", "At 06:00, on day 1 of the month"],
    ["0 */6 * * *", "At minute 0, every 6 hours"],
    ["0 0 1 1,7 *", "At 00:00, on day 1 of the month, in January, July"],
    ["@hourly", "At the start of every hour"],
  ])("%s → %s", (expr, text) => expect(describeCron(expr, t)).toBe(text));

  it("returns null for invalid expressions", () => {
    expect(describeCron("nope", t)).toBeNull();
  });
});

describe("cron fields", () => {
  it("splits and joins", () => {
    expect(splitCron("0 6")).toEqual(["0", "6", "*", "*", "*"]);
    expect(joinCron(["0", "", "*", "*", "1"])).toBe("0 * * * 1");
  });
});

describe("defaultSchedule", () => {
  it.each(SCHEDULE_TYPES.map((s) => s.type))("builds a %s schedule", (type) => {
    expect(defaultSchedule(type).type).toBe(type);
  });

  it("carries the asset condition and time schedule over", () => {
    const expression = combine("any", [assetLeaf("a"), aliasLeaf("b")]);
    const next = defaultSchedule("assets_or_time", { type: "assets", expression });
    expect(next).toMatchObject({ type: "assets_or_time", expression, time: { type: "cron" } });
    const back = defaultSchedule("cron", {
      type: "partitioned_cron",
      expression: "5 4 * * *",
      timezone: "Europe/Istanbul",
    });
    expect(back).toEqual({ type: "cron", expression: "5 4 * * *", timezone: "Europe/Istanbul" });
  });

  it("defaults partitioned cron to UTC", () => {
    expect(defaultSchedule("partitioned_cron")).toMatchObject({ timezone: "UTC" });
  });
});

describe("intervals and assets", () => {
  it("splits seconds into h/m/s", () => {
    expect(secondsToHms(3725)).toEqual({ h: 1, m: 2, s: 5 });
    expect(hmsToSeconds({ h: 1, m: 2, s: 5 })).toBe(3725);
  });

  it("collects asset names", () => {
    const expr = combine("all", [assetLeaf("a"), combine("any", [assetLeaf("", { uri: "s3://x" }), aliasLeaf("al")])]);
    expect(collectAssetNames(expr)).toEqual(["a", "s3://x", "al"]);
  });
});
