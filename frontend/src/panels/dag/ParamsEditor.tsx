import { Badge, Box, Button, Checkbox, Field, HStack, IconButton, Input, Stack, Text, Wrap } from "@chakra-ui/react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { LuArrowDown, LuArrowUp, LuChevronDown, LuChevronRight, LuPlus, LuTrash2 } from "react-icons/lu";

import type { DagParam } from "../../api/gen/types.gen";
import {
  PARAM_FORMATS,
  PARAM_NAME,
  PARAM_TYPES,
  type ParamType,
  compactParam,
  matchesTypes,
  moveKey,
  normalizeParams,
  paramTypes,
  paramsToTriggerForm,
  primaryType,
  renameKey,
} from "../../lib/dagParams";
import { Autocomplete } from "../../widgets/Autocomplete";
import { CodeEditor } from "../../widgets/code";
import {
  CodeField,
  FIELD_ATTR,
  Hint,
  JsonField,
  KeyValueField,
  NumberField,
  Panel,
  SectionTitle,
  SelectField,
  StringListField,
  TextField,
} from "../../widgets/fields";
import { SchemaForm } from "../../widgets/SchemaForm";

/** Input for a value of a given JSON type (undefined = "not set"). */
export function TypedValueInput({
  label,
  type,
  value,
  onChange,
  help,
  field,
  format,
}: {
  label: string;
  type: ParamType | null;
  value: unknown;
  onChange: (v: unknown) => void;
  help?: string;
  field?: string;
  format?: string | null | undefined;
}) {
  const { t } = useTranslation("inspector");
  switch (type) {
    case "string":
      return format === "multiline" ? (
        <CodeField
          label={label}
          help={help}
          field={field}
          rows={3}
          value={typeof value === "string" ? value : ""}
          onChange={(v) => onChange(v)}
        />
      ) : (
        <TextField
          label={label}
          help={help}
          field={field}
          placeholder={format ?? undefined}
          value={typeof value === "string" ? value : value === undefined ? "" : JSON.stringify(value)}
          onChange={(v) => onChange(v ?? undefined)}
        />
      );
    case "integer":
    case "number":
      return (
        <NumberField
          label={label}
          help={help}
          field={field}
          step={type === "integer" ? 1 : undefined}
          value={typeof value === "number" ? value : null}
          onChange={(v) => onChange(v ?? undefined)}
        />
      );
    case "boolean":
      return (
        <SelectField
          label={label}
          help={help}
          field={field}
          emptyLabel={t("dag.params.notSet")}
          value={value === true ? "true" : value === false ? "false" : null}
          options={["true", "false"]}
          onChange={(v) => onChange(v === null ? undefined : v === "true")}
        />
      );
    case "null":
      return (
        <Field.Root {...(field ? { [FIELD_ATTR]: field } : {})}>
          <Field.Label fontSize="xs">{label}</Field.Label>
          <Text fontFamily="mono" fontSize="xs" color="fg.muted">
            null
          </Text>
        </Field.Root>
      );
    default:
      return (
        <JsonField
          label={label}
          help={help ?? t("dag.params.jsonHelp")}
          field={field}
          rows={3}
          value={value}
          onChange={onChange}
          validate={(v) =>
            type === "array" && !Array.isArray(v)
              ? t("dag.params.mustBe", { type: "array" })
              : type === "object" && (typeof v !== "object" || v === null || Array.isArray(v))
                ? t("dag.params.mustBe", { type: "object" })
                : undefined
          }
        />
      );
  }
}

