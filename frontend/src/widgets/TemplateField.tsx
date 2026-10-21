import { Box, Button, Field, HStack, Input, Menu, Portal, Text } from "@chakra-ui/react";
import { useQuery } from "@tanstack/react-query";
import { type ReactNode, useEffect, useMemo, useRef } from "react";
import { useTranslation } from "react-i18next";
import { LuBraces, LuEye } from "react-icons/lu";
import { create } from "zustand";

import { listConnectionsOptions, listVariablesOptions } from "../api/gen/@tanstack/react-query.gen";
import { upstreamTasks } from "../canvas/graph";
import { isTemplateString, renderPreview, sampleContext, templateTimeVariables } from "../lib/jinja";
import { detectReturnKeys, effectiveTaskId } from "../lib/xcom";
import { useBuilder } from "../store/builder";
import { CodeEditor, type CodeEditorHandle, type CodeLanguage, type JinjaCompletionData } from "./code";
import { useEditingNode } from "./context";
import { FIELD_ATTR, Help, Hint, Label } from "./fields";
import { useInsertTarget, xcomTemplate } from "./xcomDnd";
import { useTeamQuery } from "../store/team";

// ----------------------------------------------------------------------------- shared state

const todayUtc = () => new Date().toISOString().slice(0, 10);

type PreviewDateState = { date: string; setDate: (d: string) => void };

/** Sample logical date (YYYY-MM-DD, UTC) shared by every template preview. */
const usePreviewDate = create<PreviewDateState>()((set) => ({
  date: todayUtc(),
  setDate: (date) => set({ date: date || todayUtc() }),
}));

const PARAM_KEYS = new Set([
  "default",
  "type",
  "title",
  "description",
  "description_md",
  "section",
  "format",
  "enum",
  "values_display",
  "examples",
  "minimum",
  "maximum",
  "min_length",
  "max_length",
  "pattern",
  "items",
  "const",
]);

/** Default value of a DAG param (a `DagParam` object or a bare value). */
function paramDefault(v: unknown): unknown {
  if (v && typeof v === "object" && !Array.isArray(v) && Object.keys(v).some((k) => PARAM_KEYS.has(k))) {
    return (v as { default?: unknown }).default ?? null;
  }
  return v;
}

function paramDetail(v: unknown): string | undefined {
  if (v && typeof v === "object" && !Array.isArray(v) && "type" in v) {
    const t = (v as { type?: unknown }).type;
    return Array.isArray(t) ? t.join(" | ") : typeof t === "string" ? t : undefined;
  }
  return undefined;
}

// ----------------------------------------------------------------------------- completion data

/**
 * Everything the Jinja completion offers: declared DAG params, Airflow Variables and
 * connections, and the outputs of tasks upstream of the node being edited.
 */
function useJinjaCompletionData(): JinjaCompletionData {
  const nodeId = useEditingNode();
  const params = useBuilder((s) => s.dag.params);
  const nodes = useBuilder((s) => s.nodes);
  const edges = useBuilder((s) => s.edges);
  const variables = useQuery({ ...listVariablesOptions(useTeamQuery()), retry: false, staleTime: 60_000 });
  const connections = useQuery({ ...listConnectionsOptions(useTeamQuery()), retry: false, staleTime: 60_000 });
  return useMemo(
    () => ({
      params: Object.entries(params ?? {}).map(([name, p]) => ({ name, detail: paramDetail(p) })),
      variables: (variables.data ?? []).map((v) => v.key),
      connections: (connections.data ?? []).map((c) => ({ id: c.connection_id, type: c.conn_type })),
      upstream: nodeId
        ? upstreamTasks(nodes, edges, nodeId).map((n) => ({
            taskId: effectiveTaskId(nodes, n.id),
            keys: n.type === "python_task" && n.data.spec.multiple_outputs ? detectReturnKeys(n.data.spec.code) : [],
          }))
        : [],
    }),
    [params, variables.data, connections.data, nodes, edges, nodeId],
  );
}

