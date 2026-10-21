import { Button, Field, HStack, Input, SimpleGrid, Stack, Text, Textarea } from "@chakra-ui/react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { LuCheck, LuChevronDown, LuChevronRight, LuPlugZap, LuPlus, LuTriangleAlert } from "react-icons/lu";

import {
  createConnectionMutation,
  listConnectionHookMetaOptions,
  listConnectionTypesOptions,
  testConnectionMutation,
} from "../api/gen/@tanstack/react-query.gen";
import type { ConnectionDraft, ConnectionHookMeta } from "../api/gen/types.gen";
import { errorMessage } from "../api/setup";
import { DialogShell } from "../lifecycle/components/DialogShell";
import { Autocomplete } from "./Autocomplete";
import {
  buildExtra,
  type ExtraInput,
  extraKind,
  extraLabel,
  initialExtras,
  metaFor,
  type StandardField,
  standardField,
} from "./connectionForm";
import { SelectField, SwitchField, TextField } from "./fields";

const CONN_ID = /^[A-Za-z0-9_.-]{1,200}$/;
const LOOK_FIELDS = {
  host: "host",
  port: "port",
  login: "login",
  password: "password",
  schema: "schema",
  description: "description",
} as const satisfies Record<string, StandardField>;

/** Typed inputs of the connection type's `extra` fields (from Airflow's hook metadata). */
function ExtraFields({
  meta,
  values,
  invalid,
  onChange,
}: {
  meta: ConnectionHookMeta;
  values: Record<string, ExtraInput>;
  invalid: string[];
  onChange: (name: string, value: ExtraInput) => void;
}) {
  const { t } = useTranslation("inspector");
  const fields = Object.entries(meta.extra_fields ?? {});
  if (!fields.length) return null;
  return (
    <Stack gap={2} data-testid="connection-extra-fields">
      <Text fontSize="xs" fontWeight="semibold" color="fg.muted">
        {t("newConnection.extraFields", { type: meta.hook_name || meta.connection_type })}
      </Text>
      <SimpleGrid columns={2} gap={3}>
        {fields.map(([name, field]) => {
          const kind = extraKind(field);
          const label = extraLabel(name, field);
          const help = field.description ?? undefined;
          const error = invalid.includes(name) ? t("newConnection.extraFieldInvalid", { kind }) : undefined;
          const value = values[name];
          if (kind === "boolean") {
            return (
              <SwitchField
                key={name}
                label={label}
                help={help}
                checked={value === true}
                onChange={(v) => onChange(name, v)}
              />
            );
          }
          if (kind === "enum") {
            const options = ((field.schema?.enum as unknown[]) ?? []).map(String);
            return (
              <SelectField
                key={name}
                label={label}
                help={help}
                value={typeof value === "string" ? value : ""}
                options={options}
                emptyLabel=""
                onChange={(v) => onChange(name, v ?? "")}
              />
            );
          }
          if (kind === "password" || kind === "multiline" || kind === "json") {
            return (
              <Field.Root key={name} invalid={!!error} gridColumn={kind === "password" ? undefined : "span 2"}>
                <Field.Label fontSize="xs">{label}</Field.Label>
                {kind === "password" ? (
                  <Input
                    size="sm"
                    type="password"
                    autoComplete="new-password"
                    value={typeof value === "string" ? value : ""}
                    onChange={(e) => onChange(name, e.target.value)}
                  />
                ) : (
                  <Textarea
                    size="sm"
                    rows={2}
                    fontFamily={kind === "json" ? "mono" : undefined}
                    value={typeof value === "string" ? value : ""}
                    onChange={(e) => onChange(name, e.target.value)}
                  />
                )}
                {error ? (
                  <Field.ErrorText>{error}</Field.ErrorText>
                ) : help ? (
                  <Field.HelperText fontSize="xs">{help}</Field.HelperText>
                ) : null}
              </Field.Root>
            );
          }
          return (
            <TextField
              key={name}
              label={label}
              help={help}
              error={error}
              dtype={kind === "integer" ? "int" : kind === "number" ? "float" : "str"}
              value={typeof value === "string" ? value : ""}
              onChange={(v) => onChange(name, v ?? "")}
            />
          );
        })}
      </SimpleGrid>
    </Stack>
  );
}

