import { Alert, Button, Checkbox, Field, NativeSelect, Stack, Text, Textarea } from "@chakra-ui/react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { LuGitCommitHorizontal, LuGitPullRequestArrow, LuHardDrive, LuRocket } from "react-icons/lu";

import { useBuilder } from "../../store/builder";
import { deploy } from "../actions";
import { DeployGovernance, type GovernanceValue } from "../components/DeployGovernance";
import { DialogShell } from "../components/DialogShell";
import { GitTokenField } from "../components/GitTokenField";
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
    const ok = await deploy(
      {
        message: message.trim() || null,
        environment,
        ...(canDirectPush && directPush ? { directPush: true } : {}),
        prDescription: governance.prDescription.trim() || null,
        gitConnId: governance.gitConnId,
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
