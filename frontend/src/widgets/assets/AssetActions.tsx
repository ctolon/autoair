import { Box, Button, HStack, Stack } from "@chakra-ui/react";
import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { LuSend, LuZap } from "react-icons/lu";

import { createAssetEventMutation, materializeAssetMutation } from "../../api/gen/@tanstack/react-query.gen";
import { errorMessage } from "../../api/setup";
import { useAccess } from "../../lib/access";
import { useAirflowCapabilities } from "../../lifecycle/capabilities";
import { toaster } from "../../ui/toaster";
import { Hint, JsonField } from "../fields";

const isObject = (v: unknown): v is Record<string, unknown> => !!v && typeof v === "object" && !Array.isArray(v);

/**
 * Actions on an asset Airflow already knows: send a test event (downstream asset-scheduled
 * DAGs react to it) and, when the Airflow version has it, materialize (run the producing DAG).
 * Needs the `runs` feature and the `run:trigger` permission; Airflow applies its own RBAC.
 */
export function AssetActions({ assetId, name }: { assetId: number; name: string }) {
  const { t } = useTranslation("inspector");
  const access = useAccess();
  const caps = useAirflowCapabilities();
  const [editing, setEditing] = useState(false);
  const [extra, setExtra] = useState<unknown>({});
  const send = useMutation({
    ...createAssetEventMutation(),
    onSuccess: () => {
      toaster.create({ type: "success", title: t("assets.eventSent", { name }) });
      setEditing(false);
    },
    onError: (e) => toaster.create({ type: "error", title: t("assets.eventFailed"), description: errorMessage(e) }),
  });
  const materialize = useMutation({
    ...materializeAssetMutation(),
    onSuccess: (r) =>
      toaster.create({
        type: "success",
        title: t("assets.materialized", { name }),
        description: typeof r.dag_run_id === "string" ? r.dag_run_id : undefined,
      }),
    onError: (e) =>
      toaster.create({ type: "error", title: t("assets.materializeFailed"), description: errorMessage(e) }),
  });
  if (!access.feature("runs")) return null;
  const allowed = access.can("run:trigger");
  const denied = allowed ? undefined : t("assets.denied");
  return (
    <Stack gap={1} data-testid="asset-actions">
      <HStack gap={1} wrap="wrap">
        <Button size="2xs" variant="outline" disabled={!allowed} title={denied} onClick={() => setEditing((v) => !v)}>
          <LuSend /> {t("assets.sendEvent")}
        </Button>
        {caps.materialize ? (
          <Button
            size="2xs"
            variant="outline"
            disabled={!allowed}
            title={denied ?? t("assets.materializeHelp")}
            loading={materialize.isPending}
            onClick={() => materialize.mutate({ path: { asset_id: assetId } })}
          >
            <LuZap /> {t("assets.materialize")}
          </Button>
        ) : null}
      </HStack>
      {editing && allowed ? (
        <Box borderWidth="1px" rounded="md" p={2}>
          <JsonField
            label={t("assets.eventExtra")}
            help={t("assets.eventExtraHelp")}
            rows={3}
            value={extra}
            validate={(v) => (isObject(v) ? undefined : t("assets.mustBeObject"))}
            onChange={(v) => setExtra(v ?? {})}
          />
          <HStack justify="end" mt={1}>
            <Button
              size="2xs"
              colorPalette="blue"
              disabled={!isObject(extra)}
              loading={send.isPending}
              onClick={() => isObject(extra) && send.mutate({ path: { asset_id: assetId }, body: { extra } })}
            >
              <LuSend /> {t("assets.send")}
            </Button>
          </HStack>
          <Hint>{t("assets.sendEventHelp")}</Hint>
        </Box>
      ) : null}
    </Stack>
  );
}
