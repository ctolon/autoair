import { Button, Field, HStack, Input, RadioGroup, Stack, Switch, Text, Textarea } from "@chakra-ui/react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { LuPlay } from "react-icons/lu";

import { listDagRunsQueryKey, triggerDagMutation } from "../../api/gen/@tanstack/react-query.gen";
import { errorMessage } from "../../api/setup";
import { useBuilder } from "../../store/builder";
import { toaster } from "../../ui/toaster";
import { JsonField } from "../../widgets/fields";
import { SchemaForm } from "../../widgets/SchemaForm";
import { DialogShell } from "../components/DialogShell";
import { localInputToIso, toLocalInput } from "../lib/dates";
import { paramDefaults, paramsToJsonSchema } from "../lib/params";
import { useLifecycle } from "../store";

type DateMode = "now" | "specific" | "none";

/** Trigger a DAG run: params form (from the DAG params) or raw conf, logical date, note… */
export default function TriggerDialog() {
  const { t, i18n } = useTranslation("lifecycle");
  const qc = useQueryClient();
  const dagId = useBuilder((s) => s.savedDagId);
  const params = useBuilder((s) => s.dag.params);
  const env = useBuilder((s) => s.environment);
  const envDefaults = useBuilder((s) => {
    const overlay = env ? s.dag.environments?.[env] : undefined;
    return overlay && typeof overlay === "object" && "params_defaults" in overlay
      ? (overlay as { params_defaults?: Record<string, unknown> }).params_defaults
      : undefined;
  });
  const schema = useMemo(() => paramsToJsonSchema(params), [params]);
  const hasParams = Object.keys(schema.properties ?? {}).length > 0;
  const [conf, setConf] = useState<Record<string, unknown>>(() => ({ ...paramDefaults(params), ...envDefaults }));
  const [raw, setRaw] = useState(!hasParams);
  const [dateMode, setDateMode] = useState<DateMode>("now");
  const [date, setDate] = useState(toLocalInput(new Date()));
  const [partitionKey, setPartitionKey] = useState("");
  const [note, setNote] = useState("");
  const [unpause, setUnpause] = useState(true);
  const [confValid, setConfValid] = useState(true);
  const confInvalid = raw && !confValid;
  const close = useLifecycle((s) => s.closeDialog);

  const trigger = useMutation({
    ...triggerDagMutation(),
    onSuccess: async (r) => {
      toaster.create({ type: "success", title: t("trigger.done"), description: r.dag_run_id ?? "" });
      if (dagId) await qc.invalidateQueries({ queryKey: listDagRunsQueryKey({ path: { dag_id: dagId } }) });
      const lc = useLifecycle.getState();
      if (r.dag_run_id) lc.setSelectedRunId(r.dag_run_id);
      lc.setBottomTab("runs");
      close();
    },
    onError: (e) => toaster.create({ type: "error", title: t("trigger.failed"), description: errorMessage(e) }),
  });

  if (!dagId) {
    return (
      <DialogShell title={t("trigger.title")} size="sm">
        <Text fontSize="sm">{t("common.deployFirst")}</Text>
      </DialogShell>
    );
  }

  const logicalDate = dateMode === "now" ? "now" : dateMode === "specific" ? localInputToIso(date) : null;
  const submit = () =>
    trigger.mutate({
      path: { dag_id: dagId },
      body: {
        conf,
        unpause,
        logical_date: logicalDate,
        partition_key: partitionKey.trim() || null,
        note: note.trim() || null,
      },
    });

  return (
    <DialogShell
      title={t("trigger.title")}
      description={t("trigger.description", { dagId })}
      size="lg"
      footer={
        <>
          <Button size="sm" variant="ghost" onClick={close}>
            {t("common.cancel")}
          </Button>
          <Button
            size="sm"
            colorPalette="green"
            loading={trigger.isPending}
            disabled={(dateMode === "specific" && !logicalDate) || confInvalid}
            onClick={submit}
          >
            <LuPlay /> {t("trigger.button")}
          </Button>
        </>
      }
    >
      <Stack gap={4}>
        <HStack justify="space-between">
          <Text fontWeight="semibold" fontSize="sm">
            {t("trigger.conf")}
          </Text>
          {hasParams ? (
            <Switch.Root
              size="sm"
              checked={raw}
              onCheckedChange={(e) => {
                setRaw(e.checked);
                setConfValid(true); // the form edits the last valid conf
              }}
            >
              <Switch.HiddenInput />
              <Switch.Control />
              <Switch.Label>{t("trigger.rawJson")}</Switch.Label>
            </Switch.Root>
          ) : null}
        </HStack>
        {raw ? (
          <JsonField
            help={t("trigger.confJsonHelp")}
            label={t("trigger.confJson")}
            rows={8}
            value={conf}
            validate={(v) => (v && typeof v === "object" && !Array.isArray(v) ? undefined : t("common.mustBeObject"))}
            onChange={(v) => setConf((v as Record<string, unknown> | undefined) ?? {})}
            onValidityChange={(valid) => setConfValid(valid)}
          />
        ) : (
          <SchemaForm schema={schema} value={conf} onChange={setConf} />
        )}

        <Field.Root>
          <Field.Label>{t("trigger.logicalDate")}</Field.Label>
          <RadioGroup.Root
            size="sm"
            value={dateMode}
            onValueChange={(e) => setDateMode((e.value as DateMode) ?? "now")}
          >
            <HStack gap={4} wrap="wrap">
              {(["now", "specific", "none"] as const).map((m) => (
                <RadioGroup.Item key={m} value={m}>
                  <RadioGroup.ItemHiddenInput />
                  <RadioGroup.ItemIndicator />
                  <RadioGroup.ItemText>{t(`trigger.date.${m}`)}</RadioGroup.ItemText>
                </RadioGroup.Item>
              ))}
            </HStack>
          </RadioGroup.Root>
          {dateMode === "specific" ? (
            <Input
              mt={2}
              size="sm"
              type="datetime-local"
              lang={i18n.language}
              value={date}
              aria-label={t("trigger.logicalDate")}
              onChange={(e) => setDate(e.target.value)}
            />
          ) : null}
          <Field.HelperText>{t(`trigger.dateHelp.${dateMode}`)}</Field.HelperText>
        </Field.Root>

        <Field.Root>
          <Field.Label>{t("trigger.partitionKey")}</Field.Label>
          <Input size="sm" value={partitionKey} onChange={(e) => setPartitionKey(e.target.value)} />
          <Field.HelperText>{t("trigger.partitionHelp")}</Field.HelperText>
        </Field.Root>
        <Field.Root>
          <Field.Label>{t("trigger.note")}</Field.Label>
          <Textarea size="sm" rows={2} value={note} onChange={(e) => setNote(e.target.value)} />
        </Field.Root>
        <Switch.Root size="sm" checked={unpause} onCheckedChange={(e) => setUnpause(e.checked)}>
          <Switch.HiddenInput />
          <Switch.Control />
          <Switch.Label>{t("trigger.unpause")}</Switch.Label>
        </Switch.Root>
      </Stack>
    </DialogShell>
  );
}
