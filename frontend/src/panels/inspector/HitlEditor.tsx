import { Box, Button, Checkbox, HStack, IconButton, Input, NativeSelect, Stack, Table, Text } from "@chakra-ui/react";
import { useTranslation } from "react-i18next";
import { LuPlus, LuTrash2, LuWandSparkles } from "react-icons/lu";
import { useShallow } from "zustand/react/shallow";

import { type JsonSchema, withoutKey } from "../../lib/formSchema";
import { effectiveTaskId, downstreamTasks } from "../../lib/xcom";
import { useBuilder } from "../../store/builder";
import { FIELD_ATTR, Hint, NumberField, SectionTitle, StringListField, SwitchField } from "../../widgets/fields";
import { TemplateField } from "../../widgets/TemplateField";
import { DagParamsEditor } from "../dag/ParamsEditor";

/** Params of HITL operators handled by this editor (others fall back to the generic form). */
export const HITL_PARAMS = [
  "subject",
  "body",
  "options",
  "defaults",
  "multiple",
  "params",
  "assigned_users",
  "response_timeout",
  "options_mapping",
] as const;

type HitlUser = { id: string; name: string };

/** Seconds, stored as a number or as a `{$type: "timedelta"}` value (codegen emits both as a timedelta). */
const timeoutSeconds = (v: unknown): number | null => {
  if (typeof v === "number") return v;
  if (v && typeof v === "object" && (v as { $type?: unknown }).$type === "timedelta") {
    const s = (v as { seconds?: unknown }).seconds;
    return typeof s === "number" ? s : null;
  }
  return null;
};

const asStringList = (v: unknown): string[] =>
  Array.isArray(v) ? v.map(String) : typeof v === "string" && v ? [v] : [];

function UsersEditor({ value, onChange }: { value: HitlUser[]; onChange: (v: HitlUser[]) => void }) {
  const { t } = useTranslation("inspector");
  return (
    <Stack gap={1} {...{ [FIELD_ATTR]: "assigned_users" }}>
      <Text fontSize="xs" fontWeight="medium">
        {t("hitl.assignedUsers")}
      </Text>
      <Hint>{t("hitl.assignedUsersHelp")}</Hint>
      {value.map((u, i) => (
        <HStack key={i} gap={1}>
          <Input
            size="xs"
            fontFamily="mono"
            placeholder={t("hitl.userId")}
            aria-label={`${t("hitl.userId")} ${i + 1}`}
            value={u.id}
            onChange={(e) => onChange(value.map((x, j) => (j === i ? { ...x, id: e.target.value } : x)))}
          />
          <Input
            size="xs"
            placeholder={t("hitl.userName")}
            aria-label={`${t("hitl.userName")} ${i + 1}`}
            value={u.name}
            onChange={(e) => onChange(value.map((x, j) => (j === i ? { ...x, name: e.target.value } : x)))}
          />
          <IconButton
            aria-label={t("common.removeItem", { item: u.name || u.id || i + 1 })}
            size="2xs"
            variant="ghost"
            onClick={() => onChange(value.filter((_, j) => j !== i))}
          >
            <LuTrash2 />
          </IconButton>
        </HStack>
      ))}
      <Button size="xs" variant="outline" alignSelf="start" onClick={() => onChange([...value, { id: "", name: "" }])}>
        <LuPlus /> {t("hitl.addUser")}
      </Button>
    </Stack>
  );
}