function TypesPicker({ value, onChange }: { value: ParamType[]; onChange: (v: ParamType[]) => void }) {
  const { t } = useTranslation("inspector");
  return (
    <Field.Root>
      <Field.Label fontSize="xs">{t("dag.params.types")}</Field.Label>
      <Wrap gap={2} role="group" aria-label={t("dag.params.types")}>
        {PARAM_TYPES.map((type) => (
          <Checkbox.Root
            key={type}
            size="sm"
            checked={value.includes(type)}
            onCheckedChange={(e) =>
              onChange(
                e.checked === true
                  ? PARAM_TYPES.filter((x) => x === type || value.includes(x))
                  : value.filter((x) => x !== type),
              )
            }
          >
            <Checkbox.HiddenInput />
            <Checkbox.Control />
            <Checkbox.Label fontSize="xs" fontFamily="mono">
              {type}
            </Checkbox.Label>
          </Checkbox.Root>
        ))}
      </Wrap>
      <Field.HelperText fontSize="xs">{t("dag.params.typesHelp")}</Field.HelperText>
    </Field.Root>
  );
}

function ParamDetail({
  name,
  param,
  onChange,
  onRename,
  existing,
  sections,
  field,
}: {
  name: string;
  param: DagParam;
  onChange: (p: DagParam) => void;
  onRename: (to: string) => void;
  existing: string[];
  sections: string[];
  field?: string | undefined;
}) {
  const { t } = useTranslation("inspector");
  const [draftName, setDraftName] = useState(name);
  const nameError =
    draftName === name
      ? undefined
      : !PARAM_NAME.test(draftName)
        ? t("dag.params.invalidName")
        : existing.includes(draftName)
          ? t("dag.params.duplicateName")
          : undefined;
  const commitName = () => {
    if (draftName !== name && !nameError) onRename(draftName);
  };
  const set = (patch: Partial<DagParam>) => onChange(compactParam({ ...param, ...patch }));
  const types = paramTypes(param);
  const declared = param.type != null;
  const primary = primaryType(param);
  const isText = primary === "string";
  const isNumeric = primary === "number" || primary === "integer";
  const defaultMismatch = param.default !== undefined && !matchesTypes(param.default, types);

  return (
    <Stack gap={2} pt={2}>
      <Field.Root invalid={!!nameError}>
        <Field.Label fontSize="xs">{t("dag.params.name")}</Field.Label>
        <Input
          size="sm"
          fontFamily="mono"
          value={draftName}
          onChange={(e) => setDraftName(e.target.value)}
          onBlur={commitName}
          onKeyDown={(e) => e.key === "Enter" && commitName()}
        />
        {nameError ? (
          <Field.ErrorText>{nameError}</Field.ErrorText>
        ) : (
          <Field.HelperText fontSize="xs">{t("dag.params.nameHelp", { name })}</Field.HelperText>
        )}
      </Field.Root>
      <TypesPicker
        value={declared ? types : []}
        onChange={(next) => set({ type: next.length === 0 ? null : next.length === 1 ? next[0] : next })}
      />
      {!declared && types.length ? <Hint>{t("dag.params.inferred", { type: types.join(", ") })}</Hint> : null}
      <TypedValueInput
        label={t("dag.params.default")}
        type={primary}
        format={param.format}
        field={field ? `${field}.default` : undefined}
        value={param.default}
        onChange={(v) => {
          const next = { ...param };
          if (v === undefined) delete next.default;
          else next.default = v;
          onChange(compactParam(next));
        }}
      />
      {defaultMismatch ? <Hint tone="warning">{t("dag.params.defaultMismatch")}</Hint> : null}
      {param.default === undefined && types.length && !types.includes("null") ? (
        <Hint>{t("dag.params.noDefaultRequired")}</Hint>
      ) : null}
      <HStack align="start">
        <TextField
          help={t("dag.params.titleHelp")}
          label={t("dag.params.title")}
          value={param.title}
          onChange={(v) => set({ title: v })}
        />
        <Autocomplete
          help={t("dag.params.sectionHelp")}
          label={t("dag.params.section")}
          value={param.section}
          options={sections.map((s) => ({ value: s }))}
          onChange={(v) => set({ section: v })}
        />
      </HStack>
      <TextField
        help={t("dag.params.descriptionHelp")}
        label={t("dag.params.description")}
        value={param.description}
        onChange={(v) => set({ description: v })}
      />
      <Field.Root>
        <Field.Label fontSize="xs">{t("dag.params.descriptionMd")}</Field.Label>
        <Box w="100%">
          <CodeEditor
            language="markdown"
            ariaLabel={t("dag.params.descriptionMd")}
            compact
            minHeight="2.5rem"
            maxHeight="10rem"
            value={param.description_md ?? ""}
            onChange={(v) => set({ description_md: v || null })}
          />
        </Box>
        <Field.HelperText fontSize="xs">{t("dag.params.descriptionMdHelp")}</Field.HelperText>
      </Field.Root>
      {isText || primary === null ? (
        <SelectField
          help={t("dag.params.formatHelp")}
          label={t("dag.params.format")}
          emptyLabel={t("dag.params.noFormat")}
          value={param.format}
          options={PARAM_FORMATS.map((f) => ({ value: f, label: t(`dag.params.formats.${f}`, { defaultValue: f }) }))}
          onChange={(v) => set({ format: v })}
        />
      ) : null}
      {isText ? (
        <StringListField
          label={t("dag.params.enum")}
          help={t("dag.params.enumHelp")}
          value={(param.enum ?? []).map(String)}
          onChange={(v) => set({ enum: v })}
        />
      ) : (
        <JsonField
          label={t("dag.params.enum")}
          help={t("dag.params.enumJsonHelp")}
          rows={2}
          value={param.enum ?? undefined}
          validate={(v) => (Array.isArray(v) ? undefined : t("dag.params.mustBe", { type: "array" }))}
          onChange={(v) => set({ enum: (v as unknown[] | undefined) ?? null })}
        />
      )}
      {param.enum?.length ? (
        <KeyValueField
          label={t("dag.params.valuesDisplay")}
          help={t("dag.params.valuesDisplayHelp")}
          keyPlaceholder={t("dag.params.enumValue")}
          valuePlaceholder={t("dag.params.displayLabel")}
          value={param.values_display ?? {}}
          onChange={(v) => set({ values_display: v })}
        />
      ) : null}
      {isNumeric ? (
        <HStack align="start">
          <NumberField
            help={t("dag.params.minimumHelp")}
            label={t("dag.params.minimum")}
            value={param.minimum}
            onChange={(v) => set({ minimum: v })}
          />
          <NumberField
            help={t("dag.params.maximumHelp")}
            label={t("dag.params.maximum")}
            value={param.maximum}
            onChange={(v) => set({ maximum: v })}
          />
        </HStack>
      ) : null}
      {isText ? (
        <>
          <HStack align="start">
            <NumberField
              help={t("dag.params.minLengthHelp")}
              label={t("dag.params.minLength")}
              min={0}
              value={param.min_length}
              onChange={(v) => set({ min_length: v })}
            />
            <NumberField
              help={t("dag.params.maxLengthHelp")}
              label={t("dag.params.maxLength")}
              min={0}
              value={param.max_length}
              onChange={(v) => set({ max_length: v })}
            />
          </HStack>
          <TextField
            help={t("dag.params.patternHelp")}
            label={t("dag.params.pattern")}
            mono
            placeholder="^[a-z]+$"
            value={param.pattern}
            error={patternError(param.pattern, t("dag.params.invalidPattern"))}
            onChange={(v) => set({ pattern: v })}
          />
        </>
      ) : null}
      {primary === "array" ? (
        <JsonField
          label={t("dag.params.items")}
          help={t("dag.params.itemsHelp")}
          rows={2}
          value={param.items ?? undefined}
          onChange={(v) => set({ items: (v as Record<string, unknown> | undefined) ?? null })}
        />
      ) : null}
      <JsonField
        label={t("dag.params.examples")}
        help={t("dag.params.examplesHelp")}
        rows={2}
        value={param.examples ?? undefined}
        validate={(v) => (Array.isArray(v) ? undefined : t("dag.params.mustBe", { type: "array" }))}
        onChange={(v) => set({ examples: (v as unknown[] | undefined) ?? null })}
      />
      <JsonField
        label={t("dag.params.const")}
        help={t("dag.params.constHelp")}
        rows={1}
        value={param.const}
        onChange={(v) => {
          const next = { ...param };
          if (v === undefined) delete next.const;
          else next.const = v;
          onChange(compactParam(next));
        }}
      />
    </Stack>
  );
}

