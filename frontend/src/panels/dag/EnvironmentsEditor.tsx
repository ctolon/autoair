import { Badge, Box, Button, HStack, IconButton, Stack, Text } from "@chakra-ui/react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useShallow } from "zustand/react/shallow";
import { LuChevronDown, LuChevronRight, LuLayers, LuPlus, LuTrash2 } from "react-icons/lu";

import type { DagParam, DefaultArgs, EnvironmentOverlay } from "../../api/gen/types.gen";
import { normalizeParams, primaryType } from "../../lib/dagParams";
import type { Schedule } from "../../lib/schedule";
import { useBuilder } from "../../store/builder";
import { useAirflowGate } from "../../widgets/airflowMeta";
import { Autocomplete } from "../../widgets/Autocomplete";
import {
  FIELD_ATTR,
  Hint,
  NumberField,
  Panel,
  SelectField,
  StringListField,
  SwitchField,
  TextField,
} from "../../widgets/fields";
import { EnvironmentMappingEditor } from "./EnvironmentMappingEditor";
import { TypedValueInput } from "./ParamsEditor";
import { ScheduleEditor } from "./ScheduleEditor";

const ENV_NAME = /^[A-Za-z0-9_.-]+$/;

const isObject = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);

/** The spec types overlays loosely (`unknown | EnvironmentOverlay`); treat non-objects as empty. */
const asOverlay = (v: unknown): EnvironmentOverlay => (isObject(v) ? (v as EnvironmentOverlay) : {});

const asMapping = (v: unknown): Record<string, string> =>
  isObject(v)
    ? Object.fromEntries(Object.entries(v).filter((e): e is [string, string] => typeof e[1] === "string"))
    : {};

/** Removes keys that mean "inherit" so the overlay only holds overrides. */
function compactOverlay(o: EnvironmentOverlay): EnvironmentOverlay {
  const out: EnvironmentOverlay = {};
  if (o.schedule) out.schedule = o.schedule;
  if (o.is_paused_upon_creation != null) out.is_paused_upon_creation = o.is_paused_upon_creation;
  if (o.tags != null) out.tags = o.tags;
  if (o.max_active_runs != null) out.max_active_runs = o.max_active_runs;
  if (o.default_args) {
    const da = Object.fromEntries(Object.entries(o.default_args).filter(([, v]) => v != null && v !== ""));
    if (Object.keys(da).length) out.default_args = da as DefaultArgs;
  }
  if (o.params_defaults && Object.keys(o.params_defaults).length) out.params_defaults = o.params_defaults;
  if (o.connections && Object.keys(o.connections).length) out.connections = o.connections;
  if (o.variables && Object.keys(o.variables).length) out.variables = o.variables;
  return out;
}