/** HITLBranchOperator: which downstream task(s) each option leads to. */
function OptionsMappingEditor({
  nodeId,
  options,
  value,
  onChange,
  onAutoFill,
}: {
  nodeId: string;
  options: string[];
  value: Record<string, string>;
  onChange: (v: Record<string, string>) => void;
  /** Sets options and mapping together (auto-fill). */
  onAutoFill: (opts: string[], mapping: Record<string, string>) => void;
}) {
  const { t } = useTranslation("inspector");
  const { nodes, edges } = useBuilder(useShallow((s) => ({ nodes: s.nodes, edges: s.edges })));
  const targets = downstreamTasks(nodes, edges, nodeId).map((n) => effectiveTaskId(nodes, n.id));
  const autoFill = () => {
    // One option per downstream task when none are defined yet; otherwise map by position/name.
    const opts = options.length ? options : targets;
    const next: Record<string, string> = {};
    opts.forEach((o, i) => {
      const byName = targets.find((tid) => tid === o || tid.endsWith(`.${o}`));
      const pick = value[o] ?? byName ?? targets[i];
      if (pick) next[o] = pick;
    });
    onAutoFill(opts, next);
  };
  return (
    <Box {...{ [FIELD_ATTR]: "options_mapping" }}>
      <HStack justify="space-between">
        <Text fontSize="xs" fontWeight="medium">
          {t("hitl.optionsMapping")}
        </Text>
        <Button size="2xs" variant="outline" onClick={autoFill} disabled={!targets.length}>
          <LuWandSparkles /> {t("hitl.autoFill")}
        </Button>
      </HStack>
      <Hint>{targets.length ? t("hitl.optionsMappingHelp") : t("hitl.noDownstream")}</Hint>
      {options.length ? (
        <Table.Root size="sm" mt={1}>
          <Table.Header>
            <Table.Row>
              <Table.ColumnHeader fontSize="xs">{t("hitl.option")}</Table.ColumnHeader>
              <Table.ColumnHeader fontSize="xs">{t("hitl.downstreamTask")}</Table.ColumnHeader>
            </Table.Row>
          </Table.Header>
          <Table.Body>
            {options.map((o) => (
              <Table.Row key={o}>
                <Table.Cell fontSize="xs">{o}</Table.Cell>
                <Table.Cell>
                  <NativeSelect.Root size="xs">
                    <NativeSelect.Field
                      aria-label={t("hitl.taskFor", { option: o })}
                      value={value[o] ?? ""}
                      onChange={(e) => {
                        onChange(e.target.value ? { ...value, [o]: e.target.value } : withoutKey(value, o));
                      }}
                    >
                      <option value="">{t("common.none")}</option>
                      {targets.map((tid) => (
                        <option key={tid} value={tid}>
                          {tid}
                        </option>
                      ))}
                      {value[o] && !targets.includes(value[o]) ? <option value={value[o]}>{value[o]}</option> : null}
                    </NativeSelect.Field>
                    <NativeSelect.Indicator />
                  </NativeSelect.Root>
                </Table.Cell>
              </Table.Row>
            ))}
          </Table.Body>
        </Table.Root>
      ) : null}
    </Box>
  );
}

/**
 * Dedicated editor for Human-in-the-loop operators (Airflow ≥ 3.1): the question shown
 * to the reviewer, its options, the input form and who may answer.
 */
