import { Badge, Box, Field, Flex, HStack, Heading, Stack, Tabs, Text } from "@chakra-ui/react";
import { useQuery } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  LuBell,
  LuBookOpen,
  LuCalendarClock,
  LuCircleAlert,
  LuLayers,
  LuSettings,
  LuSlidersHorizontal,
  LuTriangleAlert,
  LuWrench,
} from "react-icons/lu";
import { useShallow } from "zustand/react/shallow";
import { problemText } from "../lib/problemMessages";

import { listPoolsOptions } from "../api/gen/@tanstack/react-query.gen";
import type { DefaultArgs, Problem } from "../api/gen/types.gen";
import type { Schedule } from "../lib/schedule";
import { useBuilder } from "../store/builder";
import { useFocusRequest } from "../store/focus";
import { useAirflowGate } from "../widgets/airflowMeta";
import { Autocomplete } from "../widgets/Autocomplete";
import { CallbacksEditor, DAG_CALLBACK_EVENTS } from "../widgets/CallbacksEditor";
import { CodeEditor } from "../widgets/code";
import {
  DateTimeField,
  FIELD_ATTR,
  Hint,
  KeyValueField,
  NumberField,
  Panel,
  SelectField,
  StringListField,
  SwitchField,
  TextField,
  focusField,
} from "../widgets/fields";
import { VersionBadge } from "../widgets/VersionBadge";
import { DeadlinesEditor } from "./dag/DeadlinesEditor";
import { EnvironmentSelector, EnvironmentsEditor } from "./dag/EnvironmentsEditor";
import { DagParamsEditor } from "./dag/ParamsEditor";
import { ScheduleEditor, TimezoneField } from "./dag/ScheduleEditor";
import { useTeamQuery } from "../store/team";

type DagTab = "general" | "schedule" | "params" | "defaults" | "alerts" | "environments" | "docs";

const TAB_ICONS: Record<DagTab, typeof LuSettings> = {
  general: LuSettings,
  schedule: LuCalendarClock,
  params: LuSlidersHorizontal,
  defaults: LuWrench,
  alerts: LuBell,
  environments: LuLayers,
  docs: LuBookOpen,
};
const TABS = Object.keys(TAB_ICONS) as DagTab[];

const FIELD_TABS: [prefix: string, tab: DagTab][] = [
  ["schedule", "schedule"],
  ["start_date", "schedule"],
  ["end_date", "schedule"],
  ["catchup", "schedule"],
  ["params", "params"],
  ["default_args", "defaults"],
  ["callbacks", "alerts"],
  ["deadlines", "alerts"],
  ["environments", "environments"],
  ["doc_md", "docs"],
];

/** Tab of the DAG settings panel that edits a spec field path. */
function tabForField(field: string | null | undefined): DagTab {
  if (!field) return "general";
  const hit = FIELD_TABS.find(([p]) => field === p || field.startsWith(`${p}.`) || field.startsWith(`${p}[`));
  return hit ? hit[1] : "general";
}

const YAML_MIN_AIRFLOW = "3.4";
const RERUN_LATEST_MIN_AIRFLOW = "3.3";