function OverlayEditor({
  name,
  value,
  onChange,
  baseSchedule,
  params,
}: {
  name: string;
  value: EnvironmentOverlay;
  onChange: (v: EnvironmentOverlay) => void;
  baseSchedule: Schedule | null | undefined;
  params: Record<string, DagParam>;
}) {
  const { t } = useTranslation("inspector");
  const set = (patch: Partial<EnvironmentOverlay>) => onChange(compactOverlay({ ...value, ...patch }));
  const da: DefaultArgs = value.default_args ?? {};
  const setDa = (patch: Partial<DefaultArgs>) => set({ default_args: { ...da, ...patch } });
  const pd = value.params_defaults ?? {};
  const field = `environments.${name}`;
  return (
    <Stack gap={2} pt={2}>
      <SwitchField
        help={t("dag.env.overrideScheduleHelp")}
        label={t("dag.env.overrideSchedule")}
        field={`${field}.schedule`}
        checked={!!value.schedule}
        onChange={(on) => set({ schedule: on ? (baseSchedule ?? { type: "none" }) : null })}
      />
      {value.schedule ? (
        <Box borderLeftWidth="2px" pl={2}>
          <ScheduleEditor
            value={value.schedule}
            onChange={(schedule: Schedule) => set({ schedule })}
            field={`${field}.schedule`}
          />
        </Box>
      ) : null}
      <SelectField
        help={t("dag.env.pausedHelp")}
        label={t("dag.env.paused")}
        emptyLabel={t("dag.env.inherit")}
        value={value.is_paused_upon_creation == null ? null : value.is_paused_upon_creation ? "true" : "false"}
        options={[
          { value: "true", label: t("dag.env.pausedTrue") },
          { value: "false", label: t("dag.env.pausedFalse") },
        ]}
        onChange={(v) => set({ is_paused_upon_creation: v === null ? null : v === "true" })}
      />
      <SwitchField
        help={t("dag.env.overrideTagsHelp")}
        label={t("dag.env.overrideTags")}
        checked={value.tags != null}
        onChange={(on) => set({ tags: on ? [] : null })}
      />
      {value.tags != null ? (
        <StringListField
          help={t("dag.env.tagsHelp")}
          label={t("dag.env.tags")}
          value={value.tags}
          onChange={(tags) => set({ tags })}
        />
      ) : null}
      <NumberField
        help={t("dag.general.maxActiveRunsHelp")}
        label={t("dag.general.maxActiveRuns")}
        placeholder={t("dag.env.inherit")}
        min={1}
        value={value.max_active_runs}
        onChange={(v) => set({ max_active_runs: v })}
      />
      <Panel title={t("dag.env.defaultArgs")}>
        <HStack align="start">
          <TextField
            help={t("dag.defaults.ownerHelp")}
            label={t("dag.defaults.owner")}
            placeholder={t("dag.env.inherit")}
            value={da.owner}
            onChange={(v) => setDa({ owner: v })}
          />
          <NumberField
            help={t("dag.defaults.retriesHelp")}
            label={t("dag.defaults.retries")}
            placeholder={t("dag.env.inherit")}
            min={0}
            value={da.retries}
            onChange={(v) => setDa({ retries: v })}
          />
        </HStack>
        <NumberField
          dtype="timedelta (s)"
          help={t("dag.defaults.retryDelayHelp")}
          label={t("dag.defaults.retryDelay")}
          placeholder={t("dag.env.inherit")}
          min={0}
          value={da.retry_delay_seconds}
          onChange={(v) => setDa({ retry_delay_seconds: v })}
        />
        <HStack align="start">
          <TextField
            help={t("dag.defaults.poolHelp")}
            label={t("dag.defaults.pool")}
            placeholder={t("dag.env.inherit")}
            value={da.pool}
            onChange={(v) => setDa({ pool: v })}
          />
          <TextField
            help={t("dag.defaults.queueHelp")}
            label={t("dag.defaults.queue")}
            placeholder={t("dag.env.inherit")}
            value={da.queue}
            onChange={(v) => setDa({ queue: v })}
          />
        </HStack>
      </Panel>
      <EnvironmentMappingEditor
        env={name}
        connections={asMapping(value.connections)}
        variables={asMapping(value.variables)}
        onChange={(patch) => set(patch)}
      />
      {Object.keys(params).length ? (
        <Panel title={t("dag.env.paramsDefaults")}>
          <Hint>{t("dag.env.paramsDefaultsHelp")}</Hint>
          {Object.entries(params).map(([pname, p]) => (
            <TypedValueInput
              key={pname}
              label={pname}
              type={primaryType(p)}
              format={p.format}
              value={pd[pname]}
              onChange={(v) => {
                const rest = Object.fromEntries(Object.entries(pd).filter(([k]) => k !== pname));
                set({ params_defaults: v === undefined ? rest : { ...rest, [pname]: v } });
              }}
            />
          ))}
        </Panel>
      ) : null}
    </Stack>
  );
}

