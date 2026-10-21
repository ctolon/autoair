import { Box, Button, Code, HStack, IconButton, Input, NativeSelect, Stack, Switch, Text } from "@chakra-ui/react";
import { useTranslation } from "react-i18next";
import { LuDatabase, LuPlug, LuPlus, LuTrash2 } from "react-icons/lu";

import { withoutKey } from "../../lib/formSchema";
import {
  CLASS_NAME,
  OUTPUT_FIELD_TYPES,
  type OutputField,
  type OutputFieldType,
  type OutputModelValue,
  type ToolsetValue,
  cleanToolset,
  isOutputModel,
  isToolset,
  newOutputModel,
  outputModelErrors,
  outputModelPreview,
  timedelta,
  timedeltaSeconds,
} from "../../lib/llm";
import { ConnectionPicker } from "../../widgets/AirflowPickers";
import {
  FIELD_ATTR,
  Hint,
  NumberField,
  SectionTitle,
  SelectField,
  StringListField,
  SwitchField,
  TextField,
} from "../../widgets/fields";
import { TemplateField } from "../../widgets/TemplateField";

/** `output_type`: plain text (str) or a structured pydantic model built from typed fields. */
export function OutputTypeEditor({
  value,
  onChange,
  error,
}: {
  value: unknown;
  onChange: (v: OutputModelValue | undefined) => void;
  error?: string | undefined;
}) {
  const { t } = useTranslation("inspector");
  const model = isOutputModel(value) ? value : null;
  const problems = model ? new Set(outputModelErrors(model)) : new Set<string>();
  const setField = (i: number, patch: Partial<OutputField>) =>
    model && onChange({ ...model, fields: model.fields.map((f, j) => (j === i ? { ...f, ...patch } : f)) });
  return (
    <Stack gap={2}>
      <SelectField
        label={t("llm.outputType")}
        field="output_type"
        dtype="type"
        help={t("llm.outputTypeHelp")}
        value={model ? "model" : "text"}
        options={[
          { value: "text", label: t("llm.outputText") },
          { value: "model", label: t("llm.outputStructured") },
        ]}
        onChange={(v) => onChange(v === "model" ? (model ?? newOutputModel()) : undefined)}
      />
      {error ? <Hint tone="error">{error}</Hint> : null}
      {!model && value !== undefined && value !== null ? <Hint tone="warning">{t("llm.outputTypeOther")}</Hint> : null}
      {model ? (
        <Stack gap={2} borderLeftWidth="2px" pl={2}>
          <HStack align="start">
            <TextField
              label={t("llm.className")}
              help={t("llm.classNameHelp")}
              mono
              required
              error={problems.has("name") ? t("llm.classNameInvalid") : undefined}
              value={model.name}
              onChange={(v) => onChange({ ...model, name: v ?? "" })}
            />
            <TextField
              label={t("llm.modelDescription")}
              value={model.description ?? ""}
              onChange={(v) => onChange(v ? { ...model, description: v } : { ...model, description: null })}
            />
          </HStack>
          <Stack gap={1}>
            {model.fields.map((f, i) => (
              <Box key={i} borderWidth="1px" rounded="md">
                <Box p={1}>
                  <Stack gap={1}>
                    <HStack gap={1}>
                      <Input
                        size="xs"
                        fontFamily="mono"
                        aria-label={`${t("llm.fieldName")} ${i + 1}`}
                        aria-invalid={problems.has(`fields.${i}.name`) || undefined}
                        borderColor={problems.has(`fields.${i}.name`) ? "border.error" : undefined}
                        value={f.name}
                        onChange={(e) => setField(i, { name: e.target.value })}
                      />
                      <NativeSelect.Root size="xs" width="9rem">
                        <NativeSelect.Field
                          aria-label={`${t("llm.fieldType")} ${i + 1}`}
                          value={f.type ?? "str"}
                          onChange={(e) => {
                            const type = e.target.value as OutputFieldType;
                            setField(i, type === "str" ? { type } : { type, choices: [] });
                          }}
                        >
                          {OUTPUT_FIELD_TYPES.map((ty) => (
                            <option key={ty} value={ty}>
                              {ty}
                            </option>
                          ))}
                        </NativeSelect.Field>
                        <NativeSelect.Indicator />
                      </NativeSelect.Root>
                      <Switch.Root
                        size="sm"
                        checked={f.required !== false}
                        onCheckedChange={(e) => setField(i, { required: e.checked })}
                      >
                        <Switch.HiddenInput aria-label={`${t("llm.fieldRequired")} ${i + 1}`} />
                        <Switch.Control />
                        <Switch.Label fontSize="xs" aria-hidden>
                          {t("llm.fieldRequired")}
                        </Switch.Label>
                      </Switch.Root>
                      <IconButton
                        aria-label={t("common.removeItem", { item: f.name || i + 1 })}
                        size="2xs"
                        variant="ghost"
                        disabled={model.fields.length <= 1}
                        onClick={() => onChange({ ...model, fields: model.fields.filter((_, j) => j !== i) })}
                      >
                        <LuTrash2 />
                      </IconButton>
                    </HStack>
                    <Input
                      size="xs"
                      placeholder={t("llm.fieldDescription")}
                      aria-label={`${t("llm.fieldDescription")} ${i + 1}`}
                      value={f.description ?? ""}
                      onChange={(e) => setField(i, { description: e.target.value || null })}
                    />
                    {(f.type ?? "str") === "str" ? (
                      <StringListField
                        label={t("llm.fieldChoices")}
                        help={t("llm.fieldChoicesHelp")}
                        value={f.choices ?? []}
                        onChange={(choices) => setField(i, { choices })}
                      />
                    ) : null}
                  </Stack>
                </Box>
              </Box>
            ))}
          </Stack>
          <Button
            size="xs"
            variant="outline"
            alignSelf="start"
            onClick={() =>
              onChange({
                ...model,
                fields: [...model.fields, { name: `field_${model.fields.length + 1}`, type: "str" }],
              })
            }
          >
            <LuPlus /> {t("llm.addField")}
          </Button>
          <Box>
            <Text fontSize="xs" color="fg.muted">
              {t("llm.preview")}
            </Text>
            <Code
              display="block"
              whiteSpace="pre"
              fontSize="xs"
              p={2}
              overflowX="auto"
              data-testid="output-model-preview"
            >
              {outputModelPreview(model)}
            </Code>
          </Box>
          {CLASS_NAME.test(model.name) ? null : <Hint tone="error">{t("llm.classNameInvalid")}</Hint>}
        </Stack>
      ) : null}
    </Stack>
  );
}