function GeneralTab() {
  const { t } = useTranslation("inspector");
  const dag = useBuilder((s) => s.dag);
  const setDag = useBuilder((s) => s.setDag);
  const errors = useBuilder(
    useShallow((s) =>
      Object.fromEntries(
        s.problems.flatMap((p) => (!p.node_id && p.field && p.severity === "error" ? [[p.field, problemText(p)]] : [])),
      ),
    ),
  ) as Record<string, string>;
  const err = (field: string): string | undefined => errors[field];
  const unsupportedInYaml = useBuilder((s) => s.nodes.some((n) => n.type === "task_group" || n.type === "python_task"));
  const { supports } = useAirflowGate();
  const mode = dag.output_mode ?? "python";
  return (
    <Stack gap={2}>
      <TextField
        help={t("dag.general.dagIdHelp")}
        label={t("dag.general.dagId")}
        required
        mono
        field="dag_id"
        value={dag.dag_id}
        error={err("dag_id")}
        onChange={(v) => setDag({ dag_id: v ?? "" })}
      />
      <TextField
        help={t("dag.general.displayNameHelp")}
        label={t("dag.general.displayName")}
        field="display_name"
        value={dag.display_name}
        onChange={(v) => setDag({ display_name: v })}
      />
      <TextField
        help={t("dag.general.descriptionHelp")}
        label={t("dag.general.description")}
        field="description"
        error={err("description")}
        value={dag.description}
        onChange={(v) => setDag({ description: v })}
      />
      <StringListField
        help={t("dag.general.tagsHelp")}
        label={t("dag.general.tags")}
        field="tags"
        error={err("tags")}
        value={dag.tags ?? []}
        addLabel={t("dag.general.addTag")}
        onChange={(tags) => setDag({ tags })}
      />
      <Stack gap={1}>
        <SelectField
          help={t("dag.general.outputModeHelp")}
          label={t("dag.general.outputMode")}
          field="output_mode"
          value={mode}
          options={[
            { value: "python", label: t("dag.general.output.python") },
            { value: "loader", label: t("dag.general.output.loader") },
            {
              value: "yaml",
              label: t("dag.general.output.yaml"),
              disabled: !supports(YAML_MIN_AIRFLOW) && mode !== "yaml",
            },
          ]}
          onChange={(v) => setDag({ output_mode: v ?? "python" })}
        />
        {mode === "yaml" || !supports(YAML_MIN_AIRFLOW) ? (
          <HStack gap={1} wrap="wrap">
            <Text fontSize="xs" color="fg.muted">
              YAML:
            </Text>
            <VersionBadge min={YAML_MIN_AIRFLOW} neutral />
          </HStack>
        ) : null}
        {mode === "yaml" ? (
          <Hint tone={unsupportedInYaml ? "warning" : "muted"}>
            {t("dag.general.yamlNote")}
            {unsupportedInYaml ? ` ${t("dag.general.yamlUnsupportedNodes")}` : ""}
          </Hint>
        ) : null}
      </Stack>
      {/* Two columns when there is room; stacked in a narrow inspector (long labels wrap). */}
      <HStack align="start" wrap="wrap" css={{ "& > *": { flex: "1 1 210px", minWidth: 0 } }}>
        <TextField
          help={t("dag.general.teamHelp")}
          label={t("dag.general.team")}
          field="team"
          value={dag.team}
          onChange={(v) => setDag({ team: v })}
        />
        <SelectField
          label={t("dag.general.lineage")}
          help={t("behaviour.lineageHelp")}
          field="lineage"
          value={dag.lineage ?? "inherit"}
          options={[
            { value: "inherit", label: t("behaviour.lineageOptions.inherit") },
            { value: "enabled", label: t("behaviour.lineageOptions.enabled") },
            { value: "disabled", label: t("behaviour.lineageOptions.disabled") },
          ]}
          onChange={(v) => setDag({ lineage: v ?? "inherit" })}
        />
      </HStack>
      <KeyValueField
        label={t("dag.general.ownerLinks")}
        help={t("dag.general.ownerLinksHelp")}
        field="owner_links"
        keyPlaceholder={t("dag.general.owner")}
        valuePlaceholder="https://… / mailto:…"
        value={dag.owner_links ?? {}}
        onChange={(owner_links) => setDag({ owner_links })}
      />
      <Panel title={t("dag.general.runs")}>
        <HStack align="start">
          <NumberField
            help={t("dag.general.maxActiveRunsHelp")}
            label={t("dag.general.maxActiveRuns")}
            field="max_active_runs"
            min={1}
            value={dag.max_active_runs}
            onChange={(v) => setDag({ max_active_runs: v })}
          />
          <NumberField
            help={t("dag.general.maxActiveTasksHelp")}
            label={t("dag.general.maxActiveTasks")}
            field="max_active_tasks"
            min={1}
            value={dag.max_active_tasks}
            onChange={(v) => setDag({ max_active_tasks: v })}
          />
        </HStack>
        <HStack align="start">
          <NumberField
            dtype="timedelta (s)"
            help={t("dag.general.runTimeoutHelp")}
            label={t("dag.general.runTimeout")}
            field="dagrun_timeout_seconds"
            min={1}
            value={dag.dagrun_timeout_seconds}
            onChange={(v) => setDag({ dagrun_timeout_seconds: v })}
          />
          <NumberField
            help={t("dag.general.maxConsecutiveFailedHelp")}
            label={t("dag.general.maxConsecutiveFailed")}
            field="max_consecutive_failed_dag_runs"
            min={0}
            value={dag.max_consecutive_failed_dag_runs}
            onChange={(v) => setDag({ max_consecutive_failed_dag_runs: v })}
          />
        </HStack>
        <SwitchField
          help={t("dag.general.pausedUponCreationHelp")}
          label={t("dag.general.pausedUponCreation")}
          field="is_paused_upon_creation"
          checked={dag.is_paused_upon_creation ?? true}
          onChange={(v) => setDag({ is_paused_upon_creation: v })}
        />
        <SwitchField
          label={t("dag.general.failFast")}
          help={t("dag.general.failFastHelp")}
          field="fail_fast"
          checked={!!dag.fail_fast}
          onChange={(v) => setDag({ fail_fast: v || null })}
        />
        <SwitchField
          label={t("dag.general.nativeTemplates")}
          help={t("dag.general.nativeTemplatesHelp")}
          field="render_template_as_native_obj"
          checked={!!dag.render_template_as_native_obj}
          onChange={(v) => setDag({ render_template_as_native_obj: v || null })}
        />
        <Stack gap={0}>
          <SwitchField
            label={t("dag.general.rerunLatest")}
            help={t("dag.general.rerunLatestHelp")}
            field="rerun_with_latest_version"
            disabled={!supports(RERUN_LATEST_MIN_AIRFLOW) && !dag.rerun_with_latest_version}
            checked={!!dag.rerun_with_latest_version}
            onChange={(v) => setDag({ rerun_with_latest_version: v || null })}
          />
          <Box>
            <VersionBadge min={RERUN_LATEST_MIN_AIRFLOW} />
          </Box>
        </Stack>
      </Panel>
    </Stack>
  );
}

