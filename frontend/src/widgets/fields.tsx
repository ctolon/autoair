import { Box, Button, Field, HStack, IconButton, Input, NativeSelect, Stack, Switch, Text } from "@chakra-ui/react";
import { type ReactNode, useState } from "react";
import { useTranslation } from "react-i18next";
import { LuArrowDown, LuArrowUp, LuPlus, LuTrash2 } from "react-icons/lu";

import { CodeEditor, type CodeLanguage } from "./code";

type Base = {
  label: string;
  help?: ReactNode;
  /** Python type shown next to the label (`int`, `str | None`…); each field has a default. */
  dtype?: string | undefined;
  error?: string | undefined;
  required?: boolean;
  /** Spec field path; problems referencing it can focus this input (see focusField). */
  field?: string | undefined;
  disabled?: boolean | undefined;
  /** Keep the label for screen readers only (caller renders its own header). */
  hideLabel?: boolean | undefined;
};

/** Attribute used to locate a form control from a validation problem. */
export const FIELD_ATTR = "data-autoair-field";
const anchor = (field: string | undefined) => (field ? { [FIELD_ATTR]: field } : {});

/** `default_args.retries` / `outlets.0.uri` → `retries` / `uri`: the key of the shared help text. */
function helpKey(field: string | undefined): string | undefined {
  if (!field) return undefined;
  const parts = field.split(".").filter((p) => !/^\d+$/.test(p));
  return parts[parts.length - 1];
}

/**
 * Help text for a field: the caller's, else the shared description of that spec field
 * (`inspector:fieldHelp.<name>`), so every Airflow attribute (queue, pool…) is explained.
 */
function useFieldHelp(help: ReactNode, field: string | undefined): ReactNode {
  const { t, i18n } = useTranslation("inspector");
  if (help) return help;
  const key = helpKey(field);
  return key && i18n.exists(`fieldHelp.${key}`, { ns: "inspector" }) ? t(`fieldHelp.${key}`) : undefined;
}

/** Type badge: `int`, `str`, `int | str`… */
export function TypeBadge({ dtype }: { dtype: string | undefined }) {
  if (!dtype) return null;
  return (
    <Text
      as="span"
      fontFamily="mono"
      fontSize="2xs"
      fontWeight="normal"
      color="fg.muted"
      bg="bg.muted"
      px="1"
      rounded="sm"
      ms="1.5"
      whiteSpace="nowrap"
      data-dtype={dtype}
    >
      {dtype}
    </Text>
  );
}

const numberType = (step: number | undefined) => (step !== undefined && !Number.isInteger(step) ? "float" : "int");

/** Field label: name, red `*` when required, type badge. */
export function Label({
  label,
  required,
  dtype,
  hideLabel,
  suffix,
}: {
  label: ReactNode;
  required?: boolean | undefined;
  dtype?: string | undefined;
  hideLabel?: boolean | undefined;
  suffix?: ReactNode;
}) {
  return (
    <Field.Label fontSize="xs" srOnly={hideLabel ?? false}>
      {label}
      {suffix}
      {required ? <Field.RequiredIndicator color="red.500" /> : null}
      <TypeBadge dtype={dtype} />
    </Field.Label>
  );
}

export function Help({
  error,
  help,
  field,
}: {
  error: string | undefined;
  help: ReactNode;
  field: string | undefined;
}) {
  const text = useFieldHelp(help, field);
  if (error) return <Field.ErrorText>{error}</Field.ErrorText>;
  return text ? <Field.HelperText fontSize="xs">{text}</Field.HelperText> : null;
}