/** `toolsets`: SQL (SQLToolset) and MCP (MCPToolset) toolsets bound to Airflow connections. */
export function ToolsetsEditor({
  value,
  onChange,
  error,
}: {
  value: unknown;
  onChange: (v: ToolsetValue[] | undefined) => void;
  error?: string | undefined;
}) {
  const { t } = useTranslation("inspector");
  const list = Array.isArray(value) ? value : [];
  const toolsets = list.filter(isToolset);
  const others = list.length - toolsets.length;
  const commit = (next: ToolsetValue[]) => onChange(next.length ? next.map(cleanToolset) : undefined);
  const set = (i: number, patch: Partial<ToolsetValue>) =>
    commit(toolsets.map((ts, j) => (j === i ? { ...ts, ...patch } : ts)));
  return (
    <Stack gap={2} {...{ [FIELD_ATTR]: "toolsets" }}>
      <Text fontSize="sm" fontWeight="medium">
        {t("llm.toolsets")}
      </Text>
      <Hint>{t("llm.toolsetsHelp")}</Hint>
      {error ? <Hint tone="error">{error}</Hint> : null}
      {others ? <Hint tone="warning">{t("llm.toolsetsOther", { count: others })}</Hint> : null}
      {toolsets.map((ts, i) => (
        <Stack key={i} gap={1} borderWidth="1px" rounded="md" p={2} data-testid={`toolset-${i}`}>
          <HStack justify="space-between">
            <HStack gap={1}>
              {ts.kind === "sql" ? <LuDatabase /> : <LuPlug />}
              <Text fontSize="xs" fontWeight="medium">
                {ts.kind === "sql" ? "SQLToolset" : "MCPToolset"}
              </Text>
            </HStack>
            <IconButton
              aria-label={t("common.removeItem", { item: ts.conn_id || i + 1 })}
              size="2xs"
              variant="ghost"
              onClick={() => commit(toolsets.filter((_, j) => j !== i))}
            >
              <LuTrash2 />
            </IconButton>
          </HStack>
          <ConnectionPicker
            label={t(ts.kind === "sql" ? "llm.dbConnection" : "llm.mcpConnection")}
            required
            connTypes={ts.kind === "mcp" ? ["mcp"] : []}
            value={ts.conn_id}
            onChange={(v) => set(i, { conn_id: v ?? "" })}
          />
          {ts.kind === "sql" ? (
            <>
              <StringListField
                label={t("llm.allowedTables")}
                help={t("llm.allowedTablesHelp")}
                mono
                value={ts.allowed_tables ?? []}
                onChange={(v) => set(i, { allowed_tables: v })}
              />
              <HStack align="start">
                <TextField
                  label={t("llm.dbSchema")}
                  mono
                  value={ts.db_schema ?? ""}
                  onChange={(v) => set(i, { db_schema: v })}
                />
                <NumberField
                  label={t("llm.maxRows")}
                  min={1}
                  placeholder="50"
                  value={ts.max_rows ?? null}
                  onChange={(v) => set(i, { max_rows: v })}
                />
              </HStack>
              <SwitchField
                label={t("llm.allowWrites")}
                help={t("llm.allowWritesHelp")}
                checked={ts.allow_writes === true}
                onChange={(v) => set(i, { allow_writes: v })}
              />
            </>
          ) : (
            <TextField
              label={t("llm.toolPrefix")}
              mono
              value={ts.tool_prefix ?? ""}
              onChange={(v) => set(i, { tool_prefix: v })}
            />
          )}
        </Stack>
      ))}
      <HStack gap={2}>
        <Button
          size="xs"
          variant="outline"
          onClick={() => commit([...toolsets, { $type: "toolset", kind: "sql", conn_id: "" }])}
        >
          <LuDatabase /> {t("llm.addSqlToolset")}
        </Button>
        <Button
          size="xs"
          variant="outline"
          onClick={() => commit([...toolsets, { $type: "toolset", kind: "mcp", conn_id: "" }])}
        >
          <LuPlug /> {t("llm.addMcpToolset")}
        </Button>
      </HStack>
    </Stack>
  );
}