// ----------------------------------------------------------------------------- helper menu

type Snippet = { label: string; text: string; detail?: string | undefined };
type SnippetGroup = { title: string; items: Snippet[] };

const MAX_PER_GROUP = 40;

function InsertMenu({
  data,
  onInsert,
  disabled,
}: {
  data: JinjaCompletionData;
  onInsert: (text: string) => void;
  disabled?: boolean | undefined;
}) {
  const { t } = useTranslation("inspector");
  const groups: SnippetGroup[] = [
    {
      title: t("jinja.menu.context"),
      items: [
        { label: "ds", text: "{{ ds }}", detail: t("jinja.menu.ds") },
        { label: "ds_nodash", text: "{{ ds_nodash }}" },
        { label: "ts", text: "{{ ts }}" },
        { label: "logical_date", text: "{{ logical_date }}" },
        { label: "data_interval_start", text: "{{ data_interval_start }}" },
        { label: "data_interval_end", text: "{{ data_interval_end }}" },
        { label: "run_id", text: "{{ run_id }}" },
        { label: "dag_run.conf", text: "{{ dag_run.conf.get('key') }}" },
        { label: "macros.ds_add", text: "{{ macros.ds_add(ds, 1) }}", detail: t("jinja.menu.dsAdd") },
        { label: "macros.ds_format", text: "{{ macros.ds_format(ds, '%Y-%m-%d', '%Y/%m/%d') }}" },
        { label: "partition_key", text: "{{ partition_key }}" },
      ],
    },
    {
      title: t("jinja.menu.params"),
      items: data.params.map((p) => ({ label: p.name, text: `{{ params.${p.name} }}`, detail: p.detail })),
    },
    {
      title: t("jinja.menu.upstream"),
      items: data.upstream.flatMap((u) => [
        { label: u.taskId, text: xcomTemplate({ taskId: u.taskId }), detail: t("jinja.menu.returnValue") },
        ...u.keys.map((key) => ({ label: `${u.taskId}[${key}]`, text: xcomTemplate({ taskId: u.taskId, key }) })),
      ]),
    },
    {
      title: t("jinja.menu.variables"),
      items: data.variables.map((k) => ({ label: k, text: `{{ var.value.${k} }}` })),
    },
    {
      title: t("jinja.menu.connections"),
      items: data.connections.map((c) => ({
        label: c.id,
        text: `{{ conn.${c.id}.host }}`,
        detail: c.type ?? undefined,
      })),
    },
  ];
  const lookup = new Map<string, string>();
  groups.forEach((g, gi) => g.items.forEach((s, si) => lookup.set(`${gi}:${si}`, s.text)));

  return (
    <Menu.Root
      positioning={{ placement: "bottom-end" }}
      onSelect={(d) => {
        const text = lookup.get(d.value);
        if (text) onInsert(text);
      }}
    >
      <Menu.Trigger asChild>
        <Button size="2xs" variant="ghost" disabled={disabled ?? false} aria-label={t("jinja.menu.trigger")}>
          <LuBraces /> {t("jinja.menu.insert")}
        </Button>
      </Menu.Trigger>
      <Portal>
        <Menu.Positioner>
          <Menu.Content maxH="320px" overflowY="auto" minW="240px">
            {groups.map((g, gi) => (
              <Menu.ItemGroup key={g.title}>
                <Menu.ItemGroupLabel fontSize="xs">{g.title}</Menu.ItemGroupLabel>
                {g.items.length === 0 ? (
                  <Text px={2} pb={1} fontSize="xs" color="fg.muted">
                    {t("jinja.menu.empty")}
                  </Text>
                ) : null}
                {g.items.slice(0, MAX_PER_GROUP).map((s, si) => (
                  <Menu.Item key={`${gi}:${si}`} value={`${gi}:${si}`} fontSize="xs">
                    <Text fontFamily="mono" truncate flex="1">
                      {s.label}
                    </Text>
                    {s.detail ? (
                      <Text color="fg.muted" fontSize="2xs" truncate maxW="40%">
                        {s.detail}
                      </Text>
                    ) : null}
                  </Menu.Item>
                ))}
                {g.items.length > MAX_PER_GROUP ? (
                  <Text px={2} pb={1} fontSize="2xs" color="fg.muted">
                    {t("jinja.menu.more", { count: g.items.length - MAX_PER_GROUP })}
                  </Text>
                ) : null}
              </Menu.ItemGroup>
            ))}
          </Menu.Content>
        </Menu.Positioner>
      </Portal>
    </Menu.Root>
  );
}