function ScheduleTab() {
  const { t } = useTranslation("inspector");
  const dag = useBuilder((s) => s.dag);
  const setDag = useBuilder((s) => s.setDag);
  /** Changing the DAG timezone keeps the dates' wall-clock values (they are local to it). */
  const setTimezone = (tz: string | null) => {
    const wallClock = (v: string | null | undefined) =>
      v ? (tz ? v.replace(/(\.\d+)?(Z|[+-]\d\d:\d\d)$/, "") : /Z|[+-]\d\d:\d\d$/.test(v) ? v : `${v}Z`) : v;
    setDag({ timezone: tz || null, start_date: wallClock(dag.start_date), end_date: wallClock(dag.end_date) });
  };
  return (
    <Stack gap={2}>
      <TimezoneField
        field="timezone"
        label={t("dag.schedule.timezone")}
        help={t("dag.schedule.timezoneHelp")}
        placeholder={t("dag.schedule.timezonePlaceholder")}
        value={dag.timezone}
        onChange={setTimezone}
      />
      <ScheduleEditor
        field="schedule"
        preview
        value={dag.schedule}
        onChange={(schedule: Schedule) => setDag({ schedule })}
      />
      {/* Wraps under each other when the inspector is narrow (a datetime input needs ~210px). */}
      <Flex gap={2} wrap="wrap" align="start" css={{ "& > *": { flex: "1 1 210px", minWidth: 0 } }}>
        <DateTimeField
          help={t("dag.schedule.startDateHelp")}
          label={t("dag.schedule.startDate")}
          field="start_date"
          timezone={dag.timezone}
          value={dag.start_date}
          onChange={(v) => setDag({ start_date: v })}
        />
        <DateTimeField
          help={t("dag.schedule.endDateHelp")}
          label={t("dag.schedule.endDate")}
          field="end_date"
          timezone={dag.timezone}
          value={dag.end_date}
          onChange={(v) => setDag({ end_date: v })}
        />
      </Flex>
      <SwitchField
        label={t("dag.schedule.catchup")}
        help={t("dag.schedule.catchupHelp")}
        field="catchup"
        checked={!!dag.catchup}
        onChange={(v) => setDag({ catchup: v })}
      />
    </Stack>
  );
}