function patternError(pattern: string | null | undefined, message: string): string | undefined {
  if (!pattern) return undefined;
  try {
    new RegExp(pattern);
    return undefined;
  } catch {
    return message;
  }
}

const summarize = (v: unknown): string => {
  if (v === undefined) return "";
  const s = typeof v === "string" ? JSON.stringify(v) : JSON.stringify(v);
  return s.length > 28 ? `${s.slice(0, 27)}…` : s;
};

/**
 * Editor for a map of `airflow.sdk.Param`s (DAG params, HITL params). Bare values in
 * `value` are normalised to `{default: value}` on first edit.
 */
export function DagParamsEditor({
  value,
  onChange,
  title,
  showPreview,
  field,
}: {
  value: Record<string, unknown>;
  onChange: (v: Record<string, DagParam>) => void;
  title?: string;
  showPreview?: boolean;
  field?: string;
}) {
  const { t } = useTranslation("inspector");
  const params = useMemo(() => normalizeParams(value), [value]);
  const [open, setOpen] = useState<string | null>(null);
  const [newName, setNewName] = useState("");
  const names = Object.keys(params);
  const sections = [...new Set(Object.values(params).flatMap((p) => (p.section ? [p.section] : [])))];
  const newError =
    newName && !PARAM_NAME.test(newName)
      ? t("dag.params.invalidName")
      : names.includes(newName)
        ? t("dag.params.duplicateName")
        : undefined;
  const add = () => {
    if (!newName || newError) return;
    onChange({ ...params, [newName]: { type: "string", default: "" } });
    setOpen(newName);
    setNewName("");
  };

  return (
    <Stack gap={2} {...(field ? { [FIELD_ATTR]: field } : {})}>
      {title ? <SectionTitle>{title}</SectionTitle> : null}
      <Hint>{t("dag.params.help")}</Hint>
      {names.length === 0 ? <Hint>{t("dag.params.empty")}</Hint> : null}
      <Stack gap={1}>
        {names.map((name, i) => {
          const p = params[name] as DagParam;
          const expanded = open === name;
          return (
            <Box key={name} borderWidth="1px" rounded="md" px={2} py={1}>
              <HStack gap={1}>
                <IconButton
                  aria-label={expanded ? t("dag.params.collapse", { name }) : t("dag.params.expand", { name })}
                  aria-expanded={expanded}
                  size="2xs"
                  variant="ghost"
                  onClick={() => setOpen(expanded ? null : name)}
                >
                  {expanded ? <LuChevronDown /> : <LuChevronRight />}
                </IconButton>
                <Text
                  fontFamily="mono"
                  fontSize="xs"
                  fontWeight="semibold"
                  truncate
                  cursor="pointer"
                  onClick={() => setOpen(expanded ? null : name)}
                >
                  {name}
                </Text>
                {paramTypes(p).map((ty) => (
                  <Badge key={ty} size="xs" variant="subtle">
                    {ty}
                  </Badge>
                ))}
                {p.section ? (
                  <Badge size="xs" variant="outline">
                    {p.section}
                  </Badge>
                ) : null}
                <Text
                  fontSize="2xs"
                  color="fg.muted"
                  fontFamily="mono"
                  truncate
                  flex="1"
                  title={JSON.stringify(p.default)}
                >
                  {summarize(p.default)}
                </Text>
                <IconButton
                  aria-label={t("common.moveUp")}
                  size="2xs"
                  variant="ghost"
                  disabled={i === 0}
                  onClick={() => onChange(moveKey(params, name, -1))}
                >
                  <LuArrowUp />
                </IconButton>
                <IconButton
                  aria-label={t("common.moveDown")}
                  size="2xs"
                  variant="ghost"
                  disabled={i === names.length - 1}
                  onClick={() => onChange(moveKey(params, name, 1))}
                >
                  <LuArrowDown />
                </IconButton>
                <IconButton
                  aria-label={t("common.removeItem", { item: name })}
                  size="2xs"
                  variant="ghost"
                  colorPalette="red"
                  onClick={() => {
                    const { [name]: _removed, ...rest } = params;
                    onChange(rest);
                  }}
                >
                  <LuTrash2 />
                </IconButton>
              </HStack>
              {expanded ? (
                <ParamDetail
                  name={name}
                  param={p}
                  existing={names}
                  sections={sections}
                  field={field ? `${field}.${name}` : undefined}
                  onChange={(np) => onChange({ ...params, [name]: np })}
                  onRename={(to) => {
                    onChange(renameKey(params, name, to));
                    setOpen(to);
                  }}
                />
              ) : null}
            </Box>
          );
        })}
      </Stack>
      <Field.Root invalid={!!newError}>
        <Field.Label srOnly>{t("dag.params.newName")}</Field.Label>
        <HStack gap={1} w="100%">
          <Input
            size="xs"
            fontFamily="mono"
            placeholder={t("dag.params.newName")}
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && add()}
          />
          <Button size="xs" variant="outline" onClick={add} disabled={!newName || !!newError}>
            <LuPlus /> {t("dag.params.add")}
          </Button>
        </HStack>
        {newError ? <Field.ErrorText>{newError}</Field.ErrorText> : null}
      </Field.Root>
      {showPreview && names.length ? <TriggerFormPreview params={params} /> : null}
    </Stack>
  );
}