// ----------------------------------------------------------------------------- preview

function TemplatePreview({ template }: { template: string }) {
  const { t, i18n } = useTranslation("inspector");
  const nodeId = useEditingNode();
  const date = usePreviewDate((s) => s.date);
  const setDate = usePreviewDate((s) => s.setDate);
  const dagId = useBuilder((s) => s.dag.dag_id);
  const params = useBuilder((s) => s.dag.params);
  const taskId = useBuilder((s) => (nodeId ? effectiveTaskId(s.nodes, nodeId) : undefined));
  const result = useMemo(() => {
    const logical = new Date(`${date}T00:00:00Z`);
    const ctx = sampleContext({
      logicalDate: Number.isNaN(logical.getTime()) ? new Date() : logical,
      dagId,
      taskId,
      params: Object.fromEntries(Object.entries(params ?? {}).map(([k, v]) => [k, paramDefault(v)])),
    });
    return renderPreview(template, ctx);
  }, [template, date, dagId, taskId, params]);

  return (
    <Box
      w="100%"
      mt={1}
      px={2}
      py={1}
      rounded="sm"
      bg="bg.subtle"
      fontSize="xs"
      role="group"
      aria-label={t("jinja.preview.label")}
    >
      <HStack gap={2} justify="space-between" mb={0.5}>
        <HStack gap={1} color="fg.muted">
          <LuEye aria-hidden />
          <Text>{t("jinja.preview.label")}</Text>
        </HStack>
        <HStack gap={1}>
          <Text color="fg.muted" fontSize="2xs">
            {t("jinja.preview.logicalDate")}
          </Text>
          <Input
            type="date"
            lang={i18n.language}
            size="2xs"
            w="auto"
            value={date}
            aria-label={t("jinja.preview.logicalDateAria")}
            onChange={(e) => setDate(e.target.value)}
          />
        </HStack>
      </HStack>
      <Text fontFamily="mono" whiteSpace="pre-wrap" wordBreak="break-all" maxH="8rem" overflowY="auto">
        {result.segments.map((s, i) =>
          s.kind === "text" || s.kind === "value" ? (
            <span key={i}>{s.text}</span>
          ) : (
            <Text
              as="span"
              key={i}
              color={s.kind === "error" ? "red.500" : "fg.muted"}
              fontStyle="italic"
              title={s.kind === "error" ? t("jinja.preview.errorTitle") : t("jinja.preview.unknownTitle")}
            >
              {s.text}
            </Text>
          ),
        )}
      </Text>
      {result.errors.length ? (
        <Text color="red.500" fontSize="2xs" mt={0.5}>
          {result.errors.join(" · ")}
        </Text>
      ) : null}
    </Box>
  );
}

// ----------------------------------------------------------------------------- field

const RENDERER_LANGUAGE: Record<string, CodeLanguage> = {
  bash: "bash",
  sh: "bash",
  shell: "bash",
  sql: "sql",
  python: "python",
  py: "python",
  yaml: "yaml",
  json: "json",
  markdown: "markdown",
  md: "markdown",
};

const ASSET_SCHEDULES = new Set(["assets", "partitioned_assets"]);