function DefaultsTab() {
  const { t } = useTranslation("inspector");
  const da: DefaultArgs = useBuilder((s) => s.dag.default_args) ?? {};
  const setDag = useBuilder((s) => s.setDag);
  const ownerError = useBuilder((s) => s.problems.find((p) => !p.node_id && p.field === "default_args.owner")?.message);
  const pools = useQuery({ ...listPoolsOptions(useTeamQuery()), retry: false, staleTime: 60_000 });
  const setDa = (patch: Partial<DefaultArgs>) => setDag({ default_args: { ...da, ...patch } });
  return (
    <Stack gap={2}>
      <Hint>{t("dag.defaults.help")}</Hint>
      <HStack align="start">
        <TextField
          help={t("dag.defaults.ownerHelp")}
          label={t("dag.defaults.owner")}
          field="default_args.owner"
          error={ownerError}
          value={da.owner}
          onChange={(v) => setDa({ owner: v })}
        />
        <NumberField
          help={t("dag.defaults.retriesHelp")}
          label={t("dag.defaults.retries")}
          field="default_args.retries"
          min={0}
          value={da.retries}
          onChange={(v) => setDa({ retries: v })}
        />
      </HStack>
      <HStack align="start">
        <NumberField
          dtype="timedelta (s)"
          help={t("dag.defaults.retryDelayHelp")}
          label={t("dag.defaults.retryDelay")}
          field="default_args.retry_delay_seconds"
          min={0}
          value={da.retry_delay_seconds}
          onChange={(v) => setDa({ retry_delay_seconds: v })}
        />
        <NumberField
          dtype="timedelta (s)"
          help={t("dag.defaults.executionTimeoutHelp")}
          label={t("dag.defaults.executionTimeout")}
          field="default_args.execution_timeout_seconds"
          min={1}
          value={da.execution_timeout_seconds}
          onChange={(v) => setDa({ execution_timeout_seconds: v })}
        />
      </HStack>
      <HStack align="start">
        <Autocomplete
          help={t("dag.defaults.poolHelp")}
          label={t("dag.defaults.pool")}
          field="default_args.pool"
          value={da.pool}
          options={(pools.data ?? []).map((p) => ({
            value: p.name,
            description: p.slots != null ? t("dag.defaults.slots", { count: p.slots }) : (p.description ?? undefined),
          }))}
          onChange={(v) => setDa({ pool: v })}
        />
        <TextField
          help={t("dag.defaults.queueHelp")}
          label={t("dag.defaults.queue")}
          field="default_args.queue"
          value={da.queue}
          onChange={(v) => setDa({ queue: v })}
        />
      </HStack>
      <SwitchField
        label={t("dag.defaults.dependsOnPast")}
        help={t("dag.defaults.dependsOnPastHelp")}
        field="default_args.depends_on_past"
        checked={!!da.depends_on_past}
        onChange={(v) => setDa({ depends_on_past: v || null })}
      />
      <StringListField
        help={t("dag.defaults.emailHelp")}
        label={t("dag.defaults.email")}
        field="default_args.email"
        placeholder="team@example.com"
        value={da.email ?? []}
        onChange={(email) => setDa({ email: email.length ? email : null })}
      />
      <SwitchField
        help={t("dag.defaults.emailOnFailureHelp")}
        label={t("dag.defaults.emailOnFailure")}
        field="default_args.email_on_failure"
        checked={!!da.email_on_failure}
        onChange={(v) => setDa({ email_on_failure: v || null })}
      />
    </Stack>
  );
}

function AlertsTab() {
  const { t } = useTranslation("inspector");
  const callbacks = useBuilder((s) => s.dag.callbacks);
  const deadlines = useBuilder((s) => s.dag.deadlines);
  const setDag = useBuilder((s) => s.setDag);
  return (
    <Stack gap={4}>
      <Stack gap={2}>
        <Text fontSize="sm" fontWeight="semibold">
          {t("dag.alerts.callbacks")}
        </Text>
        <Box {...{ [FIELD_ATTR]: "callbacks" }}>
          <CallbacksEditor
            value={callbacks}
            events={DAG_CALLBACK_EVENTS}
            onChange={(cb) => setDag({ callbacks: cb })}
          />
        </Box>
      </Stack>
      <DeadlinesEditor value={deadlines} onChange={(d) => setDag({ deadlines: d })} />
    </Stack>
  );
}

