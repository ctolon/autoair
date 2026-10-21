import {
  Badge,
  Box,
  Button,
  Code,
  Collapsible,
  HStack,
  IconButton,
  Input,
  Menu,
  Portal,
  Stack,
  Switch,
  Text,
} from "@chakra-ui/react";
import { type ReactNode, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  LuArrowLeftRight,
  LuBraces,
  LuChevronDown,
  LuCode,
  LuFileCode,
  LuLock,
  LuSparkles,
  LuSquareFunction,
  LuTimer,
  LuWandSparkles,
} from "react-icons/lu";

import {
  type JsonSchema,
  type ParamField,
  type FileAssetValue,
  type InlineFunctionValue,
  PYTHON_PATH_RE,
  type ParamMode,
  type PythonRefValue,
  fillRequired,
  functionTemplate,
  hasJinja,
  isFileAsset,
  isInlineFunction,
  isPythonRef,
  paramFields,
  paramMode,
  scriptTemplate,
  withoutKey,
} from "../lib/formSchema";
import { useMeta } from "../lifecycle/hooks";
import { inlineRst } from "../lib/docstring";
import { CodeEditor, type CodeLanguage } from "./code";
import type { PythonLspContext } from "./code/types";
import { type XComRef, isXComRef } from "../lib/types";
import { ConnectionPicker, DATALIST, PoolPicker } from "./AirflowPickers";
import { useEditingNode } from "./context";
import {
  CodeField,
  FIELD_ATTR,
  Hint,
  TypeBadge,
  JsonField,
  NumberField,
  SelectField,
  StringListField,
  TextField,
} from "./fields";
import { TemplateField } from "./TemplateField";
import { type UpstreamOutput, useUpstreamOutputs } from "./upstream";
import { XComChip } from "./XComChip";
import { type XComPayload, useInsertTarget, useLatest, useXcomDrop, xcomRef, xcomTemplate } from "./xcomDnd";

type ParamsFormProps = {
  schema: JsonSchema;
  value: Record<string, unknown>;
  onChange: (v: Record<string, unknown>) => void;
  /** Prefix of the spec path used to focus fields from problems (operator params: none). */
  fieldPrefix?: string | undefined;
  /** Offer upstream task outputs (XCom) as values. Off for notifier params. */
  allowXcom?: boolean;
  /** Fallback connection types for `*_conn_id` params without a per-param hint. */
  connectionTypes?: string[];
  /** Group BaseSensorOperator params into a "Sensor" section. */
  isSensor?: boolean | undefined;
  /** Render `deferrable` as a prominent switch. */
  deferrable?: boolean | undefined;
  /** Params rendered elsewhere (e.g. by a dedicated editor). */
  exclude?: string[];
  /** Validation messages per param name. */
  errors?: Record<string, string>;
  /** Collapse unset optional params under "More parameters" (default true). */
  collapseOptional?: boolean;
  /** Params fixed by an operator template: shown read-only. */
  locked?: ReadonlySet<string> | undefined;
};

const isUnset = (v: unknown) => v === undefined || v === null || v === "";

function defaultText(f: ParamField): string | undefined {
  if (!f.hasDefault || f.default === null || f.default === undefined) return undefined;
  return typeof f.default === "string" ? f.default : JSON.stringify(f.default);
}

/**
 * An importable Python object for params that take callables/classes (or any param):
 * `pkg.module.name`. The generated DAG imports it; needs the allow_python_code permission.
 */
function PythonRefInput({
  field,
  value,
  error,
  onChange,
}: {
  field: ParamField;
  value: string;
  error?: string | undefined;
  onChange: (path: string) => void;
}) {
  const { t } = useTranslation("inspector");
  const meta = useMeta();
  const invalid = value !== "" && !PYTHON_PATH_RE.test(value);
  return (
    <Stack gap={1}>
      <Input
        size="sm"
        fontFamily="mono"
        placeholder={t("params.python.placeholder")}
        aria-label={t("params.python.label", { name: field.name })}
        aria-invalid={invalid || !!error || undefined}
        borderColor={invalid || error ? "red.500" : undefined}
        value={value}
        onChange={(e) => onChange(e.target.value.trim())}
      />
      {invalid ? <Hint tone="error">{t("params.python.invalid")}</Hint> : null}
      {error && !invalid ? <Hint tone="error">{error}</Hint> : null}
      {meta && !meta.allow_python_code ? <Hint tone="warning">{t("params.python.disabled")}</Hint> : null}
      <Hint>{t(field.pythonRef === "required" ? "params.python.helpRequired" : "params.python.help")}</Hint>
    </Stack>
  );
}