/**
 * Create an Airflow connection without leaving the builder. Credentials go straight to
 * Airflow (with the user's own permissions) and are never stored by AutoAir.
 */
export function NewConnectionDialog({
  connTypes,
  suggestedId,
  onCreated,
  onClose,
}: {
  connTypes: string[];
  suggestedId?: string | undefined;
  onCreated: (connectionId: string) => void;
  onClose: () => void;
}) {
  const { t } = useTranslation("inspector");
  const qc = useQueryClient();
  const types = useQuery({ ...listConnectionTypesOptions(), retry: false, staleTime: 300_000 });
  // Typed form per conn_type; absent (old Airflow, 403…) -> the generic form with raw extra.
  const hookMeta = useQuery({ ...listConnectionHookMetaOptions(), retry: false, staleTime: 300_000 });
  const [draft, setDraft] = useState<ConnectionDraft>({
    connection_id: suggestedId ?? "",
    conn_type: connTypes[0] ?? "",
  });
  const meta = metaFor(hookMeta.data, draft.conn_type);
  const [extras, setExtras] = useState<{ type: string; values: Record<string, ExtraInput> }>({
    type: "",
    values: {},
  });
  // Reset the typed values (to the type's defaults) whenever the conn_type changes.
  const extraValues = extras.type === draft.conn_type ? extras.values : initialExtras(meta);
  const [raw, setRaw] = useState("");
  const [showRaw, setShowRaw] = useState(false);
  const set = (patch: Partial<ConnectionDraft>) => {
    setDraft((d) => ({ ...d, ...patch }));
    test.reset();
  };
  const setExtra = (name: string, value: ExtraInput) => {
    setExtras({ type: draft.conn_type, values: { ...extraValues, [name]: value } });
    test.reset();
  };
  const test = useMutation(testConnectionMutation());
  const create = useMutation({
    ...createConnectionMutation(),
    onSuccess: async (conn) => {
      await qc.invalidateQueries({
        predicate: (q) => (q.queryKey[0] as { _id?: string } | undefined)?._id === "listConnections",
      });
      onCreated(conn.connection_id);
      onClose();
    },
  });
  const idOk = CONN_ID.test(draft.connection_id);
  const built = buildExtra(meta, extraValues, raw);
  const extraOk = !built.rawInvalid && built.invalid.length === 0;
  const valid = idOk && !!draft.conn_type && extraOk;
  const look = (f: StandardField) => standardField(meta, f);
  const body = (): ConnectionDraft =>
    Object.fromEntries(
      Object.entries({ ...draft, extra: built.extra }).filter(
        // Hidden standard fields are not sent (the type does not use them).
        ([k, v]) =>
          v !== "" &&
          v !== null &&
          v !== undefined &&
          !(k in LOOK_FIELDS && look(LOOK_FIELDS[k as keyof typeof LOOK_FIELDS]).hidden),
      ),
    ) as ConnectionDraft;
  const hasTypedExtras = !!meta && Object.keys(meta.extra_fields ?? {}).length > 0;
  const rawOpen = showRaw || !hasTypedExtras;
  const typeOptions = (types.data ?? []).map((ct) => ({
    value: ct.conn_type,
    description: ct.hook_name ?? "",
  }));
  const text = (key: "host" | "login" | "schema" | "description") => ({
    value: draft[key] ?? "",
    onChange: (e: { target: { value: string } }) => set({ [key]: e.target.value }),
  });
  return (
    <DialogShell
      title={t("newConnection.title")}
      description={t("newConnection.help")}
      size="md"
      onClose={onClose}
      footer={
        <HStack w="full" justify="space-between">
          <Button
            size="sm"
            variant="outline"
            disabled={!valid}
            loading={test.isPending}
            onClick={() => test.mutate({ body: body() })}
          >
            <LuPlugZap /> {t("newConnection.test")}
          </Button>
          <Button
            size="sm"
            colorPalette="blue"
            disabled={!valid}
            loading={create.isPending}
            onClick={() => create.mutate({ body: body() })}
          >
            <LuPlus /> {t("newConnection.create")}
          </Button>
        </HStack>
      }
    >
      <Stack gap={3}>
        <SimpleGrid columns={2} gap={3}>
          <Field.Root required invalid={!!draft.connection_id && !idOk}>
            <Field.Label>{t("newConnection.id")}</Field.Label>
            <Input
              size="sm"
              fontFamily="mono"
              autoFocus
              value={draft.connection_id}
              onChange={(e) => set({ connection_id: e.target.value.trim() })}
            />
            <Field.ErrorText>{t("newConnection.idInvalid")}</Field.ErrorText>
          </Field.Root>
          <Autocomplete
            help={t("newConnection.typeHelp")}
            label={t("newConnection.type")}
            required
            mono
            value={draft.conn_type}
            onChange={(v) => set({ conn_type: (v ?? "").trim() })}
            options={typeOptions}
          />
          {(["host", "port", "login", "password", "schema", "description"] as const).map((f) => {
            const l = look(f);
            if (l.hidden) return null;
            return (
              <Field.Root key={f}>
                <Field.Label>{l.label ?? t(`newConnection.${f}`)}</Field.Label>
                {f === "port" ? (
                  <Input
                    size="sm"
                    type="number"
                    placeholder={l.placeholder ?? undefined}
                    value={draft.port ?? ""}
                    onChange={(e) => set({ port: e.target.value === "" ? null : Number(e.target.value) })}
                  />
                ) : f === "password" ? (
                  <Input
                    size="sm"
                    type="password"
                    autoComplete="new-password"
                    placeholder={l.placeholder ?? undefined}
                    value={draft.password ?? ""}
                    onChange={(e) => set({ password: e.target.value })}
                  />
                ) : (
                  <Input
                    size="sm"
                    fontFamily={f === "host" ? "mono" : undefined}
                    autoComplete={f === "login" ? "off" : undefined}
                    placeholder={l.placeholder ?? undefined}
                    {...text(f)}
                  />
                )}
              </Field.Root>
            );
          })}
        </SimpleGrid>
        {meta ? <ExtraFields meta={meta} values={extraValues} invalid={built.invalid} onChange={setExtra} /> : null}
        {hasTypedExtras ? (
          <Button size="xs" variant="ghost" alignSelf="start" onClick={() => setShowRaw((v) => !v)}>
            {rawOpen ? <LuChevronDown /> : <LuChevronRight />} {t("newConnection.advancedExtra")}
          </Button>
        ) : null}
        {rawOpen ? (
          <Field.Root invalid={built.rawInvalid}>
            <Field.Label>{t("newConnection.extra")}</Field.Label>
            <Textarea
              size="sm"
              fontFamily="mono"
              rows={3}
              placeholder='{"key": "value"}'
              value={raw}
              onChange={(e) => {
                setRaw(e.target.value);
                test.reset();
              }}
            />
            {built.rawInvalid ? (
              <Field.ErrorText>{t("newConnection.extraInvalid")}</Field.ErrorText>
            ) : hasTypedExtras ? (
              <Field.HelperText fontSize="xs">{t("newConnection.rawExtraHelp")}</Field.HelperText>
            ) : null}
          </Field.Root>
        ) : null}
        {test.data ? (
          <HStack role="status" fontSize="sm" color={test.data.status ? "fg.success" : "fg.error"} alignItems="start">
            {test.data.status ? <LuCheck /> : <LuTriangleAlert />}
            <Text>{test.data.message || t(test.data.status ? "newConnection.ok" : "newConnection.failed")}</Text>
          </HStack>
        ) : null}
        {test.error || create.error ? (
          <Text role="alert" fontSize="sm" color="fg.error">
            {errorMessage(create.error ?? test.error)}
          </Text>
        ) : null}
      </Stack>
    </DialogShell>
  );
}
