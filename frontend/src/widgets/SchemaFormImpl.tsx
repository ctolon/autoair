import { Box, Text } from "@chakra-ui/react";
import Form from "@rjsf/chakra-ui";
import type {
  DescriptionFieldProps,
  FieldProps,
  RJSFSchema,
  RegistryFieldsType,
  RegistryWidgetsType,
  TemplatesType,
  WidgetProps,
} from "@rjsf/utils";
import validator from "@rjsf/validator-ajv8";
import { useMemo } from "react";

import { type JsonSchema, type UiSchema, compactParams, toFormSchema } from "../lib/formSchema";
import { DATALIST } from "./AirflowPickers";
import { CodeField, JsonField, TextField } from "./fields";

function datalistWidget(list: string) {
  return function DatalistWidget(props: WidgetProps) {
    return (
      <TextField
        label={props.label}
        required={props.required}
        value={typeof props.value === "string" ? props.value : ""}
        placeholder={props.placeholder}
        list={list}
        onChange={(v) => props.onChange(v ?? undefined)}
      />
    );
  };
}

function CodeWidget(props: WidgetProps) {
  return (
    <CodeField
      label={props.label}
      required={props.required}
      value={typeof props.value === "string" ? props.value : ""}
      placeholder={props.placeholder}
      rows={5}
      onChange={(v) => props.onChange(v === "" ? undefined : v)}
    />
  );
}

function JsonFieldAdapter(props: FieldProps) {
  return (
    <JsonField
      label={props.schema.title ?? props.name}
      help={props.schema.description}
      value={props.formData}
      onChange={(v) => props.onChange(v, [])}
    />
  );
}

/** Compact helper-text style descriptions (the theme default renders large <sup> text). */
function Description({ description, id }: DescriptionFieldProps) {
  if (!description) return null;
  return (
    <Text id={id} as="span" display="block" fontSize="xs" fontWeight="normal" color="fg.muted" lineHeight="short">
      {description}
    </Text>
  );
}

const templates: Partial<TemplatesType> = { DescriptionFieldTemplate: Description };

const widgets: RegistryWidgetsType = {
  connection: datalistWidget(DATALIST.connection),
  pool: datalistWidget(DATALIST.pool),
  variable: datalistWidget(DATALIST.variable),
  code: CodeWidget,
};
const fields: RegistryFieldsType = { json: JsonFieldAdapter };

export type SchemaFormProps = {
  schema: JsonSchema;
  value: Record<string, unknown>;
  onChange: (v: Record<string, unknown>) => void;
  /**
   * Use the schema as-is (no catalog normalisation: defaults stay defaults) — e.g. the DAG
   * trigger form preview. Catalog schemas (operator / extension params) are normalised.
   */
  raw?: boolean;
  uiSchema?: UiSchema;
  /** Run client-side JSON-Schema validation and show errors live. */
  validate?: boolean;
  disabled?: boolean;
};

/**
 * Renders a JSON-Schema form (extension config, trigger form preview) with Chakra.
 * Client-side validation is off by default: templated strings ("{{ ... }}") are valid for
 * any field type, so the backend (Pydantic + catalog) is the source of truth.
 */
export default function SchemaFormImpl({
  schema,
  value,
  onChange,
  raw,
  uiSchema,
  validate,
  disabled,
}: SchemaFormProps) {
  const form = useMemo(() => (raw ? { schema, uiSchema: {} } : toFormSchema(schema)), [schema, raw]);
  if (!Object.keys(form.schema.properties ?? {}).length) return null;
  return (
    <Box className="autoair-rjsf" fontSize="sm">
      <Form
        // Our JsonSchema is a structural subset of JSON Schema 7 (plus x-autoair hints).
        schema={form.schema as RJSFSchema}
        uiSchema={{ ...form.uiSchema, ...uiSchema, "ui:submitButtonOptions": { norender: true } }}
        formData={value}
        validator={validator}
        widgets={widgets}
        fields={fields}
        templates={templates}
        noValidate={!validate}
        liveValidate={validate ? "onChange" : false}
        showErrorList={false}
        noHtml5Validate
        disabled={disabled ?? false}
        liveOmit={false}
        experimental_defaultFormStateBehavior={{ emptyObjectFields: "skipDefaults" }}
        onChange={(e) =>
          onChange(raw ? (e.formData as Record<string, unknown>) : compactParams(e.formData as Record<string, unknown>))
        }
      />
    </Box>
  );
}
