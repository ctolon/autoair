import { Alert, Box, Button, Checkbox, Code, Stack, Text } from "@chakra-ui/react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { LuFileCode, LuRefreshCw, LuShieldCheck, LuTriangleAlert } from "react-icons/lu";

import type { DagSpec, SourcePlan } from "../../api/gen/types.gen";
import { ejectedSpec } from "../../codemode/rawDoc";
import { useAccess } from "../../lib/access";
import { withPolicy } from "../../lib/sourcePolicy";
import { useBuilder } from "../../store/builder";
import { deploy } from "../actions";
import { DialogShell } from "../components/DialogShell";
import { KEPT_STATES, SourcePlanSummary, SourcePreview, planOptions, planState } from "../components/SourcePlan";
import { useRetryToken } from "../lib/retryToken";
import { type DeployOptions, useLifecycle } from "../store";

/**
 * A deploy the source policy stopped: generating code would replace a DAG file AutoAir does
 * not regenerate on its own - the hand-written file it keeps, a file changed outside AutoAir,
 * or one it did not create (blocked: apply the change to that file, overwrite it, or keep it
 * as raw code) - or the change cannot
 * be applied to the existing hand-written file (refused: keep it as raw code, or switch the
 * DAG to generated code deliberately). Nothing is regenerated without an explicit choice.
 */
export default function SourcePlanDialog({
  plan,
  request,
  spec,
}: {
  plan: SourcePlan;
  request: DeployOptions;
  spec: DagSpec;
}) {
  const { t } = useTranslation("lifecycle");
  const close = useLifecycle((s) => s.closeDialog);
  const [busy, setBusy] = useState(false);
  const [confirmOverwrite, setConfirmOverwrite] = useState(false);
  const retry = useRetryToken();
  const access = useAccess();
  const offered = planOptions(plan);
  const blocked = plan.action === "blocked";
  const state = planState(plan);
  const byState = blocked && KEPT_STATES.has(state);
  // The server offers these by its own rules; the UI also follows the user's permissions as
  // /api/meta reports them (the server still refuses with 403).
  const rawAvailable = access.feature("code_editor") && access.feature("python_code");
  const canRaw = access.can("code:raw") && access.can("spec:write");
  const rawDenied = !access.can("code:raw") ? "code:raw" : "spec:write";
  // Hidden unless the server allows it at all (preserve.allow_force_overwrite).
  const canOverwrite = access.forceOverwrite;
  const rawCode = typeof plan.current_code === "string" && plan.current_code ? plan.current_code : null;

  const redeploy = async (resolution: "preserve" | "overwrite") => {
    const secret = retry.take();
    if (!secret) return;
    setBusy(true);
    close();
    // Bound to the file this dialog describes: a later change stops the deploy again.
    await deploy({ ...request, spec, sourceResolution: resolution, sourceTargetSha: plan.target_sha ?? null }, secret);
    setBusy(false);
  };
  const keepRaw = () => {
    if (!rawCode || !canRaw) return;
    useBuilder.getState().replaceSpec(ejectedSpec(spec, rawCode));
    close();
  };
  const switchToGenerate = () => {
    const st = useBuilder.getState();
    st.setDag({ source: withPolicy(st.dag, "generate") });
    close();
    useLifecycle.getState().openDialog({ kind: "deploy", focus: "message" });
  };

  return (
    <DialogShell
      title={
        byState ? t(`source.blockedTitleBy.${state}`) : blocked ? t("source.blockedTitle") : t("source.refusedTitle")
      }
      size="lg"
      footer={
        <>
          <Button size="sm" variant="ghost" onClick={close}>
            {t("common.cancel")}
          </Button>
          {offered.has("raw") && rawCode && rawAvailable ? (
            <Button
              size="sm"
              variant="outline"
              onClick={keepRaw}
              disabled={!canRaw}
              title={canRaw ? undefined : t("access.denied", { permission: rawDenied })}
              data-testid="source-keep-raw"
            >
              <LuFileCode /> {t("source.keepRaw")}
            </Button>
          ) : null}
          {!blocked && offered.has("generate") ? (
            <Button size="sm" variant="outline" onClick={switchToGenerate} data-testid="source-generate">
              <LuRefreshCw /> {t("source.switchGenerate")}
            </Button>
          ) : null}
          {blocked && offered.has("overwrite") && canOverwrite ? (
            <Button
              size="sm"
              colorPalette="red"
              variant="outline"
              loading={busy}
              disabled={!confirmOverwrite}
              onClick={() => void redeploy("overwrite")}
              data-testid="source-overwrite"
            >
              <LuTriangleAlert /> {t("source.overwrite")}
            </Button>
          ) : null}
          {blocked && offered.has("preserve") ? (
            <Button
              size="sm"
              colorPalette="blue"
              loading={busy}
              onClick={() => void redeploy("preserve")}
              data-testid="source-apply-preserve"
            >
              <LuShieldCheck /> {t("source.applyPreserve")}
            </Button>
          ) : null}
        </>
      }
    >
      <Stack gap={3}>
        <Alert.Root status="warning" size="sm">
          <Alert.Indicator />
          <Alert.Content>
            <Alert.Description fontSize="sm">
              {byState
                ? t(`source.blockedHelpBy.${state}`)
                : blocked
                  ? t("source.blockedHelp")
                  : t("source.refusedHelp")}
            </Alert.Description>
          </Alert.Content>
        </Alert.Root>
        {plan.target_file ? (
          <Code display="block" p={2} fontSize="xs" whiteSpace="pre-wrap" wordBreak="break-all">
            {plan.target_file}
          </Code>
        ) : null}
        <SourcePlanSummary plan={plan} showDiff={false} showState={false} />
        {blocked && offered.has("preserve") ? (
          <Box>
            <Text fontSize="sm" fontWeight="semibold" mb={1}>
              {t("source.preservePreview")}
            </Text>
            <SourcePreview
              spec={spec}
              environment={request.environment ?? null}
              resolution="preserve"
              showState={false}
            />
          </Box>
        ) : null}
        {blocked && offered.has("overwrite") && canOverwrite ? (
          <Checkbox.Root
            size="sm"
            checked={confirmOverwrite}
            onCheckedChange={(e) => setConfirmOverwrite(e.checked === true)}
            data-testid="source-confirm-overwrite"
          >
            <Checkbox.HiddenInput />
            <Checkbox.Control />
            <Checkbox.Label>
              {state === "hand_written" ? t("source.confirmOverwriteHandWritten") : t("source.confirmOverwrite")}
            </Checkbox.Label>
          </Checkbox.Root>
        ) : null}
        <Box>{retry.field}</Box>
      </Stack>
    </DialogShell>
  );
}