/** Params where an embedded script makes sense: text, code, or untyped values. */
const acceptsScript = (field: ParamField) =>
  field.pythonRef !== "required" && ["text", "code", "json"].includes(field.widget);

const SCRIPT_LANGUAGE: Record<string, CodeLanguage> = {
  py: "python",
  sh: "bash",
  bash: "bash",
  sql: "sql",
  hql: "sql",
  yaml: "yaml",
  yml: "yaml",
  json: "json",
  md: "markdown",
  go: "go",
  java: "java",
  js: "javascript",
  mjs: "javascript",
  ts: "typescript",
};
const scriptLanguage = (name: string): CodeLanguage =>
  SCRIPT_LANGUAGE[name.split(".").pop()?.toLowerCase() ?? ""] ?? "text";

/** A Python function written in the builder (emitted in the DAG file, passed as the value). */
function InlineFunctionInput({
  field,
  value,
  error,
  onChange,
  onClear,
}: {
  field: ParamField;
  value: InlineFunctionValue;
  error?: string | undefined;
  onChange: (v: InlineFunctionValue) => void;
  onClear: () => void;
}) {
  const { t } = useTranslation("inspector");
  const meta = useMeta();
  return (
    <Stack gap={1}>
      <CodeEditor
        language="python"
        pythonLsp={MODULE_LSP}
        ariaLabel={t("params.function.label", { name: field.name })}
        value={value.source}
        minHeight="120px"
        maxHeight="420px"
        invalid={!!error}
        onChange={(source) => onChange({ $type: "function", source })}
      />
      {error ? <Hint tone="error">{error}</Hint> : null}
      {meta && !meta.allow_python_code ? <Hint tone="warning">{t("params.python.disabled")}</Hint> : null}
      <HStack justify="space-between">
        <Hint>{t("params.function.help")}</Hint>
        {field.pythonRef !== "required" ? (
          <Button size="2xs" variant="ghost" onClick={onClear}>
            {t("params.function.remove")}
          </Button>
        ) : null}
      </HStack>
    </Stack>
  );
}

/** A script written in the builder: embedded in the DAG, written to a file on every worker. */
function ScriptFileInput({
  field,
  value,
  error,
  onChange,
}: {
  field: ParamField;
  value: FileAssetValue;
  error?: string | undefined;
  onChange: (v: FileAssetValue) => void;
}) {
  const { t } = useTranslation("inspector");
  const meta = useMeta();
  const badName = !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,120}$/.test(value.name);
  return (
    <Stack gap={1}>
      <HStack gap={2}>
        <Input
          size="xs"
          fontFamily="mono"
          flex="1"
          aria-label={t("params.file.name", { name: field.name })}
          aria-invalid={badName || undefined}
          borderColor={badName ? "red.500" : undefined}
          value={value.name}
          onChange={(e) => onChange({ ...value, name: e.target.value.trim() })}
        />
        <Input
          size="xs"
          fontFamily="mono"
          w="110px"
          placeholder={t("params.file.runWithPlaceholder")}
          aria-label={t("params.file.runWith")}
          title={t("params.file.runWithHelp")}
          value={value.run_with ?? ""}
          onChange={(e) => onChange({ ...value, run_with: e.target.value.trim() || null })}
        />
      </HStack>
      <CodeEditor
        language={scriptLanguage(value.name)}
        pythonLsp={scriptLanguage(value.name) === "python" ? MODULE_LSP : undefined}
        ariaLabel={t("params.file.content", { name: value.name })}
        value={value.content}
        minHeight="140px"
        maxHeight="480px"
        invalid={!!error}
        onChange={(content) => onChange({ ...value, content })}
      />
      {badName ? <Hint tone="error">{t("params.file.badName")}</Hint> : null}
      {error ? <Hint tone="error">{error}</Hint> : null}
      {meta && !meta.allow_python_code ? <Hint tone="warning">{t("params.python.disabled")}</Hint> : null}
      <Hint>{t(value.run_with ? "params.file.helpRun" : "params.file.help", { run: value.run_with ?? "" })}</Hint>
    </Stack>
  );
}

