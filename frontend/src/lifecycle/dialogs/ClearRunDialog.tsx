import { Button, Separator, Stack, Switch, Text } from "@chakra-ui/react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { LuEraser, LuEye } from "react-icons/lu";

import {
  clearDagRunMutation,
  getRunOverlayQueryKey,
  listDagRunsQueryKey,
} from "../../api/gen/@tanstack/react-query.gen";
import { errorMessage } from "../../api/setup";
import { useBuilder } from "../../store/builder";
import { toaster } from "../../ui/toaster";
import { DialogShell } from "../components/DialogShell";
import { ResultPreview } from "../components/ResultPreview";
import { useAirflowCapabilities } from "../capabilities";
import { LatestVersionSelect } from "../components/LatestVersionSelect";
import { useLifecycle } from "../store";

function Toggle({
  label,
  help,
  checked,
  disabled,
  onChange,
}: {
  label: string;
  help?: string;
  checked: boolean;
  disabled?: boolean;
  onChange: (v: boolean) => void;
}) {
  return (
    <Stack gap={0.5}>
      <Switch.Root
        size="sm"
        checked={checked}
        disabled={disabled ?? false}
        onCheckedChange={(e) => onChange(e.checked)}
      >
        <Switch.HiddenInput />
        <Switch.Control />
        <Switch.Label>{label}</Switch.Label>
      </Switch.Root>
      {help ? (
        <Text fontSize="xs" color="fg.muted" pl={9}>
          {help}
        </Text>
      ) : null}
    </Stack>
  );
}

/** Clear (re-run) task instances of a DAG run, with a dry-run preview. */
export default function ClearRunDialog({ runId }: { runId: string }) {
  const { t } = useTranslation("lifecycle");
  const qc = useQueryClient();
  const dagId = useBuilder((s) => s.savedDagId) ?? "";
  const close = useLifecycle((s) => s.closeDialog);
  const caps = useAirflowCapabilities();
  const [onlyFailed, setOnlyFailed] = useState(true);
  const [onlyNew, setOnlyNew] = useState(false);
  const [keepState, setKeepState] = useState(false);
  const [latest, setLatest] = useState<boolean | null>(null);
  const preview = useMutation({
    ...clearDagRunMutation(),
    onError: (e) => toaster.create({ type: "error", title: t("clear.failed"), description: errorMessage(e) }),
  });
  const clear = useMutation({
    ...clearDagRunMutation(),
    onSuccess: async () => {
      toaster.create({ type: "success", title: t("clear.done", { runId }) });
      await Promise.all([
        qc.invalidateQueries({ queryKey: listDagRunsQueryKey({ path: { dag_id: dagId } }) }),
        qc.invalidateQueries({ queryKey: getRunOverlayQueryKey({ path: { dag_id: dagId, run_id: runId } }) }),
      ]);
      close();
    },
    onError: (e) => toaster.create({ type: "error", title: t("clear.failed"), description: errorMessage(e) }),
  });
  const vars = (dry_run: boolean) => ({
    path: { dag_id: dagId, run_id: runId },
    // Options the connected Airflow lacks are never sent (strict request bodies).
    body: {
      only_failed: onlyFailed,
      dry_run,
      ...(caps.clear_only_new && onlyNew ? { only_new: true } : {}),
      ...(caps.clear_run_on_latest_version && latest !== null ? { run_on_latest_version: latest } : {}),
      ...(caps.keep_task_state && keepState ? { keep_task_state: true } : {}),
    },
  });
  const update = (fn: () => void) => {
    fn();
    preview.reset();
  };
  return (
    <DialogShell
      title={t("clear.title")}
      description={runId}
      size="md"
      footer={
        <>
          <Button size="sm" variant="outline" loading={preview.isPending} onClick={() => preview.mutate(vars(true))}>
            <LuEye /> {t("clear.dryRun")}
          </Button>
          <Button size="sm" colorPalette="orange" loading={clear.isPending} onClick={() => clear.mutate(vars(false))}>
            <LuEraser /> {t("clear.button")}
          </Button>
        </>
      }
    >
      <Stack gap={3}>
        <Toggle
          label={t("clear.onlyFailed")}
          checked={onlyFailed}
          onChange={(v) =>
            update(() => {
              setOnlyFailed(v);
              if (v) setOnlyNew(false);
            })
          }
        />
        {caps.clear_only_new ? (
          <Toggle
            label={t("clear.onlyNew")}
            help={t("clear.onlyNewHelp")}
            checked={onlyNew}
            onChange={(v) =>
              update(() => {
                setOnlyNew(v);
                if (v) {
                  setOnlyFailed(false);
                  setKeepState(false);
                }
              })
            }
          />
        ) : null}
        {caps.keep_task_state ? (
          <Toggle
            label={t("clear.keepTaskState")}
            help={t("clear.keepTaskStateHelp")}
            checked={keepState}
            onChange={(v) =>
              update(() => {
                setKeepState(v);
                if (v) setOnlyNew(false);
              })
            }
          />
        ) : null}
        {caps.clear_run_on_latest_version ? (
          <LatestVersionSelect value={latest} onChange={(v) => update(() => setLatest(v))} />
        ) : null}
        <Text fontSize="xs" color="fg.muted">
          {t("clear.help")}
        </Text>
        {preview.data ? (
          <>
            <Separator />
            <ResultPreview result={preview.data} columns={["task_id", "map_index", "state"]} />
          </>
        ) : null}
      </Stack>
    </DialogShell>
  );
}