/**
 * Live preview of Airflow's "Trigger DAG" form for the given params: one form per
 * section, defaults pre-filled, JSON-Schema validation as the user types.
 */
function TriggerFormPreview({ params }: { params: Record<string, unknown> }) {
  const { t } = useTranslation("inspector");
  const form = useMemo(() => paramsToTriggerForm(params), [params]);
  const initial = useMemo(
    () => Object.assign({}, ...form.sections.map((s) => s.formData)) as Record<string, unknown>,
    [form],
  );
  const [data, setData] = useState(initial);
  // Reset the preview's values whenever the param definitions change.
  const [syncedFrom, setSyncedFrom] = useState(initial);
  if (syncedFrom !== initial) {
    setSyncedFrom(initial);
    setData(initial);
  }
  return (
    <Panel
      title={t("dag.params.preview.title")}
      actions={
        <Button size="2xs" variant="ghost" onClick={() => setData(initial)}>
          {t("dag.params.preview.reset")}
        </Button>
      }
    >
      <Hint>{t("dag.params.preview.help")}</Hint>
      {form.sections.map((sec) => (
        <Box key={sec.name ?? ""}>
          <Text fontSize="sm" fontWeight="semibold" mb={1}>
            {sec.name ?? t("dag.params.preview.defaultSection")}
          </Text>
          <SchemaForm
            raw
            validate
            schema={sec.schema}
            uiSchema={sec.uiSchema}
            value={Object.fromEntries(sec.names.flatMap((n) => (n in data ? [[n, data[n]]] : [])))}
            onChange={(v) => setData((d) => ({ ...d, ...v }))}
          />
        </Box>
      ))}
      <Box>
        <Text fontSize="xs" color="fg.muted" mb={1}>
          {t("dag.params.preview.conf")}
        </Text>
        <Box as="pre" fontSize="2xs" fontFamily="mono" bg="bg.muted" p={2} rounded="sm" overflowX="auto">
          {JSON.stringify(data, null, 2)}
        </Box>
      </Box>
    </Panel>
  );
}