export function TextField({
  label,
  help,
  error,
  required,
  value,
  onChange,
  placeholder,
  list,
  mono,
  field,
  disabled,
  hideLabel,
  dtype,
  type,
}: Base & {
  value: string | null | undefined;
  onChange: (v: string | null) => void;
  placeholder?: string | undefined;
  list?: string;
  mono?: boolean;
  /** `password`: masked input (secret params). */
  type?: "text" | "password";
}) {
  return (
    <Field.Root invalid={!!error} required={required ?? false} disabled={disabled ?? false} {...anchor(field)}>
      <Label label={label} required={required} dtype={dtype ?? "str"} hideLabel={hideLabel} />
      <Input
        size="sm"
        value={value ?? ""}
        placeholder={placeholder}
        list={list}
        type={type}
        autoComplete={type === "password" ? "off" : undefined}
        fontFamily={mono ? "mono" : undefined}
        onChange={(e) => onChange(e.target.value === "" ? null : e.target.value)}
      />
      <Help error={error} help={help} field={field} />
    </Field.Root>
  );
}

export function NumberField({
  label,
  help,
  error,
  value,
  onChange,
  min,
  step,
  max,
  placeholder,
  field,
  disabled,
  required,
  hideLabel,
  dtype,
}: Base & {
  value: number | null | undefined;
  onChange: (v: number | null) => void;
  min?: number;
  max?: number;
  step?: number;
  placeholder?: string | undefined;
}) {
  return (
    <Field.Root invalid={!!error} disabled={disabled ?? false} required={required ?? false} {...anchor(field)}>
      <Label label={label} required={required} dtype={dtype ?? numberType(step)} hideLabel={hideLabel} />
      <Input
        size="sm"
        type="number"
        min={min}
        max={max}
        step={step}
        placeholder={placeholder}
        value={value ?? ""}
        onChange={(e) => onChange(e.target.value === "" ? null : Number(e.target.value))}
      />
      <Help error={error} help={help} field={field} />
    </Field.Root>
  );
}

export function SelectField<T extends string>({
  label,
  help,
  value,
  options,
  onChange,
  emptyLabel,
  field,
  disabled,
  error,
  required,
  hideLabel,
  dtype,
}: Base & {
  value: T | null | undefined;
  options: readonly (T | { value: T; label: string; disabled?: boolean })[];
  onChange: (v: T | null) => void;
  emptyLabel?: string;
}) {
  return (
    <Field.Root invalid={!!error} disabled={disabled ?? false} required={required ?? false} {...anchor(field)}>
      <Label label={label} required={required} dtype={dtype ?? "str"} hideLabel={hideLabel} />
      <NativeSelect.Root size="sm">
        <NativeSelect.Field
          value={value ?? ""}
          onChange={(e) => onChange(e.target.value === "" ? null : (e.target.value as T))}
        >
          {emptyLabel !== undefined ? <option value="">{emptyLabel}</option> : null}
          {options.map((o) => {
            const opt = typeof o === "string" ? { value: o, label: o, disabled: false } : o;
            return (
              <option key={opt.value} value={opt.value} disabled={opt.disabled ?? false}>
                {opt.label}
              </option>
            );
          })}
        </NativeSelect.Field>
        <NativeSelect.Indicator />
      </NativeSelect.Root>
      <Help error={error} help={help} field={field} />
    </Field.Root>
  );
}

export function SwitchField({
  label,
  help,
  checked,
  onChange,
  field,
  disabled,
  error,
  dtype,
}: Base & { checked: boolean; onChange: (v: boolean) => void }) {
  return (
    <Field.Root invalid={!!error} disabled={disabled ?? false} {...anchor(field)}>
      <Switch.Root
        size="sm"
        checked={checked}
        disabled={disabled ?? false}
        onCheckedChange={(e) => onChange(e.checked)}
      >
        <Switch.HiddenInput />
        <Switch.Control />
        <Switch.Label fontSize="sm">
          {label}
          <TypeBadge dtype={dtype ?? "bool"} />
        </Switch.Label>
      </Switch.Root>
      <Help error={error} help={help} field={field} />
    </Field.Root>
  );
}

