import { Alert, Button, Field, HStack, Link, NativeSelect, SimpleGrid, Stack, Text } from "@chakra-ui/react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { LuArrowRight, LuExternalLink, LuGitPullRequestArrow } from "react-icons/lu";

import { getStorageStatusOptions } from "../../api/gen/@tanstack/react-query.gen";
import { promoteEnvironment } from "../../api/gen/sdk.gen";
import { errorMessage } from "../../api/setup";
import { useBuilder } from "../../store/builder";
import { DialogShell } from "../components/DialogShell";
import { GitTokenField } from "../components/GitTokenField";
import { useEnvironments } from "../hooks";
import { gitTokenErrorCode, gitTokenHeaders, tokenMissing, useGitTokenInfo } from "../lib/gitToken";

function EnvSelect({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: string;
  options: string[];
  onChange: (v: string) => void;
}) {
  return (
    <Field.Root>
      <Field.Label>{label}</Field.Label>
      <NativeSelect.Root size="sm">
        <NativeSelect.Field value={value} onChange={(e) => onChange(e.target.value)}>
          {options.map((o) => (
            <option key={o} value={o}>
              {o}
            </option>
          ))}
        </NativeSelect.Field>
        <NativeSelect.Indicator />
      </NativeSelect.Root>
    </Field.Root>
  );
}

/** Promote a DAG from one environment (branch) to another through a pull request. */
export default function PromoteDialog() {
  const { t } = useTranslation("lifecycle");
  const dagId = useBuilder((s) => s.savedDagId);
  const storage = useQuery(getStorageStatusOptions());
  const envs = useEnvironments();
  const branches = useMemo(() => {
    const raw = storage.data?.details?.environments;
    return raw && typeof raw === "object" ? (raw as Record<string, string>) : {};
  }, [storage.data]);
  const options = useMemo(() => [...new Set([...envs, ...Object.keys(branches)])], [envs, branches]);
  const [from, setFrom] = useState<string | null>(null);
  const [to, setTo] = useState<string | null>(null);
  const fromEnv = from ?? options[0] ?? "";
  const toEnv = to ?? options.find((o) => o !== fromEnv) ?? "";
  const tokenInfo = useGitTokenInfo(null, storage.data?.kind === "git");
  // The user's git token for this promotion: component state, sent as this request's
  // header from the closure below (never mutation variables), cleared after each attempt.
  const [gitToken, setGitToken] = useState("");
  const [tokenError, setTokenError] = useState(false);
  const promote = useMutation({
    mutationFn: async (vars: { dagId: string; from: string; to: string }) => {
      const headers = gitTokenHeaders(gitToken);
      setGitToken("");
      const { data } = await promoteEnvironment({
        path: { dag_id: vars.dagId },
        body: { from_env: vars.from, to_env: vars.to },
        headers,
        throwOnError: true,
      });
      return data;
    },
  });
  const submit = (id: string) => {
    if (tokenMissing(tokenInfo, gitToken)) {
      setTokenError(true);
      return;
    }
    promote.mutate({ dagId: id, from: fromEnv, to: toEnv });
  };

  if (!dagId) {
    return (
      <DialogShell title={t("promote.title")} size="sm">
        <Text fontSize="sm">{t("common.deployFirst")}</Text>
      </DialogShell>
    );
  }
  const branchOf = (env: string) => (branches[env] ? ` (${branches[env]})` : "");
  return (
    <DialogShell
      title={t("promote.title")}
      description={t("promote.description", { dagId })}
      size="md"
      footer={
        <Button
          size="sm"
          colorPalette="blue"
          disabled={!fromEnv || !toEnv || fromEnv === toEnv}
          loading={promote.isPending}
          onClick={() => submit(dagId)}
        >
          <LuGitPullRequestArrow /> {t("promote.button")}
        </Button>
      }
    >
      <Stack gap={4}>
        {options.length < 2 ? (
          <Alert.Root status="info" size="sm">
            <Alert.Indicator />
            <Alert.Description fontSize="sm">{t("promote.needTwo")}</Alert.Description>
          </Alert.Root>
        ) : null}
        <SimpleGrid columns={3} gap={2} alignItems="end">
          <EnvSelect label={t("promote.from")} value={fromEnv} options={options} onChange={setFrom} />
          <HStack justify="center" pb={2} aria-hidden>
            <LuArrowRight />
          </HStack>
          <EnvSelect label={t("promote.to")} value={toEnv} options={options} onChange={setTo} />
        </SimpleGrid>
        <Text fontSize="xs" color="fg.muted">
          {fromEnv}
          {branchOf(fromEnv)} → {toEnv}
          {branchOf(toEnv)}
        </Text>
        {fromEnv && fromEnv === toEnv ? (
          <Text fontSize="xs" color="red.500">
            {t("promote.same")}
          </Text>
        ) : null}
        <GitTokenField
          info={tokenInfo}
          value={gitToken}
          onChange={(v) => {
            setGitToken(v);
            if (v.trim()) setTokenError(false);
          }}
          invalid={tokenError}
        />
        {promote.isError ? (
          <Text color="red.500" fontSize="sm" role="alert">
            {gitTokenErrorCode(promote.error) === "git_token_rejected"
              ? `${t("gitToken.rejected")}. ${t("gitToken.rejectedHelp")}`
              : errorMessage(promote.error)}
          </Text>
        ) : null}
        {promote.data ? (
          <Alert.Root status="success" aria-live="polite">
            <Alert.Indicator />
            <Alert.Content>
              <Alert.Title>{promote.data.message}</Alert.Title>
              {promote.data.pr_url ? (
                <Alert.Description>
                  <Link href={promote.data.pr_url} target="_blank" rel="noreferrer">
                    {promote.data.pr_url} <LuExternalLink />
                  </Link>
                </Alert.Description>
              ) : null}
            </Alert.Content>
          </Alert.Root>
        ) : null}
      </Stack>
    </DialogShell>
  );
}