/** Menu switching a param between a literal value, a Jinja template and an upstream output. */
function ModeMenu({
  field,
  mode,
  upstream,
  onPick,
}: {
  field: ParamField;
  mode: ParamMode;
  upstream: UpstreamOutput[] | null;
  onPick: (
    choice: { mode: "literal" | "template" | "python" | "function" | "file" } | { mode: "xcom"; payload: XComPayload },
  ) => void;
}) {
  const { t } = useTranslation("inspector");
  const icon =
    mode === "xcom" ? (
      <LuArrowLeftRight />
    ) : mode === "template" ? (
      <LuBraces />
    ) : mode === "python" ? (
      <LuCode />
    ) : mode === "function" ? (
      <LuSquareFunction />
    ) : mode === "file" ? (
      <LuFileCode />
    ) : (
      <LuSparkles />
    );
  return (
    <Menu.Root positioning={{ placement: "bottom-end" }}>
      <Menu.Trigger asChild>
        <IconButton
          size="2xs"
          variant={mode === "literal" ? "ghost" : "subtle"}
          aria-label={t("params.mode.change", { name: field.name, mode: t(`params.mode.${mode}`) })}
          title={t(`params.mode.${mode}`)}
        >
          {icon}
        </IconButton>
      </Menu.Trigger>
      <Portal>
        <Menu.Positioner>
          <Menu.Content minW="220px" maxH="320px" overflowY="auto">
            {field.pythonRef !== "required" ? (
              <Menu.Item value="literal" onSelect={() => onPick({ mode: "literal" })}>
                <LuSparkles /> {t("params.mode.literal")}
              </Menu.Item>
            ) : null}
            {field.pythonRef ? (
              <Menu.Item value="function" onSelect={() => onPick({ mode: "function" })}>
                <LuSquareFunction /> {t("params.mode.function")}
              </Menu.Item>
            ) : null}
            <Menu.Item value="python" onSelect={() => onPick({ mode: "python" })}>
              <LuCode /> {t("params.mode.python")}
            </Menu.Item>
            {acceptsScript(field) ? (
              <Menu.Item value="file" onSelect={() => onPick({ mode: "file" })}>
                <LuFileCode /> {t("params.mode.file")}
              </Menu.Item>
            ) : null}
            {field.pythonRef !== "required" && field.widget !== "text" && field.widget !== "code" ? (
              <Menu.Item value="template" onSelect={() => onPick({ mode: "template" })}>
                <LuBraces /> {t("params.mode.template")}
              </Menu.Item>
            ) : null}
            {upstream ? (
              <Menu.ItemGroup>
                <Menu.ItemGroupLabel fontSize="xs">{t("params.mode.xcom")}</Menu.ItemGroupLabel>
                {upstream.length === 0 ? (
                  <Text px={2} py={1} fontSize="xs" color="fg.muted">
                    {t("params.mode.noUpstream")}
                  </Text>
                ) : null}
                {upstream.flatMap((u) => [
                  <Menu.Item
                    key={u.node.id}
                    value={`xcom:${u.node.id}`}
                    onSelect={() => onPick({ mode: "xcom", payload: { node: u.node.id, taskId: u.taskId } })}
                  >
                    <LuArrowLeftRight /> <Text fontFamily="mono">{u.label}</Text>
                  </Menu.Item>,
                  ...u.keys.map((k) => (
                    <Menu.Item
                      key={`${u.node.id}:${k}`}
                      value={`xcom:${u.node.id}:${k}`}
                      ps={6}
                      onSelect={() => onPick({ mode: "xcom", payload: { node: u.node.id, taskId: u.taskId, key: k } })}
                    >
                      <Text fontFamily="mono" fontSize="xs">
                        {u.label}[{k}]
                      </Text>
                    </Menu.Item>
                  )),
                ])}
              </Menu.ItemGroup>
            ) : null}
          </Menu.Content>
        </Menu.Positioner>
      </Portal>
    </Menu.Root>
  );
}