export function CodeField({
  label,
  help,
  error,
  value,
  onChange,
  rows = 8,
  placeholder,
  field,
  disabled,
  required,
  hideLabel,
  dtype,
  language = "text",
}: Base & {
  value: string;
  onChange: (v: string) => void;
  rows?: number;
  placeholder?: string | undefined;
  language?: CodeLanguage;
}) {
  return (
    <Field.Root invalid={!!error} disabled={disabled ?? false} required={required ?? false} {...anchor(field)}>
      <Label label={label} required={required} dtype={dtype ?? "str"} hideLabel={hideLabel} />
      {/* CodeMirror: syntax highlighting, Tab indents, resizable, opens in a large dialog. */}
      <CodeEditor
        language={language}
        ariaLabel={label}
        value={value}
        placeholder={placeholder}
        readOnly={disabled}
        invalid={!!error}
        minHeight={`${Math.max(2, rows) * 1.5 + 0.5}em`}
        maxHeight="28rem"
        onChange={onChange}
      />
      <Help error={error} help={help} field={field} />
    </Field.Root>
  );
}

/** Edits an arbitrary JSON value as text; commits only when the text parses. */
export function JsonField({
  label,
  help,
  value,
  onChange,
  rows = 4,
  validate,
  field,
  disabled,
  error: externalError,
  hideLabel,
  required,
  dtype,
  acceptsText,
}: Base & {
  value: unknown;
  onChange: (v: unknown) => void;
  rows?: number;
  validate?: (v: unknown) => string | undefined;
  /** The value may be a plain string: text is kept as-is unless it looks like JSON. */
  acceptsText?: boolean;
}) {
  const { t: tr } = useTranslation("inspector");
  const serialize = (v: unknown) =>
    v === undefined ? "" : acceptsText && typeof v === "string" ? v : JSON.stringify(v, null, 2);
  const serialized = serialize(value);
  const [text, setText] = useState(serialized);
  const [error, setError] = useState<string>();
  // What this field last committed: its echo is not an outside change.
  const [committed, setCommitted] = useState<string | null>(null);
  // Re-sync the draft when the value changes from outside (undo, node switch, …). The echo of
  // the user's own edit must not replace the text with its pretty-printed form: that reset
  // the editor while typing (the caret jumped and the next keys were lost).
  const [synced, setSynced] = useState(serialized);
  if (synced !== serialized) {
    setSynced(serialized);
    setCommitted(null);
    if (serialized !== committed) {
      setText(serialized);
      setError(undefined);
    }
  }
  const commit = (v: unknown) => {
    setCommitted(serialize(v));
    onChange(v);
  };
  return (
    <CodeField
      label={label}
      help={help}
      required={required}
      dtype={dtype ?? "json"}
      language="json"
      error={error ?? externalError}
      rows={rows}
      field={field}
      disabled={disabled}
      hideLabel={hideLabel}
      value={text}
      onChange={(t) => {
        setText(t);
        if (t.trim() === "") {
          setError(undefined);
          commit(undefined);
          return;
        }
        if (acceptsText && !/^\s*[[{"]/.test(t)) {
          setError(undefined);
          commit(t);
          return;
        }
        try {
          const parsed: unknown = JSON.parse(t);
          const problem = validate?.(parsed);
          setError(problem);
          if (!problem) commit(parsed);
        } catch {
          // Never show the parser's own wording ("Unexpected token…") to users.
          setError(tr("params.invalidJson"));
        }
      }}
    />
  );
}

export function SectionTitle({ children }: { children: ReactNode }) {
  return (
    <Text fontSize="xs" fontWeight="bold" textTransform="uppercase" color="fg.muted" letterSpacing="wide" mt={3}>
      {children}
    </Text>
  );
}

/**
 * Scrolls to and focuses the control registered for a spec field (see `field` props).
 * Matches the exact path first, then the closest parent path (e.g. "params.sql" → "params").
 */
export function focusField(field: string, root: ParentNode = document): boolean {
  const candidates = [field];
  const parts = field.split(".");
  while (parts.length > 1) {
    parts.pop();
    candidates.push(parts.join("."));
  }
  for (const c of candidates) {
    const el = root.querySelector<HTMLElement>(`[${FIELD_ATTR}="${CSS.escape(c)}"]`);
    if (!el) continue;
    el.scrollIntoView({ block: "center", behavior: "smooth" });
    // Prefer the editable control over header buttons (mode menu, Insert…) that come first.
    const target = el.matches("input,textarea,select,[contenteditable=true]")
      ? el
      : (el.querySelector<HTMLElement>("input:not([type=hidden]),textarea,select,[contenteditable=true]") ??
        el.querySelector<HTMLElement>("button"));
    target?.focus({ preventScroll: true });
    return true;
  }
  return false;
}

/** ISO (UTC) <-> <input type="datetime-local"> (shown and interpreted as UTC). */
const isoToLocalInput = (iso: string | null | undefined): string =>
  iso ? iso.replace(/(\.\d+)?(Z|[+-]00:00)$/, "").slice(0, 16) : "";
const localInputToIso = (v: string, timezone?: string | null): string | null =>
  v ? (timezone ? `${v}:00` : `${v}:00Z`) : null;

/**
 * Date-time picker. Without a timezone it stores an ISO-8601 UTC string; with one (the DAG
 * timezone) it stores the wall-clock time in that zone (no offset).
 */
export function DateTimeField({
  label,
  help,
  error,
  value,
  onChange,
  field,
  disabled,
  required,
  hideLabel,
  timezone,
  dtype,
}: Base & {
  value: string | null | undefined;
  onChange: (v: string | null) => void;
  timezone?: string | null | undefined;
}) {
  const { t, i18n } = useTranslation("inspector");
  return (
    <Field.Root invalid={!!error} disabled={disabled ?? false} required={required ?? false} {...anchor(field)}>
      <Label
        label={label}
        required={required}
        dtype={dtype ?? "datetime"}
        hideLabel={hideLabel}
        suffix={
          <Text as="span" color="fg.muted">
            {" "}
            ({timezone || t("common.utc")})
          </Text>
        }
      />
      <Input
        size="sm"
        type="datetime-local"
        lang={i18n.language}
        minW={0}
        value={isoToLocalInput(value)}
        onChange={(e) => onChange(localInputToIso(e.target.value, timezone))}
      />
      <Help error={error} help={help} field={field} />
    </Field.Root>
  );
}

/** Ordered list of strings (tags, requirements, cron expressions…). */
export function StringListField({
  label,
  help,
  error,
  value,
  onChange,
  placeholder,
  mono,
  field,
  disabled,
  list,
  addLabel,
  hideLabel,
  dtype,
  required,
}: Base & {
  value: string[];
  onChange: (v: string[]) => void;
  placeholder?: string | undefined;
  mono?: boolean;
  list?: string;
  addLabel?: string;
}) {
  const { t } = useTranslation("inspector");
  const set = (i: number, v: string) => onChange(value.map((x, j) => (j === i ? v : x)));
  const move = (i: number, d: -1 | 1) => {
    const next = [...value];
    const [item] = next.splice(i, 1);
    if (item === undefined) return;
    next.splice(i + d, 0, item);
    onChange(next);
  };
  return (
    <Field.Root invalid={!!error} disabled={disabled ?? false} {...anchor(field)}>
      <Label label={label} required={required} dtype={dtype ?? "list[str]"} hideLabel={hideLabel} />
      <Stack gap={1} w="100%">
        {value.map((item, i) => (
          <HStack key={i} gap={1}>
            <Input
              size="xs"
              fontFamily={mono ? "mono" : undefined}
              value={item}
              list={list}
              placeholder={placeholder}
              aria-label={`${label} ${i + 1}`}
              onChange={(e) => set(i, e.target.value)}
            />
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
              disabled={i === value.length - 1}
              onClick={() => move(i, 1)}
            >
              <LuArrowDown />
            </IconButton>
            <IconButton
              aria-label={t("common.removeItem", { item: item || i + 1 })}
              size="2xs"
              variant="ghost"
              onClick={() => onChange(value.filter((_, j) => j !== i))}
            >
              <LuTrash2 />
            </IconButton>
          </HStack>
        ))}
        <Button size="xs" variant="outline" alignSelf="start" onClick={() => onChange([...value, ""])}>
          <LuPlus /> {addLabel ?? t("common.add")}
        </Button>
      </Stack>
      <Help error={error} help={help} field={field} />
    </Field.Root>
  );
}

/** String → string map editor (owner links, values_display…). */
export function KeyValueField({
  label,
  help,
  error,
  value,
  onChange,
  keyPlaceholder,
  valuePlaceholder,
  field,
  disabled,
  hideLabel,
  dtype,
  required,
}: Base & {
  value: Record<string, string>;
  onChange: (v: Record<string, string>) => void;
  keyPlaceholder?: string;
  valuePlaceholder?: string;
}) {
  const { t } = useTranslation("inspector");
  // Rows are kept as an ordered list so that renaming a key does not reorder entries.
  const entries = Object.entries(value);
  const write = (rows: [string, string][]) => onChange(Object.fromEntries(rows));
  return (
    <Field.Root invalid={!!error} disabled={disabled ?? false} {...anchor(field)}>
      <Label label={label} required={required} dtype={dtype ?? "dict[str, str]"} hideLabel={hideLabel} />
      <Stack gap={1} w="100%">
        {entries.map(([k, v], i) => (
          <HStack key={i} gap={1}>
            <Input
              size="xs"
              fontFamily="mono"
              value={k}
              placeholder={keyPlaceholder ?? t("common.key")}
              aria-label={`${label}: ${t("common.key")} ${i + 1}`}
              onChange={(e) => write(entries.map((row, j) => (j === i ? [e.target.value, row[1]] : row)))}
            />
            <Input
              size="xs"
              value={v}
              placeholder={valuePlaceholder ?? t("common.value")}
              aria-label={`${label}: ${t("common.value")} ${i + 1}`}
              onChange={(e) => write(entries.map((row, j) => (j === i ? [row[0], e.target.value] : row)))}
            />
            <IconButton
              aria-label={t("common.removeItem", { item: k || i + 1 })}
              size="2xs"
              variant="ghost"
              onClick={() => write(entries.filter((_, j) => j !== i))}
            >
              <LuTrash2 />
            </IconButton>
          </HStack>
        ))}
        <Button
          size="xs"
          variant="outline"
          alignSelf="start"
          disabled={"" in value}
          onClick={() => onChange({ ...value, "": "" })}
        >
          <LuPlus /> {t("common.add")}
        </Button>
      </Stack>
      <Help error={error} help={help} field={field} />
    </Field.Root>
  );
}

/** Muted explanatory paragraph. */
export function Hint({ children, tone = "muted" }: { children: ReactNode; tone?: "muted" | "warning" | "error" }) {
  const color = tone === "warning" ? "orange.600" : tone === "error" ? "red.500" : "fg.muted";
  return (
    <Text fontSize="xs" color={color} lineHeight="short">
      {children}
    </Text>
  );
}

/** Bordered sub-section with a title, used to group related controls. */
export function Panel({ title, actions, children }: { title?: ReactNode; actions?: ReactNode; children: ReactNode }) {
  return (
    <Box borderWidth="1px" rounded="md" p={2}>
      {title || actions ? (
        <HStack justify="space-between" mb={1}>
          <Text fontSize="xs" fontWeight="semibold">
            {title}
          </Text>
          {actions}
        </HStack>
      ) : null}
      <Stack gap={2}>{children}</Stack>
    </Box>
  );
}