/**
 * Dedicated editor of common.ai LLM / agent tasks: Jinja prompt editors, the structured output
 * builder, the toolset picker and human approval (HITL) options. Used for operator params and
 * `@task.llm`/`@task.agent` decorator kwargs; `fields` says which of them the task accepts.
 */
export function LlmEditor({
  fields,
  value,
  onChange,
  errors,
  required = [],
}: {
  fields: ReadonlySet<string>;
  value: Record<string, unknown>;
  onChange: (v: Record<string, unknown>) => void;
  errors?: Record<string, string> | undefined;
  required?: readonly string[];
}) {
  const { t } = useTranslation("inspector");
  const set = (name: string, v: unknown) => {
    const empty = v === undefined || v === null || v === "" || (Array.isArray(v) && v.length === 0);
    onChange(empty ? withoutKey(value, name) : { ...value, [name]: v });
  };
  const str = (name: string) => (typeof value[name] === "string" ? (value[name] as string) : "");
  const has = (name: string) => fields.has(name);
  const approval = has("require_approval");
  const review = has("enable_hitl_review");
  return (
    <Stack gap={3} data-testid="llm-editor">
      <SectionTitle>{t("llm.title")}</SectionTitle>
      {has("prompt") ? (
        <TemplateField
          label={t("llm.prompt")}
          help={t("llm.promptHelp")}
          field="prompt"
          compact={false}
          required={required.includes("prompt")}
          error={errors?.prompt}
          value={str("prompt")}
          onChange={(v) => set("prompt", v)}
        />
      ) : null}
      {has("system_prompt") ? (
        <TemplateField
          label={t("llm.systemPrompt")}
          help={t("llm.systemPromptHelp")}
          field="system_prompt"
          compact={false}
          error={errors?.system_prompt}
          value={str("system_prompt")}
          onChange={(v) => set("system_prompt", v)}
        />
      ) : null}
      {has("output_type") ? (
        <OutputTypeEditor
          value={value.output_type}
          error={errors?.output_type}
          onChange={(v) => set("output_type", v)}
        />
      ) : null}
      {has("toolsets") ? (
        <ToolsetsEditor value={value.toolsets} error={errors?.toolsets} onChange={(v) => set("toolsets", v)} />
      ) : null}
      {approval ? (
        <Stack gap={2} {...{ [FIELD_ATTR]: "require_approval" }}>
          <SwitchField
            label={t("llm.requireApproval")}
            help={t("llm.requireApprovalHelp")}
            checked={value.require_approval === true}
            onChange={(v) =>
              onChange(
                v
                  ? { ...value, require_approval: true }
                  : withoutKey(
                      withoutKey(withoutKey(value, "require_approval"), "approval_timeout"),
                      "allow_modifications",
                    ),
              )
            }
          />
          {value.require_approval === true ? (
            <Stack gap={2} borderLeftWidth="2px" pl={2}>
              {has("approval_timeout") ? (
                <NumberField
                  dtype="timedelta (s)"
                  label={t("llm.approvalTimeout")}
                  help={t("llm.approvalTimeoutHelp")}
                  field="approval_timeout"
                  min={1}
                  value={timedeltaSeconds(value.approval_timeout)}
                  onChange={(v) => set("approval_timeout", timedelta(v))}
                />
              ) : null}
              {has("allow_modifications") ? (
                <SwitchField
                  label={t("llm.allowModifications")}
                  help={t("llm.allowModificationsHelp")}
                  checked={value.allow_modifications === true}
                  onChange={(v) => set("allow_modifications", v || undefined)}
                />
              ) : null}
            </Stack>
          ) : null}
        </Stack>
      ) : null}
      {review ? (
        <Stack gap={2} {...{ [FIELD_ATTR]: "enable_hitl_review" }}>
          <SwitchField
            label={t("llm.hitlReview")}
            help={t("llm.hitlReviewHelp")}
            checked={value.enable_hitl_review === true}
            onChange={(v) =>
              onChange(
                v
                  ? { ...value, enable_hitl_review: true }
                  : withoutKey(
                      withoutKey(withoutKey(value, "enable_hitl_review"), "max_hitl_iterations"),
                      "hitl_timeout",
                    ),
              )
            }
          />
          {value.enable_hitl_review === true ? (
            <HStack align="start" borderLeftWidth="2px" pl={2}>
              {has("max_hitl_iterations") ? (
                <NumberField
                  label={t("llm.maxIterations")}
                  field="max_hitl_iterations"
                  min={1}
                  placeholder="5"
                  value={typeof value.max_hitl_iterations === "number" ? value.max_hitl_iterations : null}
                  onChange={(v) => set("max_hitl_iterations", v)}
                />
              ) : null}
              {has("hitl_timeout") ? (
                <NumberField
                  dtype="timedelta (s)"
                  label={t("llm.reviewTimeout")}
                  field="hitl_timeout"
                  min={1}
                  value={timedeltaSeconds(value.hitl_timeout)}
                  onChange={(v) => set("hitl_timeout", timedelta(v))}
                />
              ) : null}
            </HStack>
          ) : null}
        </Stack>
      ) : null}
    </Stack>
  );
}