function FieldHeader({ field, extra }: { field: ParamField; extra?: ReactNode }) {
  const { t } = useTranslation("inspector");
  return (
    <HStack justify="space-between" gap={1} mb={0.5}>
      <HStack gap={1} minW={0}>
        <Text fontSize="xs" fontWeight="medium" fontFamily="mono" truncate title={field.name}>
          {field.title}
          {field.required ? (
            <Text as="span" color="red.500" aria-label={t("params.required")}>
              {" *"}
            </Text>
          ) : null}
        </Text>
        <TypeBadge dtype={field.dtype} />
        {field.templated ? (
          <Badge size="xs" variant="outline" colorPalette="purple" title={t("params.templatedHelp")}>
            Jinja
          </Badge>
        ) : null}
      </HStack>
      {extra}
    </HStack>
  );
}

/** reST inline markup (``literal``, :class:`X`, links) → text with code spans. */
function InlineDoc({ text }: { text: string }) {
  const parts = inlineRst(text).split(/(`[^`]+`)/);
  return (
    <>
      {parts.map((part, i) =>
        part.startsWith("`") && part.endsWith("`") && part.length > 2 ? (
          <Code key={i} fontSize="0.95em" px={0.5} py={0} variant="subtle">
            {part.slice(1, -1)}
          </Code>
        ) : (
          part.replace(/\[([^\]]+)\]\([^)]+\)/g, "$1")
        ),
      )}
    </>
  );
}

/** Long operator docs are clamped to a few lines (full text on hover / expand). */
function Description({ text }: { text: string | undefined }) {
  const [open, setOpen] = useState(false);
  const { t } = useTranslation("inspector");
  if (!text) return null;
  const long = text.length > 160;
  return (
    <Text fontSize="xs" color="fg.muted" lineClamp={open || !long ? undefined : 2} title={long ? text : undefined}>
      <InlineDoc text={text} />{" "}
      {long ? (
        <Button size="2xs" variant="plain" px={0} h="auto" onClick={() => setOpen((o) => !o)}>
          {open ? t("params.less") : t("params.more")}
        </Button>
      ) : null}
    </Text>
  );
}

function LiteralInput({
  field,
  value,
  onChange,
  connTypes,
  error,
}: {
  field: ParamField;
  value: unknown;
  onChange: (v: unknown) => void;
  connTypes: string[];
  error?: string | undefined;
}) {
  const { t } = useTranslation("inspector");
  const def = defaultText(field);
  const placeholder = def !== undefined ? t("params.defaultPlaceholder", { value: def }) : undefined;
  const common = { label: field.title, hideLabel: true, error, required: field.required };
  switch (field.widget) {
    case "integer":
    case "number":
      return (
        <NumberField
          {...common}
          value={typeof value === "number" ? value : null}
          step={field.widget === "integer" ? 1 : undefined}
          placeholder={placeholder}
          onChange={(v) => onChange(v ?? undefined)}
        />
      );
    case "boolean":
      return (
        <SelectField
          {...common}
          value={value === true ? "true" : value === false ? "false" : null}
          emptyLabel={def !== undefined ? t("params.defaultPlaceholder", { value: def }) : t("params.unset")}
          options={[
            { value: "true", label: "True" },
            { value: "false", label: "False" },
          ]}
          onChange={(v) => onChange(v === null ? undefined : v === "true")}
        />
      );
    case "enum":
      return (
        <SelectField
          {...common}
          value={value === undefined || value === null ? null : String(value)}
          emptyLabel={def !== undefined ? t("params.defaultPlaceholder", { value: def }) : t("params.unset")}
          options={(field.enumValues ?? []).map((e) => ({ value: String(e), label: String(e) }))}
          onChange={(v) => onChange(v === null ? undefined : (field.enumValues?.find((e) => String(e) === v) ?? v))}
        />
      );
    case "connection":
      return (
        <ConnectionPicker
          {...common}
          connTypes={field.connTypes.length ? field.connTypes : connTypes}
          value={typeof value === "string" ? value : ""}
          placeholder={def}
          onChange={(v) => onChange(v ?? undefined)}
        />
      );
    case "variable":
      return (
        <TextField
          {...common}
          mono
          list={DATALIST.variable}
          value={typeof value === "string" ? value : ""}
          placeholder={placeholder ?? "{{ var.value.my_variable }}"}
          onChange={(v) => onChange(v ?? undefined)}
        />
      );
    case "secret":
      return (
        <TextField
          {...common}
          type="password"
          value={typeof value === "string" ? value : ""}
          placeholder={placeholder}
          onChange={(v) => onChange(v ?? undefined)}
        />
      );
    case "code":
      return (
        <CodeField
          {...common}
          rows={4}
          language={codeLanguage(field.renderer)}
          value={typeof value === "string" ? value : value === undefined ? "" : JSON.stringify(value)}
          placeholder={placeholder}
          onChange={(v) => onChange(v === "" ? undefined : v)}
        />
      );
    case "pool":
      return (
        <PoolPicker
          {...common}
          value={typeof value === "string" ? value : ""}
          placeholder={def}
          onChange={(v) => onChange(v ?? undefined)}
        />
      );
    case "string-list":
      return (
        <StringListField
          {...common}
          value={Array.isArray(value) ? value.map((x) => (typeof x === "string" ? x : JSON.stringify(x))) : []}
          onChange={(v) => onChange(v.length ? v : undefined)}
          mono
        />
      );
    case "json":
      return (
        <JsonField
          {...common}
          rows={3}
          value={value}
          // `str | list[str]`-style unions: plain text is a string, JSON only when it looks like it.
          acceptsText={acceptsString(field.schema)}
          onChange={(v) => onChange(v)}
        />
      );
    default:
      return (
        <TextField
          {...common}
          value={typeof value === "string" ? value : value === undefined ? "" : JSON.stringify(value)}
          placeholder={placeholder}
          onChange={(v) => onChange(v ?? undefined)}
        />
      );
  }
}

const CODE_LANGUAGES: Record<string, CodeLanguage> = {
  sql: "sql",
  bash: "bash",
  python: "python",
  py: "python",
  yaml: "yaml",
  json: "json",
  markdown: "markdown",
  md: "markdown",
  doc_md: "markdown",
};

/** Code editor language of a renderer (`x-autoair-renderer` / a code widget override). */
function codeLanguage(renderer: string | undefined): CodeLanguage {
  return (renderer && CODE_LANGUAGES[renderer.toLowerCase()]) || "text";
}

/** The (union) schema accepts a plain string. */
function acceptsString(schema: JsonSchema): boolean {
  const variants = schema.anyOf ?? schema.oneOf ?? [schema];
  return variants.some((v) => v.type === "string" || (Array.isArray(v.type) && v.type.includes("string")));
}

/** Python language services for whole modules (inline functions, embedded .py files). */
const MODULE_LSP: PythonLspContext = { functionName: "", arguments: [], usesLoopContext: false, mode: "module" };

function ParamRow({
  field,
  value,
  onChange,
  fieldAttr,
  allowXcom,
  connTypes,
  error,
  onEditStart,
}: {
  field: ParamField;
  value: unknown;
  onChange: (v: unknown) => void;
  fieldAttr: string;
  allowXcom: boolean;
  connTypes: string[];
  error?: string | undefined;
  /** The user focused one of the row's controls. */
  onEditStart?: () => void;
}) {
  const { t } = useTranslation("inspector");
  const nodeId = useEditingNode();
  const upstream = useUpstreamOutputs(allowXcom ? nodeId : null);
  const setTarget = useInsertTarget((s) => s.setTarget);
  const [forceTemplate, setForceTemplate] = useState(false);
  const latestChange = useLatest(onChange);
  const mode = paramMode(field, value);
  const textLike = field.widget === "text" || field.widget === "code";
  const asTemplate =
    mode !== "xcom" &&
    ((field.templated && textLike) || (typeof value === "string" && hasJinja(value)) || forceTemplate);

  const onPayload = (p: XComPayload) => {
    if (field.templated) {
      const current = typeof value === "string" ? value : "";
      onChange(current ? `${current}${xcomTemplate(p)}` : xcomTemplate(p));
    } else if (allowXcom) onChange(xcomRef(p));
  };
  const { isOver, dropProps } = useXcomDrop(allowXcom || field.templated ? onPayload : null);

  const menu = (
    <ModeMenu
      field={field}
      mode={asTemplate && !field.templated ? "template" : mode}
      upstream={allowXcom ? upstream : null}
      onPick={(c) => {
        if (c.mode === "xcom") {
          setForceTemplate(false);
          onChange(xcomRef(c.payload));
        } else if (c.mode === "python") {
          setForceTemplate(false);
          if (!isPythonRef(value)) onChange({ $type: "import", path: "" } satisfies PythonRefValue);
        } else if (c.mode === "function") {
          setForceTemplate(false);
          if (!isInlineFunction(value)) onChange({ $type: "function", source: functionTemplate(field.name) });
        } else if (c.mode === "file") {
          setForceTemplate(false);
          if (!isFileAsset(value)) onChange(scriptTemplate(field.name));
        } else if (c.mode === "template") {
          setForceTemplate(true);
          if (typeof value !== "string") onChange(undefined);
        } else {
          setForceTemplate(false);
          onChange(undefined);
        }
      }}
    />
  );

  let body: ReactNode;
  if (mode === "function" && isInlineFunction(value)) {
    body = (
      <InlineFunctionInput
        field={field}
        value={value}
        error={error}
        onChange={onChange}
        onClear={() => onChange(undefined)}
      />
    );
  } else if (mode === "file" && isFileAsset(value)) {
    body = <ScriptFileInput field={field} value={value} error={error} onChange={onChange} />;
  } else if (mode === "python") {
    body = (
      <PythonRefInput
        field={field}
        value={isPythonRef(value) ? value.path : ""}
        error={error}
        onChange={(path) => onChange(path || field.pythonRef === "required" ? { $type: "import", path } : undefined)}
      />
    );
  } else if (mode === "xcom" && isXComRef(value)) {
    body = (
      <HStack gap={1} wrap="wrap">
        <XComChip value={value} onRemove={() => onChange(undefined)} />
        <Input
          size="2xs"
          w="120px"
          fontFamily="mono"
          placeholder={t("params.xcomKey")}
          aria-label={t("params.xcomKeyLabel", { name: field.name })}
          value={value.key ?? ""}
          onChange={(e) => {
            const next: XComRef = { $type: "xcom", node: value.node };
            if (e.target.value) next.key = e.target.value;
            onChange(next);
          }}
        />
      </HStack>
    );
  } else if (asTemplate) {
    body = (
      <Stack gap={1}>
        <TemplateField
          label={field.title}
          hideLabel
          field={undefined}
          targetId={fieldAttr}
          required={field.required}
          error={error}
          renderer={field.widget === "code" ? (field.renderer ?? null) : null}
          compact={field.widget !== "code"}
          placeholder={defaultText(field)}
          value={typeof value === "string" ? value : ""}
          onChange={(v) => onChange(v ?? undefined)}
        />
        {!field.templated ? <Hint tone="warning">{t("params.notTemplated")}</Hint> : null}
      </Stack>
    );
  } else {
    body = <LiteralInput field={field} value={value} onChange={onChange} connTypes={connTypes} error={error} />;
  }

  return (
    <Box
      {...{ [FIELD_ATTR]: fieldAttr }}
      {...dropProps}
      rounded="md"
      outline={isOver ? "2px dashed" : undefined}
      outlineColor="teal.500"
      outlineOffset="2px"
      onFocusCapture={() => {
        onEditStart?.();
        // Templated editors register themselves (insert at cursor); others take an XCom ref.
        if (!asTemplate && allowXcom && mode !== "xcom")
          setTarget({ id: fieldAttr, label: field.title, setXcom: latestChange });
      }}
    >
      <FieldHeader field={field} extra={menu} />
      {body}
      <Description text={field.description} />
    </Box>
  );
}

/**
 * Optional parameters worth showing without expanding "more parameters" — otherwise operators
 * whose arguments all have defaults (SparkSubmitOperator, HttpOperator…) open with an empty form:
 * connections, code fields and the first templated fields (the operator's "inputs"), at most 4.
 */
function featuredFields(fields: ParamField[]): Set<string> {
  const out: string[] = [];
  for (const f of fields) if (!f.required && (f.widget === "connection" || f.renderer)) out.push(f.name);
  for (const f of fields) {
    if (out.length >= 4) break;
    if (!f.required && f.templated && !out.includes(f.name)) out.push(f.name);
  }
  return new Set(out.slice(0, 4));
}

/**
 * Form for catalog-introspected params (operators, notifiers). Every param can hold a
 * literal value, a Jinja template, or the output of an upstream task (XCom reference).
 */
export function ParamsForm({
  schema,
  value,
  onChange,
  fieldPrefix,
  allowXcom = true,
  connectionTypes = [],
  isSensor,
  deferrable,
  exclude,
  errors,
  collapseOptional = true,
  locked,
}: ParamsFormProps) {
  const { t } = useTranslation("inspector");
  const [filter, setFilter] = useState("");
  const fields = useMemo(
    // Hidden by a catalog override: not rendered (a value already set stays in the spec).
    () => paramFields(schema, { isSensor, deferrable }).filter((f) => !f.hidden && !exclude?.includes(f.name)),
    [schema, isSensor, deferrable, exclude],
  );
  const set = (name: string, v: unknown) => {
    onChange(isUnset(v) ? withoutKey(value, name) : { ...value, [name]: v });
  };
  // Where a field was (main list or "more parameters") when the user started editing it. A
  // field moves between the two when it gets or loses a value; doing that while it is being
  // typed into remounts its editor: the caret (and focus) was lost after the first character.
  const [pinned, setPinned] = useState<ReadonlyMap<string, boolean>>(() => new Map());
  // Forgotten when the form shows other fields (callers rebuild `fields` on every render).
  const pinKey = `${fieldPrefix ?? ""}:${fields.map((f) => f.name).join(",")}`;
  const [pinnedFor, setPinnedFor] = useState(pinKey);
  if (pinnedFor !== pinKey) {
    setPinnedFor(pinKey);
    setPinned(new Map());
  }
  const attr = (name: string) => (fieldPrefix ? `${fieldPrefix}.${name}` : name);
  const row = (f: ParamField) => {
    const content = (
      <ParamRow
        key={f.name}
        field={f}
        value={value[f.name]}
        onChange={(v) => set(f.name, v)}
        fieldAttr={attr(f.name)}
        allowXcom={allowXcom}
        connTypes={connectionTypes}
        error={errors?.[f.name]}
        onEditStart={() => {
          if (!pinned.has(f.name)) setPinned(new Map(pinned).set(f.name, shown(f)));
        }}
      />
    );
    if (!locked?.has(f.name)) return content;
    // Locked by an operator template: visible, not editable (`inert` also removes it from the tab order).
    return (
      <Box key={f.name} position="relative" data-locked="true" title={t("params.lockedByTemplate")}>
        <Box inert opacity={0.75} aria-readonly>
          {content}
        </Box>
        <Box position="absolute" top={0} insetEnd={0} color="fg.muted" aria-label={t("params.lockedByTemplate")}>
          <LuLock size={12} />
        </Box>
      </Box>
    );
  };

  const deferrableField = fields.find((f) => f.group === "deferrable");
  const sensorFields = fields.filter((f) => f.group === "sensor");
  const main = fields.filter((f) => f.group === "main");
  const featured = collapseOptional ? featuredFields(main) : new Set<string>();
  const shown = (f: ParamField) =>
    pinned.get(f.name) ?? (f.required || featured.has(f.name) || !isUnset(value[f.name]));
  const primary = collapseOptional ? main.filter(shown) : main;
  const optional = collapseOptional ? main.filter((f) => !shown(f)) : [];
  const q = filter.trim().toLowerCase();
  const visibleOptional = q
    ? optional.filter((f) => f.name.toLowerCase().includes(q) || (f.description ?? "").toLowerCase().includes(q))
    : optional;
  const missingRequired = fields.filter((f) => f.required && isUnset(value[f.name]));
  // Required params "fill with defaults" could not fill (no real default exists).
  const [unfilled, setUnfilled] = useState<string[]>([]);

  if (!fields.length) return <Hint>{t("params.none")}</Hint>;

  return (
    <Stack gap={3}>
      {missingRequired.length ? (
        <HStack justify="space-between" bg="orange.subtle" rounded="md" px={2} py={1}>
          <Text fontSize="xs">{t("params.missingRequired", { count: missingRequired.length })}</Text>
          <Button
            size="2xs"
            variant="outline"
            onClick={() => {
              const { value: filled, unfilled } = fillRequired(fields, value);
              onChange(filled);
              setUnfilled(unfilled);
            }}
          >
            <LuWandSparkles /> {t("params.fillRequired")}
          </Button>
        </HStack>
      ) : null}
      {unfilled.some((n) => missingRequired.some((f) => f.name === n)) ? (
        <Hint tone="warning">
          {t("params.noDefault", {
            names: unfilled.filter((n) => missingRequired.some((f) => f.name === n)).join(", "),
          })}
        </Hint>
      ) : null}

      {deferrableField ? (
        <Box
          borderWidth="1px"
          borderColor="teal.muted"
          bg="teal.subtle"
          rounded="md"
          p={2}
          {...{ [FIELD_ATTR]: attr(deferrableField.name) }}
        >
          <Switch.Root
            size="sm"
            colorPalette="teal"
            checked={value.deferrable === true}
            onCheckedChange={(e) => set("deferrable", e.checked ? true : undefined)}
          >
            <Switch.HiddenInput />
            <Switch.Control />
            <Switch.Label fontSize="sm" fontWeight="medium">
              <HStack gap={1}>
                <LuTimer aria-hidden /> {t("params.deferrable")}
              </HStack>
            </Switch.Label>
          </Switch.Root>
          <Hint>{t("params.deferrableHelp")}</Hint>
          {typeof value.deferrable === "string" ? (
            <Hint tone="warning">{t("params.deferrableTemplated", { value: value.deferrable })}</Hint>
          ) : null}
        </Box>
      ) : null}

      {primary.map(row)}

      {sensorFields.length ? (
        <Box borderWidth="1px" rounded="md" p={2}>
          <Text fontSize="xs" fontWeight="semibold" mb={1}>
            {t("params.sensorSection")}
          </Text>
          <Hint>{t("params.sensorHelp")}</Hint>
          <Stack gap={3} mt={2}>
            {sensorFields.map(row)}
          </Stack>
        </Box>
      ) : null}

      {optional.length ? (
        <Collapsible.Root>
          <Collapsible.Trigger asChild>
            <Button size="xs" variant="ghost" px={1} alignSelf="start">
              <LuChevronDown /> {t("params.moreParams", { count: optional.length })}
            </Button>
          </Collapsible.Trigger>
          <Collapsible.Content>
            <Stack gap={3} pt={2}>
              {optional.length > 6 ? (
                <Input
                  size="xs"
                  placeholder={t("params.filter")}
                  aria-label={t("params.filter")}
                  value={filter}
                  onChange={(e) => setFilter(e.target.value)}
                />
              ) : null}
              {visibleOptional.map(row)}
            </Stack>
          </Collapsible.Content>
        </Collapsible.Root>
      ) : null}
    </Stack>
  );
}