/** Per-environment overlays (`environments: {dev: {...}, prod: {...}}`) applied at deploy time. */
export function EnvironmentsEditor({
  value,
  onChange,
  baseSchedule,
  params,
}: {
  value: Record<string, unknown> | null | undefined;
  onChange: (v: Record<string, EnvironmentOverlay>) => void;
  baseSchedule: Schedule | null | undefined;
  params: Record<string, unknown> | null | undefined;
}) {
  const { t } = useTranslation("inspector");
  const { meta } = useAirflowGate();
  const envs: Record<string, EnvironmentOverlay> = Object.fromEntries(
    Object.entries(value ?? {}).map(([k, v]) => [k, asOverlay(v)]),
  );
  const declared = normalizeParams(params);
  const [open, setOpen] = useState<string | null>(null);
  const [newEnv, setNewEnv] = useState<string | null>(null);
  const names = Object.keys(envs);
  const newError =
    newEnv && !ENV_NAME.test(newEnv)
      ? t("dag.env.invalidName")
      : newEnv && names.includes(newEnv)
        ? t("dag.env.duplicate")
        : undefined;
  const add = (name: string | null) => {
    if (!name || !ENV_NAME.test(name) || names.includes(name)) return;
    onChange({ ...envs, [name]: {} });
    setOpen(name);
    setNewEnv(null);
  };
  const known = meta?.environments ?? [];
  return (
    <Stack gap={2} {...{ [FIELD_ATTR]: "environments" }}>
      <Hint>{t("dag.env.help")}</Hint>
      {names.length === 0 ? <Hint>{t("dag.env.empty")}</Hint> : null}
      {names.map((name) => {
        const overlay = envs[name] ?? {};
        const expanded = open === name;
        const count = Object.keys(overlay).length;
        return (
          <Box key={name} borderWidth="1px" rounded="md" px={2} py={1}>
            <HStack gap={1}>
              <IconButton
                aria-label={expanded ? t("dag.env.collapse", { name }) : t("dag.env.expand", { name })}
                aria-expanded={expanded}
                size="2xs"
                variant="ghost"
                onClick={() => setOpen(expanded ? null : name)}
              >
                {expanded ? <LuChevronDown /> : <LuChevronRight />}
              </IconButton>
              <LuLayers aria-hidden />
              <Text fontSize="xs" fontWeight="semibold" fontFamily="mono" flex="1" truncate>
                {name}
              </Text>
              {!known.includes(name) && known.length ? (
                <Badge size="xs" colorPalette="orange" title={t("dag.env.unknownHelp")}>
                  {t("dag.env.unknown")}
                </Badge>
              ) : null}
              <Badge size="xs" variant="subtle">
                {t("dag.env.overrides", { count })}
              </Badge>
              <IconButton
                aria-label={t("common.removeItem", { item: name })}
                size="2xs"
                variant="ghost"
                colorPalette="red"
                onClick={() => {
                  const { [name]: _removed, ...rest } = envs;
                  onChange(rest);
                }}
              >
                <LuTrash2 />
              </IconButton>
            </HStack>
            {expanded ? (
              <OverlayEditor
                name={name}
                value={overlay}
                baseSchedule={baseSchedule}
                params={declared}
                onChange={(o) => onChange({ ...envs, [name]: o })}
              />
            ) : null}
          </Box>
        );
      })}
      <HStack align="end" gap={1}>
        <Box flex="1">
          <Autocomplete
            help={t("dag.env.addLabelHelp")}
            label={t("dag.env.addLabel")}
            size="xs"
            mono
            value={newEnv}
            error={newError}
            options={known.filter((e) => !names.includes(e)).map((e) => ({ value: e }))}
            onChange={setNewEnv}
            onSelect={(o) => add(o.value)}
            placeholder="prod"
          />
        </Box>
        <Button size="xs" variant="outline" disabled={!newEnv || !!newError} onClick={() => add(newEnv)}>
          <LuPlus /> {t("dag.env.add")}
        </Button>
      </HStack>
    </Stack>
  );
}

/** Which environment overlay preview / deploy use (store.environment). */
export function EnvironmentSelector() {
  const { t } = useTranslation("inspector");
  const { meta } = useAirflowGate();
  const environment = useBuilder((s) => s.environment);
  const setEnvironment = useBuilder((s) => s.setEnvironment);
  const specEnvs = useBuilder(useShallow((s) => Object.keys(s.dag.environments ?? {})));
  const options = [...new Set([...(meta?.environments ?? []), ...specEnvs])];
  if (environment && !options.includes(environment)) options.push(environment);
  return (
    <Stack gap={1}>
      <SelectField
        label={t("dag.env.selector")}
        emptyLabel={t("dag.env.base")}
        value={environment}
        options={options.map((e) => ({
          value: e,
          label: specEnvs.includes(e) ? e : t("dag.env.noOverlay", { env: e }),
        }))}
        onChange={setEnvironment}
        help={t("dag.env.selectorHelp")}
      />
      {meta?.default_environment ? <Hint>{t("dag.env.serverDefault", { env: meta.default_environment })}</Hint> : null}
    </Stack>
  );
}
