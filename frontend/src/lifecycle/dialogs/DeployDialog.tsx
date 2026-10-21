import { Alert, Button, Checkbox, Field, NativeSelect, Stack, Text, Textarea } from "@chakra-ui/react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { LuGitCommitHorizontal, LuGitPullRequestArrow, LuHardDrive, LuRocket } from "react-icons/lu";

import type { SourcePlan } from "../../api/gen/types.gen";
import { type SourcePolicyChoice, policyOf, withPolicy } from "../../lib/sourcePolicy";
import { useBuilder } from "../../store/builder";
import { toSpec } from "../../store/convert";
import { SourcePolicyField } from "../../widgets/SourcePolicyField";
import { deploy } from "../actions";
import { DeployGovernance, type GovernanceValue } from "../components/DeployGovernance";
import { DialogShell } from "../components/DialogShell";
import { GitTokenField } from "../components/GitTokenField";
import { SourcePreview } from "../components/SourcePlan";
import { useEnvironments, useMeta } from "../hooks";
import { deployEffect, suggestCommitMessage } from "../lib/deployEffect";
import { tokenMissing, useGitTokenInfo } from "../lib/gitToken";
import { useLifecycle } from "../store";

/** "Deploy…": environment, message (pre-filled) and what exactly the deploy will do. */
export default function DeployDialog({ focus }: { focus: "message" | "environment" }) {
  const { t } = useTranslation("lifecycle");
  const meta = useMeta();
  const envs = useEnvironments();
  const dagId = useBuilder((s) => s.dag.dag_id);
  const storeEnv = useBuilder((s) => s.environment);
  const [env, setEnv] = useState<string>(storeEnv ?? meta?.default_environment ?? "");
  // Pre-filled with what changed since the deployed version (editable).
  const [message, setMessage] = useState(() => {
    const st = useBuilder.getState();
    return suggestCommitMessage(st.toSpec(), st.baseSpec);
  });
  const [busy, setBusy] = useState(false);
  // The previewed DAG file: the deploy writes only while the target's file is that one.
  const [reviewed, setReviewed] = useState<SourcePlan | null>(null);
  // The document as it would be deployed: the file preview follows edits (e.g. the policy).
  const dagState = useBuilder((s) => s.dag);
  const nodes = useBuilder((s) => s.nodes);
  const edges = useBuilder((s) => s.edges);
  // The source policy chosen here is a draft (like the environment and the message): the
  // preview follows it, Deploy applies it to the document as one undoable step, Cancel or
  // Escape drops it.
  const [policyDraft, setPolicyDraft] = useState<SourcePolicyChoice | null>(null);
  const docPolicy = policyOf(dagState);
  const pendingPolicy = policyDraft !== null && policyDraft !== docPolicy ? policyDraft : null;
  const spec = useMemo(() => {
    const doc = toSpec(dagState, nodes, edges);
    return pendingPolicy !== null ? { ...doc, source: withPolicy(doc, pendingPolicy) } : doc;
  }, [dagState, nodes, edges, pendingPolicy]);
  const rights = meta?.iam?.deploy;
  // Users allowed to bypass the pull request (deploy:direct_push) may push straight to the branch.
  const canDirectPush = rights?.git_mode === "pr" && rights.direct_push;
  const [directPush, setDirectPush] = useState(false);
  // Deploy governance: PR description + per-user git connection (context block: server-side).
  const [governance, setGovernance] = useState<GovernanceValue>({ prDescription: "", gitConnId: null });
  const close = useLifecycle((s) => s.closeDialog);
  const effect = meta ? deployEffect(meta.storage, rights, canDirectPush && directPush) : null;
  const team = useBuilder((s) => s.dag.team ?? null);
  const gitTarget = meta?.storage.kind === "git";
  const tokenInfo = useGitTokenInfo(team, gitTarget);
  // The user's git token for this one deploy: component state only (never a store, a
  // draft, storage, the URL or a query key); cleared after every attempt and on close.
  const [gitToken, setGitToken] = useState("");
  const [tokenError, setTokenError] = useState(false);

  const submit = async () => {
    if (tokenMissing(tokenInfo, gitToken)) {
      setTokenError(true);
      return;
    }
    setBusy(true);
    const environment = env || null;
    if (environment !== storeEnv) useBuilder.getState().setEnvironment(environment);
    if (pendingPolicy !== null) {
      const st = useBuilder.getState();
      st.setDag({ source: withPolicy(st.dag, pendingPolicy) });
      setPolicyDraft(null);
    }
    const ok = await deploy(
      {
        message: message.trim() || null,
        environment,
        ...(canDirectPush && directPush ? { directPush: true } : {}),
        prDescription: governance.prDescription.trim() || null,
        gitConnId: governance.gitConnId,
        ...(reviewed?.target_sha != null ? { sourceTargetSha: reviewed.target_sha } : {}),
      },
      { gitToken: tokenInfo && tokenInfo.mode !== "off" ? gitToken : null },
    );
    setGitToken("");
    setBusy(false);
    if (ok) close();
  };

  return (
    <DialogShell
      title={t("deploy.dialogTitle", { dagId })}
      description={meta ? t("deploy.target", { kind: meta.storage.kind, location: meta.storage.location }) : undefined}
      size="md"
      footer={
        <>
          <Button size="sm" variant="ghost" onClick={close}>
            {t("common.cancel")}
          </Button>
          <Button size="sm" colorPalette="blue" loading={busy} onClick={() => void submit()}>
            <LuRocket /> {t("deploy.button")}
          </Button>
        </>
      }
    >
      <Stack gap={4}>
        <Field.Root>
          <Field.Label>{t("deploy.environment")}</Field.Label>
          <NativeSelect.Root size="sm">
            <NativeSelect.Field
              autoFocus={focus === "environment"}
              value={env}
              onChange={(e) => setEnv(e.target.value)}
            >
              <option value="">{t("deploy.defaultEnvironment", { env: meta?.default_environment ?? "—" })}</option>
              {envs.map((e) => (
                <option key={e} value={e}>
                  {e}
                </option>
              ))}
            </NativeSelect.Field>
            <NativeSelect.Indicator />
          </NativeSelect.Root>
          {!envs.length ? <Field.HelperText>{t("deploy.noEnvironments")}</Field.HelperText> : null}
        </Field.Root>
        <Field.Root>
          <Field.Label>{t("deploy.message")}</Field.Label>
          <Textarea
            size="sm"
            rows={3}
            autoFocus={focus === "message"}
            value={message}
            placeholder={t("deploy.messagePlaceholder")}
            onChange={(e) => setMessage(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) void submit();
            }}
          />
          <Field.HelperText>{t("deploy.messageHelp")}</Field.HelperText>
        </Field.Root>
        {effect ? (
          <Alert.Root status={effect.kind === "pr" ? "info" : "neutral"} size="sm" data-testid="deploy-effect">
            <Alert.Indicator>
              {effect.kind === "pr" ? (
                <LuGitPullRequestArrow />
              ) : effect.kind === "push" ? (
                <LuGitCommitHorizontal />
              ) : (
                <LuHardDrive />
              )}
            </Alert.Indicator>
            <Alert.Content>
              <Alert.Title fontSize="sm">
                {effect.kind === "pr"
                  ? t("deploy.effect.pr", { branch: effect.branch })
                  : effect.kind === "push"
                    ? t(effect.path ? "deploy.effect.pushPath" : "deploy.effect.push", {
                        branch: effect.branch,
                        path: effect.path,
                      })
                    : t("deploy.effect.write", { location: effect.location })}
              </Alert.Title>
              {rights?.require_approval ? (
                <Alert.Description fontSize="xs">{t("deploy.approvalHint")}</Alert.Description>
              ) : null}
            </Alert.Content>
          </Alert.Root>
        ) : null}
        {spec.raw_code == null ? (
          <Stack gap={2} data-testid="deploy-source">
            <Text fontSize="sm" fontWeight="semibold">
              {t("source.dagFile")}
            </Text>
            <SourcePolicyField value={policyDraft ?? docPolicy} onChange={setPolicyDraft} />
            {pendingPolicy !== null ? (
              <Text fontSize="xs" color="fg.muted" data-testid="deploy-policy-draft">
                {t("source.policyDraftNote")}
              </Text>
            ) : null}
            <SourcePreview spec={spec} environment={env || null} onPlan={setReviewed} />
          </Stack>
        ) : null}
        {canDirectPush ? (
          <Checkbox.Root
            size="sm"
            checked={directPush}
            onCheckedChange={(e) => setDirectPush(e.checked === true)}
            data-testid="direct-push"
          >
            <Checkbox.HiddenInput />
            <Checkbox.Control />
            <Checkbox.Label>
              {t("deploy.directPush")}
              <Text as="span" display="block" fontSize="xs" color="fg.muted" fontWeight="normal">
                {t("deploy.directPushHelp")}
              </Text>
            </Checkbox.Label>
          </Checkbox.Root>
        ) : null}
        {gitTarget ? (
          <GitTokenField
            info={tokenInfo}
            value={gitToken}
            onChange={(v) => {
              setGitToken(v);
              if (v.trim()) setTokenError(false);
            }}
            invalid={tokenError}
          />
        ) : null}
        <DeployGovernance
          environment={env || null}
          message={message}
          directPush={!!canDirectPush && directPush}
          gitTarget={gitTarget}
          team={team}
          value={governance}
          onChange={setGovernance}
          onMessage={setMessage}
        />
      </Stack>
    </DialogShell>
  );
}
