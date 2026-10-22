import {
  Alert,
  Button,
  Field,
  HStack,
  Input,
  NativeSelect,
  Separator,
  SimpleGrid,
  Stack,
  Switch,
  Text,
} from "@chakra-ui/react";
import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { LuEye, LuHistory } from "react-icons/lu";

import { createBackfillMutation } from "../../api/gen/@tanstack/react-query.gen";
import type { BackfillRequest } from "../../api/gen/types.gen";
import { errorMessage } from "../../api/setup";
import { useBuilder } from "../../store/builder";
import { toaster } from "../../ui/toaster";
import { JsonField } from "../../widgets/fields";
import { DialogShell } from "../components/DialogShell";
import { ResultPreview } from "../components/ResultPreview";
import { localInputToIso, toLocalInput } from "../lib/dates";
import { useAirflowCapabilities } from "../capabilities";
import { LatestVersionSelect } from "../components/LatestVersionSelect";
import { useLifecycle } from "../store";

type Reprocess = NonNullable<BackfillRequest["reprocess_behavior"]>;

/** Airflow 3 backfill with a dry-run preview. */
export default function BackfillDialog() {
  const { t, i18n } = useTranslation("lifecycle");
  const dagId = useBuilder((s) => s.savedDagId);
  const close = useLifecycle((s) => s.closeDialog);
  const [from, setFrom] = useState(() => toLocalInput(new Date(Date.now() - 7 * 86_400_000)));
  const [to, setTo] = useState(() => toLocalInput(new Date()));
  const [reprocess, setReprocess] = useState<Reprocess>("none");
  const [maxActive, setMaxActive] = useState(10);
  const [backwards, setBackwards] = useState(false);
  const [conf, setConf] = useState<Record<string, unknown> | undefined>(undefined);
  const [confValid, setConfValid] = useState(true);
  const caps = useAirflowCapabilities();
  const [latest, setLatest] = useState<boolean | null>(null);
  const [drain, setDrain] = useState(false);
  const preview = useMutation({
    ...createBackfillMutation(),
    onError: (e) => toaster.create({ type: "error", title: t("backfill.failed"), description: errorMessage(e) }),
  });
  const create = useMutation({
    ...createBackfillMutation(),
    onSuccess: () => {
      toaster.create({ type: "success", title: t("backfill.created") });
      useLifecycle.getState().setBottomTab("runs");
      close();
    },
    onError: (e) => toaster.create({ type: "error", title: t("backfill.failed"), description: errorMessage(e) }),
  });

  if (!dagId) {
    return (
      <DialogShell title={t("backfill.title")} size="sm">
        <Text fontSize="sm">{t("common.deployFirst")}</Text>
      </DialogShell>
    );
  }
  const fromIso = localInputToIso(from);
  const toIso = localInputToIso(to);
  const valid = !!fromIso && !!toIso && fromIso <= toIso && maxActive >= 1 && confValid;
  const body = (dry_run: boolean): BackfillRequest => ({
    from_date: fromIso ?? "",
    to_date: toIso ?? "",
    reprocess_behavior: reprocess,
    max_active_runs: maxActive,
    run_backwards: backwards,
    dry_run,
    dag_run_conf: conf ?? null,
    // Only fields the connected Airflow knows (strict request bodies).
    ...(caps.backfill_run_on_latest_version && latest !== null ? { run_on_latest_version: latest } : {}),
    ...(caps.scheduling_state && drain ? { drain_dag: true } : {}),
  });

  return (
    <DialogShell
      title={t("backfill.title")}
      description={t("backfill.description", { dagId })}
      size="lg"
      footer={
        <>
          <Button
            size="sm"
            variant="outline"
            disabled={!valid}
            loading={preview.isPending}
            onClick={() => preview.mutate({ path: { dag_id: dagId }, body: body(true) })}
          >
            <LuEye /> {t("backfill.dryRun")}
          </Button>
          <Button
            size="sm"
            colorPalette="blue"
            disabled={!valid || !preview.data}
            title={!preview.data ? t("backfill.previewFirst") : undefined}
            loading={create.isPending}
            onClick={() => create.mutate({ path: { dag_id: dagId }, body: body(false) })}
          >
            <LuHistory /> {t("backfill.create")}
          </Button>
        </>
      }
    >
      <Stack gap={4}>
        <SimpleGrid columns={{ base: 1, md: 2 }} gap={3}>
          <Field.Root invalid={!fromIso}>
            <Field.Label>{t("backfill.from")}</Field.Label>
            <Input
              size="sm"
              type="datetime-local"
              lang={i18n.language}
              value={from}
              onChange={(e) => setFrom(e.target.value)}
            />
          </Field.Root>
          <Field.Root invalid={!toIso || (!!fromIso && fromIso > toIso)}>
            <Field.Label>{t("backfill.to")}</Field.Label>
            <Input
              size="sm"
              type="datetime-local"
              lang={i18n.language}
              value={to}
              onChange={(e) => setTo(e.target.value)}
            />
            {fromIso && toIso && fromIso > toIso ? <Field.ErrorText>{t("backfill.rangeError")}</Field.ErrorText> : null}
          </Field.Root>
          <Field.Root>
            <Field.Label>{t("backfill.reprocess")}</Field.Label>
            <NativeSelect.Root size="sm">
              <NativeSelect.Field value={reprocess} onChange={(e) => setReprocess(e.target.value as Reprocess)}>
                {(["none", "failed", "completed"] as const).map((r) => (
                  <option key={r} value={r}>
                    {t(`backfill.reprocessOptions.${r}`)}
                  </option>
                ))}
              </NativeSelect.Field>
              <NativeSelect.Indicator />
            </NativeSelect.Root>
          </Field.Root>
          <Field.Root invalid={maxActive < 1}>
            <Field.Label>{t("backfill.maxActive")}</Field.Label>
            <Input
              size="sm"
              type="number"
              min={1}
              value={maxActive}
              onChange={(e) => setMaxActive(Number(e.target.value))}
            />
          </Field.Root>
        </SimpleGrid>
        <Switch.Root size="sm" checked={backwards} onCheckedChange={(e) => setBackwards(e.checked)}>
          <Switch.HiddenInput />
          <Switch.Control />
          <Switch.Label>{t("backfill.backwards")}</Switch.Label>
        </Switch.Root>
        {caps.backfill_run_on_latest_version ? <LatestVersionSelect value={latest} onChange={setLatest} /> : null}
        {caps.scheduling_state ? (
          <Stack gap={0.5}>
            <Switch.Root size="sm" checked={drain} onCheckedChange={(e) => setDrain(e.checked)}>
              <Switch.HiddenInput />
              <Switch.Control />
              <Switch.Label>{t("backfill.drain")}</Switch.Label>
            </Switch.Root>
            <Text fontSize="xs" color="fg.muted" pl={9}>
              {t("backfill.drainHelp")}
            </Text>
          </Stack>
        ) : null}
        <JsonField
          help={t("backfill.confHelp")}
          label={t("backfill.conf")}
          value={conf}
          validate={(v) => (v && typeof v === "object" && !Array.isArray(v) ? undefined : t("common.mustBeObject"))}
          onChange={(v) => setConf(v as Record<string, unknown> | undefined)}
          onValidityChange={setConfValid}
        />
        {preview.data ? (
          <>
            <Separator />
            <HStack>
              <Text fontWeight="semibold" fontSize="sm">
                {t("backfill.previewTitle")}
              </Text>
            </HStack>
            <ResultPreview result={preview.data} columns={["logical_date", "partition_key"]} />
          </>
        ) : (
          <Alert.Root status="info" size="sm">
            <Alert.Indicator />
            <Alert.Description fontSize="xs">{t("backfill.previewHint")}</Alert.Description>
          </Alert.Root>
        )}
      </Stack>
    </DialogShell>
  );
}