function EnvironmentsTab() {
  const environments = useBuilder((s) => s.dag.environments);
  const schedule = useBuilder((s) => s.dag.schedule);
  const params = useBuilder((s) => s.dag.params);
  const setDag = useBuilder((s) => s.setDag);
  return (
    <Stack gap={3}>
      <EnvironmentSelector />
      <EnvironmentsEditor
        value={environments}
        baseSchedule={schedule}
        params={params}
        onChange={(envs) => setDag({ environments: envs })}
      />
    </Stack>
  );
}

function DocsTab() {
  const { t } = useTranslation("inspector");
  const doc = useBuilder((s) => s.dag.doc_md);
  const setDag = useBuilder((s) => s.setDag);
  return (
    <Field.Root {...{ [FIELD_ATTR]: "doc_md" }}>
      <Field.Label fontSize="xs">{t("dag.docs.label")}</Field.Label>
      <Box w="100%">
        <CodeEditor
          language="markdown"
          ariaLabel={t("dag.docs.label")}
          minHeight="12rem"
          maxHeight="32rem"
          value={doc ?? ""}
          onChange={(v) => setDag({ doc_md: v || null })}
        />
      </Box>
      <Field.HelperText fontSize="xs">{t("dag.docs.help")}</Field.HelperText>
    </Field.Root>
  );
}

function DagProblems({ onPick }: { onPick: (p: Problem) => void }) {
  const { t } = useTranslation("inspector");
  const problems = useBuilder(useShallow((s) => s.problems.filter((p) => !p.node_id)));
  if (!problems.length) return null;
  return (
    <Stack gap={1} role="list" aria-label={t("dag.problems")}>
      {problems.map((p, i) => {
        const Icon = p.severity === "error" ? LuCircleAlert : LuTriangleAlert;
        return (
          <HStack
            key={`${p.code}-${p.field ?? ""}-${i}`}
            role="listitem"
            as="button"
            textAlign="start"
            gap={2}
            px={2}
            py={1}
            rounded="md"
            fontSize="xs"
            bg={p.severity === "error" ? "red.subtle" : p.severity === "warning" ? "orange.subtle" : "bg.muted"}
            color={p.severity === "error" ? "red.fg" : p.severity === "warning" ? "orange.fg" : "fg.muted"}
            _hover={{ opacity: 0.85 }}
            onClick={() => onPick(p)}
            title={p.field ? t("dag.goToField", { field: p.field }) : undefined}
          >
            <Icon aria-hidden style={{ flexShrink: 0 }} />
            <Text flex="1">{problemText(p)}</Text>
            {p.field ? (
              <Badge size="xs" variant="outline" fontFamily="mono">
                {p.field}
              </Badge>
            ) : null}
          </HStack>
        );
      })}
    </Stack>
  );
}

/**
 * Settings of a raw code DAG: only what AutoAir lists, stores and deploys it under. Schedule,
 * default args and the rest live in the code (environment overlays do not apply to it).
 */
export function RawDagSettingsPanel() {
  const { t } = useTranslation("inspector");
  const dag = useBuilder((s) => s.dag);
  const setDag = useBuilder((s) => s.setDag);
  const errors = useBuilder(
    useShallow((s) =>
      Object.fromEntries(
        s.problems.flatMap((p) => (!p.node_id && p.field && p.severity === "error" ? [[p.field, problemText(p)]] : [])),
      ),
    ),
  ) as Record<string, string>;
  return (
    <Stack gap={2} p={3} data-testid="raw-dag-settings">
      <Heading size="sm">{t("dag.raw.title")}</Heading>
      <Hint>{t("dag.raw.help")}</Hint>
      <TextField
        help={t("dag.raw.dagIdHelp")}
        label={t("dag.general.dagId")}
        required
        mono
        field="dag_id"
        value={dag.dag_id}
        error={errors.dag_id}
        onChange={(v) => setDag({ dag_id: v ?? "" })}
      />
      <TextField
        help={t("dag.raw.displayNameHelp")}
        label={t("dag.general.displayName")}
        field="display_name"
        value={dag.display_name}
        onChange={(v) => setDag({ display_name: v })}
      />
      <TextField
        help={t("dag.raw.descriptionHelp")}
        label={t("dag.general.description")}
        field="description"
        value={dag.description}
        onChange={(v) => setDag({ description: v })}
      />
      <StringListField
        help={t("dag.raw.tagsHelp")}
        label={t("dag.general.tags")}
        field="tags"
        value={dag.tags ?? []}
        addLabel={t("dag.general.addTag")}
        onChange={(tags) => setDag({ tags })}
      />
      <TextField
        help={t("dag.general.teamHelp")}
        label={t("dag.general.team")}
        field="team"
        value={dag.team}
        onChange={(v) => setDag({ team: v })}
      />
      <Hint>{t("dag.raw.environments")}</Hint>
    </Stack>
  );
}