export function HitlEditor({
  nodeId,
  schema,
  value,
  onChange,
  errors,
}: {
  nodeId: string;
  schema: JsonSchema;
  value: Record<string, unknown>;
  onChange: (v: Record<string, unknown>) => void;
  errors?: Record<string, string>;
}) {
  const { t } = useTranslation("inspector");
  const has = (name: string) => !!schema.properties?.[name];
  const required = new Set(schema.required ?? []);
  const set = (name: string, v: unknown) => {
    const empty = v === undefined || v === null || v === "" || (Array.isArray(v) && v.length === 0);
    onChange(empty ? withoutKey(value, name) : { ...value, [name]: v });
  };
  const options = asStringList(value.options);
  const defaults = asStringList(value.defaults);
  const multiple = value.multiple === true;
  const users: HitlUser[] = Array.isArray(value.assigned_users)
    ? value.assigned_users.map((u) => {
        const o = (u ?? {}) as Partial<HitlUser>;
        return { id: String(o.id ?? ""), name: String(o.name ?? "") };
      })
    : [];
  const strVal = (name: string) => (typeof value[name] === "string" ? (value[name] as string) : "");

  return (
    <Stack gap={3}>
      <SectionTitle>{t("hitl.title")}</SectionTitle>
      <Hint>{t("hitl.help")}</Hint>
      {has("subject") ? (
        <TemplateField
          help={t("hitl.subjectHelp")}
          label={t("hitl.subject")}
          field="subject"
          compact
          required={required.has("subject")}
          error={errors?.subject}
          value={strVal("subject")}
          onChange={(v) => set("subject", v)}
        />
      ) : null}
      {has("body") ? (
        <TemplateField
          label={t("hitl.body")}
          help={t("hitl.bodyHelp")}
          field="body"
          renderer="markdown"
          error={errors?.body}
          value={strVal("body")}
          onChange={(v) => set("body", v)}
        />
      ) : null}
      {has("options") ? (
        <StringListField
          label={t("hitl.options")}
          help={t("hitl.optionsHelp")}
          field="options"
          required={required.has("options")}
          error={errors?.options}
          value={options}
          onChange={(v) => {
            // Drop defaults that are no longer valid options (single update).
            let next = v.length ? { ...value, options: v } : withoutKey(value, "options");
            const kept = defaults.filter((d) => v.includes(d));
            if (kept.length !== defaults.length) {
              next = kept.length ? { ...next, defaults: multiple ? kept : kept[0] } : withoutKey(next, "defaults");
            }
            onChange(next);
          }}
        />
      ) : null}
      {has("multiple") ? (
        <SwitchField
          label={t("hitl.multiple")}
          help={t("hitl.multipleHelp")}
          field="multiple"
          checked={multiple}
          onChange={(v) => set("multiple", v || undefined)}
        />
      ) : null}
      {has("defaults") && options.length ? (
        <Box {...{ [FIELD_ATTR]: "defaults" }}>
          <Text fontSize="xs" fontWeight="medium">
            {t("hitl.defaults")}
          </Text>
          <Hint>{t("hitl.defaultsHelp")}</Hint>
          <HStack wrap="wrap" gap={3} mt={1}>
            {options.map((o) => (
              <Checkbox.Root
                key={o}
                size="sm"
                checked={defaults.includes(o)}
                onCheckedChange={(e) => {
                  const on = e.checked === true;
                  const next = on ? (multiple ? [...defaults, o] : [o]) : defaults.filter((d) => d !== o);
                  set("defaults", multiple ? next : next[0]);
                }}
              >
                <Checkbox.HiddenInput />
                <Checkbox.Control />
                <Checkbox.Label fontSize="xs">{o}</Checkbox.Label>
              </Checkbox.Root>
            ))}
          </HStack>
        </Box>
      ) : null}
      {has("options_mapping") ? (
        <OptionsMappingEditor
          nodeId={nodeId}
          options={options}
          value={(value.options_mapping as Record<string, string> | undefined) ?? {}}
          onChange={(m) => set("options_mapping", Object.keys(m).length ? m : undefined)}
          onAutoFill={(o, m) =>
            onChange({ ...value, options: o, ...(Object.keys(m).length ? { options_mapping: m } : {}) })
          }
        />
      ) : null}
      {has("assigned_users") ? <UsersEditor value={users} onChange={(u) => set("assigned_users", u)} /> : null}
      {has("response_timeout") ? (
        <NumberField
          dtype="timedelta (s)"
          help={t("hitl.responseTimeoutHelp")}
          label={t("hitl.responseTimeout")}
          field="response_timeout"
          min={1}
          value={timeoutSeconds(value.response_timeout)}
          onChange={(v) => set("response_timeout", v)}
        />
      ) : (
        <Hint>{t("hitl.timeoutHint")}</Hint>
      )}
      {has("params") ? (
        <Box {...{ [FIELD_ATTR]: "params" }}>
          <DagParamsEditor
            title={t("hitl.formParams")}
            value={(value.params as Record<string, unknown> | undefined) ?? {}}
            onChange={(p) => set("params", Object.keys(p).length ? p : undefined)}
            showPreview
          />
        </Box>
      ) : null}
    </Stack>
  );
}