type TemplateFieldProps = {
  label: string;
  help?: ReactNode | undefined;
  error?: string | undefined;
  required?: boolean | undefined;
  /** Spec field path (problems can focus it). */
  field?: string | undefined;
  disabled?: boolean | undefined;
  value: string | null | undefined;
  onChange: (v: string | null) => void;
  /** x-autoair-renderer (bash | sql | python | yaml | json | markdown); plain Jinja otherwise. */
  renderer?: string | null | undefined;
  placeholder?: string | undefined;
  /** Id used to register as the Inputs panel insert target (defaults to field ?? label). */
  targetId?: string | undefined;
  /** Rendered next to the label (e.g. a literal / XCom / template mode switch). */
  headerExtra?: ReactNode | undefined;
  /** Visually hide the label (it remains the editor's accessible name). */
  hideLabel?: boolean | undefined;
  /** Single-line feel (no gutters). Defaults to true for plain Jinja. */
  compact?: boolean | undefined;
};

/**
 * Editor for Airflow templated fields: CodeMirror with Jinja highlighting, Airflow-context
 * completion, an "Insert" helper menu, and a live preview against a sample run.
 */
export function TemplateField({
  label,
  help,
  error,
  required,
  field,
  disabled,
  value,
  onChange,
  renderer,
  placeholder,
  targetId,
  headerExtra,
  hideLabel,
  compact,
}: TemplateFieldProps) {
  const { t } = useTranslation("inspector");
  const data = useJinjaCompletionData();
  const scheduleType = useBuilder((s) => s.dag.schedule?.type);
  const setTarget = useInsertTarget((s) => s.setTarget);
  const clearTarget = useInsertTarget((s) => s.clearTarget);
  const handleRef = useRef<CodeEditorHandle | null>(null);
  const id = targetId ?? field ?? label;
  const text = value ?? "";
  const language: CodeLanguage = (renderer && RENDERER_LANGUAGE[renderer.toLowerCase()]) || "jinja";
  const isCompact = compact ?? language === "jinja";

  // Latest value for the append fallback (no editor focused yet).
  const textRef = useRef(text);
  useEffect(() => {
    textRef.current = text;
  }, [text]);

  useEffect(() => () => clearTarget(id), [clearTarget, id]);

  const insert = (snippet: string) => {
    if (handleRef.current) handleRef.current.insert(snippet);
    else onChange(textRef.current + snippet);
  };

  const timeVars = useMemo(() => templateTimeVariables(text), [text]);
  const showTimeWarning = timeVars.length > 0 && !!scheduleType && ASSET_SCHEDULES.has(scheduleType);
  const showPreview = isTemplateString(text);

  return (
    <Field.Root
      invalid={!!error}
      required={required ?? false}
      disabled={disabled ?? false}
      {...(field ? { [FIELD_ATTR]: field } : {})}
    >
      <HStack w="100%" justify="space-between" gap={1} minH={hideLabel ? undefined : 6}>
        {hideLabel ? <span /> : <Label label={label} required={required} dtype="str" />}
        <HStack gap={0}>
          {hideLabel ? null : headerExtra}
          <InsertMenu data={data} onInsert={insert} disabled={disabled} />
        </HStack>
      </HStack>
      <CodeEditor
        value={text}
        onChange={(v) => onChange(v === "" ? null : v)}
        language={language}
        jinja={data}
        ariaLabel={label}
        readOnly={disabled}
        placeholder={placeholder}
        invalid={!!error}
        compact={isCompact}
        minHeight={isCompact ? undefined : "5rem"}
        expandTitle={label}
        expandToolbar={<InsertMenu data={data} onInsert={insert} disabled={disabled} />}
        expandFooter={showPreview ? <TemplatePreview template={text} /> : undefined}
        onFocus={(handle) => {
          handleRef.current = handle;
          setTarget({ id, label, insertTemplate: handle.insert });
        }}
      />
      {showPreview ? <TemplatePreview template={text} /> : null}
      {showTimeWarning ? (
        <Hint tone="warning">{t("jinja.assetScheduleWarning", { vars: timeVars.join(", ") })}</Hint>
      ) : null}
      <Help error={error} help={help} field={field} />
    </Field.Root>
  );
}
