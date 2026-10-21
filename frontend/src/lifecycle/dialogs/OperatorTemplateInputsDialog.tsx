import { Badge, Button, HStack, Stack, Text } from "@chakra-ui/react";
import type { XYPosition } from "@xyflow/react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { LuLock, LuPlus } from "react-icons/lu";

import type { Template } from "../../api/gen/types.gen";
import { insertOperatorTemplate } from "../../canvas/actions";
import { getPath, isEmptyValue, lockedPaths, requiredPaths } from "../../canvas/operatorTemplates";
import type { JsonSchema } from "../../lib/formSchema";
import { useOperatorDetail } from "../../panels/inspector/OperatorParams";
import { CodeField, JsonField, NumberField, TextField } from "../../widgets/fields";
import { ParamsForm } from "../../widgets/ParamsForm";
import { DialogShell } from "../components/DialogShell";
import { useLifecycle } from "../store";

const short = (v: unknown) => {
  const text = typeof v === "string" ? v : JSON.stringify(v);
  return text && text.length > 80 ? `${text.slice(0, 77)}…` : (text ?? "—");
};

/** Operator params among the required inputs, rendered with the operator's own form widgets. */
function RequiredParams({
  operator,
  names,
  value,
  onChange,
}: {
  operator: string;
  names: string[];
  value: Record<string, unknown>;
  onChange: (v: Record<string, unknown>) => void;
}) {
  const detail = useOperatorDetail(operator);
  const schema = detail.data?.params_schema as JsonSchema | undefined;
  const subset = useMemo((): JsonSchema | undefined => {
    if (!schema) return undefined;
    const properties: Record<string, JsonSchema> = {};
    for (const name of names) properties[name] = (schema.properties?.[name] as JsonSchema | undefined) ?? {};
    return { ...schema, properties, required: names };
  }, [schema, names]);
  if (!subset) {
    // Catalog not loaded / operator unknown: plain JSON-or-text inputs.
    return (
      <Stack gap={2}>
        {names.map((name) => (
          <JsonField
            key={name}
            label={name}
            required
            acceptsText
            rows={2}
            value={value[name]}
            onChange={(v) => onChange({ ...value, [name]: v })}
          />
        ))}
      </Stack>
    );
  }
  return <ParamsForm schema={subset} value={value} onChange={onChange} allowXcom={false} collapseOptional={false} />;
}

const NUMBER_FIELDS = new Set([
  "retries",
  "retry_delay_seconds",
  "retry_exponential_backoff",
  "execution_timeout_seconds",
  "pool_slots",
  "priority_weight",
  "max_active_tis_per_dagrun",
]);
const TEXT_FIELDS = new Set(["pool", "queue", "owner", "executor", "trigger_rule", "weight_rule", "task_display_name"]);

/** Input for a required task-level field (typed when the field is known, JSON otherwise). */
function TaskFieldInput({ path, value, onChange }: { path: string; value: unknown; onChange: (v: unknown) => void }) {
  if (path === "code" || path === "doc_md")
    return (
      <CodeField
        label={path}
        required
        field={path}
        language={path === "code" ? "python" : "markdown"}
        value={typeof value === "string" ? value : ""}
        onChange={onChange}
      />
    );
  if (NUMBER_FIELDS.has(path))
    return (
      <NumberField
        label={path}
        required
        field={path}
        min={0}
        value={typeof value === "number" ? value : null}
        onChange={(v) => onChange(v ?? undefined)}
      />
    );
  if (TEXT_FIELDS.has(path))
    return (
      <TextField
        label={path}
        required
        field={path}
        mono
        value={typeof value === "string" ? value : ""}
        onChange={(v) => onChange(v ?? undefined)}
      />
    );
  return <JsonField label={path} required field={path} acceptsText rows={1} value={value} onChange={onChange} />;
}

/** Asks for the required inputs of an operator template, then inserts the task. */
export default function OperatorTemplateInputsDialog({
  template,
  at,
}: {
  template: Template;
  at?: XYPosition | undefined;
}) {
  const { t } = useTranslation("lifecycle");
  const close = useLifecycle((s) => s.closeDialog);
  const node = template.blueprint?.node;
  const required = requiredPaths(template);
  const locked = lockedPaths(template);
  const paramNames = required.filter((p) => p.startsWith("params.")).map((p) => p.slice("params.".length));
  const others = required.filter((p) => !p.startsWith("params."));
  const [params, setParams] = useState<Record<string, unknown>>({});
  const [values, setValues] = useState<Record<string, unknown>>({});
  const inputs: Record<string, unknown> = { ...values };
  for (const name of paramNames) inputs[`params.${name}`] = params[name];
  const missing = required.filter((p) => isEmptyValue(inputs[p]));

  return (
    <DialogShell
      title={t("operatorTemplates.insertTitle", { title: template.title })}
      description={[template.owner_team, template.version ? `v${template.version}` : null, template.description]
        .filter(Boolean)
        .join(" · ")}
      size="md"
      footer={
        <>
          <Button size="sm" variant="ghost" onClick={close}>
            {t("common.cancel")}
          </Button>
          <Button
            size="sm"
            colorPalette="blue"
            disabled={missing.length > 0}
            onClick={() => {
              insertOperatorTemplate(template, at, inputs);
              close();
            }}
          >
            <LuPlus /> {t("operatorTemplates.insert")}
          </Button>
        </>
      }
    >
      <Stack gap={3}>
        <Text fontSize="sm" color="fg.muted">
          {t("operatorTemplates.requiredHelp", { count: required.length })}
        </Text>
        {node?.kind === "operator" && paramNames.length ? (
          <RequiredParams operator={node.operator} names={paramNames} value={params} onChange={setParams} />
        ) : null}
        {node?.kind !== "operator" && paramNames.length ? (
          <Stack gap={2}>
            {paramNames.map((name) => (
              <JsonField
                key={name}
                label={name}
                required
                acceptsText
                rows={2}
                value={params[name]}
                onChange={(v) => setParams((p) => ({ ...p, [name]: v }))}
              />
            ))}
          </Stack>
        ) : null}
        {others.map((path) => (
          <TaskFieldInput
            key={path}
            path={path}
            value={values[path]}
            onChange={(v) => setValues((cur) => ({ ...cur, [path]: v }))}
          />
        ))}
        {locked.length && node ? (
          <Stack gap={1} borderWidth="1px" rounded="md" p={2} bg="bg.subtle">
            <HStack gap={1}>
              <LuLock size={12} />
              <Text fontSize="xs" fontWeight="semibold">
                {t("operatorTemplates.lockedTitle")}
              </Text>
            </HStack>
            {locked.map((path) => (
              <HStack key={path} gap={2} fontSize="xs" minW={0}>
                <Badge size="xs" variant="outline" fontFamily="mono" flexShrink={0}>
                  {path}
                </Badge>
                <Text fontFamily="mono" color="fg.muted" truncate>
                  {short(getPath(node, path))}
                </Text>
              </HStack>
            ))}
          </Stack>
        ) : null}
      </Stack>
    </DialogShell>
  );
}