export function DagSettingsPanel() {
  const { t } = useTranslation("inspector");
  const [tab, setTab] = useState<DagTab>("general");
  const [pendingFocus, setPendingFocus] = useState<string | null>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const counts = useBuilder(
    useShallow((s) => {
      const out: Partial<Record<DagTab, number>> = {};
      for (const p of s.problems) {
        if (p.node_id || p.severity === "info") continue;
        const tb = tabForField(p.field);
        out[tb] = (out[tb] ?? 0) + 1;
      }
      return out;
    }),
  );

  const request = useFocusRequest((s) => s.request);
  const [handled, setHandled] = useState(0);
  if (request && request.nodeId === null && request.seq !== handled) {
    setHandled(request.seq);
    setTab(tabForField(request.field));
    if (request.field) setPendingFocus(request.field);
  }
  useEffect(() => {
    if (request && request.seq === handled) useFocusRequest.getState().clear();
  }, [request, handled]);

  useEffect(() => {
    if (!pendingFocus) return;
    // Wait for the newly selected tab (and lazy editors) to render.
    const timer = setTimeout(() => {
      focusField(pendingFocus, contentRef.current ?? document);
      setPendingFocus(null);
    }, 60);
    return () => clearTimeout(timer);
  }, [pendingFocus, tab]);

  return (
    <Stack gap={2} p={3}>
      <Heading size="sm">{t("dag.title")}</Heading>
      <Text fontSize="xs" color="fg.muted">
        {t("dag.selectHint")}
      </Text>
      <DagProblems
        onPick={(p) => {
          setTab(tabForField(p.field));
          if (p.field) setPendingFocus(p.field);
        }}
      />
      <Tabs.Root value={tab} onValueChange={(e) => setTab(e.value as DagTab)} size="sm" variant="line" lazyMount>
        <Tabs.List flexWrap="wrap" rowGap={0}>
          {TABS.map((id) => {
            const Icon = TAB_ICONS[id];
            const n = counts[id];
            return (
              <Tabs.Trigger key={id} value={id} px={2} fontSize="xs" gap={1} flexShrink={0}>
                <Icon aria-hidden />
                {t(`dag.tabs.${id}`)}
                {n ? (
                  <Badge size="xs" colorPalette="red" aria-label={t("dag.problemCount", { count: n })}>
                    {n}
                  </Badge>
                ) : null}
              </Tabs.Trigger>
            );
          })}
        </Tabs.List>
        <Box ref={contentRef}>
          <Tabs.Content value="general">
            <GeneralTab />
          </Tabs.Content>
          <Tabs.Content value="schedule">
            <ScheduleTab />
          </Tabs.Content>
          <Tabs.Content value="params">
            <ParamsTab />
          </Tabs.Content>
          <Tabs.Content value="defaults">
            <DefaultsTab />
          </Tabs.Content>
          <Tabs.Content value="alerts">
            <AlertsTab />
          </Tabs.Content>
          <Tabs.Content value="environments">
            <EnvironmentsTab />
          </Tabs.Content>
          <Tabs.Content value="docs">
            <DocsTab />
          </Tabs.Content>
        </Box>
      </Tabs.Root>
    </Stack>
  );
}

function ParamsTab() {
  const params = useBuilder((s) => s.dag.params);
  const setDag = useBuilder((s) => s.setDag);
  return <DagParamsEditor field="params" value={params ?? {}} showPreview onChange={(p) => setDag({ params: p })} />;
}
