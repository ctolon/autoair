import { Badge, Box, Code, Collapsible, Field, HStack, NativeSelect, Stack, Text, Textarea } from "@chakra-ui/react";
import { useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { LuChevronRight, LuShieldCheck } from "react-icons/lu";

import { GenerateDeployMessage } from "../../ai/GenerateDeployMessage";
import { getGitCredentialsOptions } from "../../api/gen/@tanstack/react-query.gen";
import { previewDeploy } from "../../api/gen/sdk.gen";
import type { DeployPreview } from "../../api/gen/types.gen";
import { errorMessage } from "../../api/setup";
import { useBuilder } from "../../store/builder";

export interface GovernanceValue {
  prDescription: string;
  gitConnId: string | null;
}

/** Debounced value (the preview is re-computed when the user stops typing). */
function useDebounced<T>(value: T, ms: number): T {
  const [current, setCurrent] = useState(value);
  useEffect(() => {
    const id = setTimeout(() => setCurrent(value), ms);
    return () => clearTimeout(id);
  }, [value, ms]);
  return current;
}

/**
 * Deploy dialog section owned by deploy governance (docs/configuration.md#deploy-governance):
 * the pull request description, the user's git connection (per-user credentials) and a
 * read-only preview of the commit message / PR description with the mandatory context block
 * (user, versions, validation, approvals, link) the server always appends. The Generate button
 * fills the commit message (`onMessage`) and, when the deploy opens a PR, its description.
 */
export function DeployGovernance({
  environment,
  message,
  directPush,
  gitTarget,
  team = null,
  value,
  onChange,
  onMessage,
}: {
  environment: string | null;
  message: string;
  directPush: boolean;
  gitTarget: boolean;
  /** Team of the DAG: its deploy target (team_storage) decides the git credentials. */
  team?: string | null;
  value: GovernanceValue;
  onChange: (next: GovernanceValue) => void;
  /** Receives the generated commit subject. */
  onMessage?: (subject: string) => void;
}) {
  const { t } = useTranslation("lifecycle");
  const credentials = useQuery({
    ...getGitCredentialsOptions(team ? { query: { team } } : undefined),
    enabled: gitTarget,
    retry: false,
    staleTime: 60_000,
  });
  const creds = credentials.data;
  const debounced = useDebounced({ environment, message, directPush, ...value }, 400);
  const [preview, setPreview] = useState<DeployPreview | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    const spec = useBuilder.getState().toSpec();
    previewDeploy({
      path: { dag_id: spec.dag_id },
      body: {
        spec,
        environment: debounced.environment,
        message: debounced.message.trim() || null,
        pr_description: debounced.prDescription.trim() || null,
        git_conn_id: debounced.gitConnId,
        ...(debounced.directPush ? { direct_push: true } : {}),
      },
      throwOnError: true,
    })
      .then(({ data }) => {
        if (!alive) return;
        setPreview(data);
        setError(null);
      })
      .catch((e: unknown) => {
        if (!alive) return;
        setPreview(null);
        setError(errorMessage(e));
      });
    return () => {
      alive = false;
    };
  }, [debounced]);

  const opensPr = preview?.opens_pr ?? false;
  const perUser = creds?.mode === "per_user" && !creds.forced_conn_id;
  const choices = perUser ? [...new Set([creds.conn_id, ...(creds.choices ?? [])].filter(Boolean) as string[])] : [];

  return (
    <Stack gap={3} data-testid="deploy-governance">
      {opensPr ? (
        <Field.Root>
          <HStack justify="space-between" w="full">
            <Field.Label>{t("deploy.prDescription")}</Field.Label>
            <GenerateDeployMessage
              environment={environment}
              onGenerated={(m) => {
                onMessage?.(m.title);
                onChange({ ...value, prDescription: m.description });
              }}
            />
          </HStack>
          <Textarea
            size="sm"
            rows={3}
            value={value.prDescription}
            placeholder={t("deploy.prDescriptionPlaceholder")}
            onChange={(e) => onChange({ ...value, prDescription: e.target.value })}
            data-testid="pr-description"
          />
          <Field.HelperText>{t("deploy.prDescriptionHelp")}</Field.HelperText>
        </Field.Root>
      ) : onMessage ? (
        <HStack justify="flex-end">
          <GenerateDeployMessage environment={environment} onGenerated={(m) => onMessage(m.title)} />
        </HStack>
      ) : null}
      {gitTarget && creds?.forced_conn_id ? (
        <Text fontSize="xs" color="fg.muted" data-testid="forced-connection">
          {t("deploy.forcedConnection", { conn: creds.forced_conn_id })}
        </Text>
      ) : null}
      {perUser ? (
        <Field.Root invalid={creds.conn_exists === false && !creds.fallback}>
          <Field.Label>{t("deploy.gitConnection")}</Field.Label>
          {choices.length > 1 ? (
            <NativeSelect.Root size="sm">
              <NativeSelect.Field
                value={value.gitConnId ?? creds.conn_id ?? ""}
                onChange={(e) => onChange({ ...value, gitConnId: e.target.value || null })}
                data-testid="git-connection"
              >
                {choices.map((c) => (
                  <option key={c} value={c}>
                    {c}
                  </option>
                ))}
              </NativeSelect.Field>
              <NativeSelect.Indicator />
            </NativeSelect.Root>
          ) : (
            <Code size="sm" data-testid="git-connection">
              {creds.conn_id ?? "—"}
            </Code>
          )}
          <Field.HelperText>
            {creds.conn_exists === false
              ? creds.fallback
                ? t("deploy.gitConnectionFallback", { conn: creds.conn_id })
                : t("deploy.gitConnectionMissing", { conn: creds.conn_id })
              : t("deploy.gitConnectionHelp")}
          </Field.HelperText>
        </Field.Root>
      ) : null}
      <Collapsible.Root>
        <Collapsible.Trigger asChild>
          <HStack
            as="button"
            gap={1}
            fontSize="xs"
            color="fg.muted"
            cursor="pointer"
            css={{ "&[data-state=open] > svg:first-of-type": { transform: "rotate(90deg)" } }}
            data-testid="context-preview-toggle"
          >
            <LuChevronRight />
            <LuShieldCheck />
            <Text>{t("deploy.contextPreview")}</Text>
            {preview ? (
              <Badge size="xs" variant="surface">
                {preview.commit_author === "user" ? t("deploy.authorUser") : t("deploy.authorService")}
              </Badge>
            ) : null}
          </HStack>
        </Collapsible.Trigger>
        <Collapsible.Content>
          <Stack gap={2} mt={2}>
            <Text fontSize="xs" color="fg.muted">
              {t("deploy.contextHelp")}
            </Text>
            {error ? (
              <Text fontSize="xs" color="fg.error" role="alert">
                {error}
              </Text>
            ) : null}
            {preview ? (
              <>
                <PreviewBlock label={t("deploy.commitPreview")} text={preview.commit_message} testId="commit-preview" />
                {preview.opens_pr && preview.pr_body ? (
                  <PreviewBlock
                    label={t("deploy.prPreview", { title: preview.pr_title ?? "" })}
                    text={preview.pr_body}
                    testId="pr-preview"
                  />
                ) : null}
              </>
            ) : null}
          </Stack>
        </Collapsible.Content>
      </Collapsible.Root>
    </Stack>
  );
}

function PreviewBlock({ label, text, testId }: { label: string; text: string; testId: string }) {
  return (
    <Box>
      <Text fontSize="xs" fontWeight="medium" mb={1}>
        {label}
      </Text>
      <Box
        as="pre"
        fontSize="xs"
        fontFamily="mono"
        whiteSpace="pre-wrap"
        wordBreak="break-word"
        p={2}
        rounded="md"
        bg="bg.subtle"
        borderWidth="1px"
        maxH="220px"
        overflowY="auto"
        data-testid={testId}
      >
        {text}
      </Box>
    </Box>
  );
}
