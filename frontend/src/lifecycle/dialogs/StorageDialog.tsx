import {
  Alert,
  Badge,
  Box,
  Button,
  Card,
  DataList,
  Field,
  HStack,
  Input,
  NativeSelect,
  SegmentGroup,
  SimpleGrid,
  Spinner,
  Stack,
  Text,
} from "@chakra-ui/react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { LuCircleCheck, LuFolder, LuFolderGit2, LuGitBranch, LuGitPullRequest, LuRotateCcw } from "react-icons/lu";
import { useAccess } from "../../lib/access";

import {
  getDeployTargetOptions,
  getDeployTargetQueryKey,
  getMetaQueryKey,
  getStorageStatusQueryKey,
  listSpecsQueryKey,
  resetDeployTargetMutation,
  setDeployTargetMutation,
  testDeployTargetMutation,
} from "../../api/gen/@tanstack/react-query.gen";
import type { DeployCandidate, StorageSettings } from "../../api/gen/types.gen";
import { errorMessage } from "../../api/setup";
import { ConnectionPicker } from "../../widgets/AirflowPickers";
import { DialogShell } from "../components/DialogShell";
import { useMeta } from "../hooks";

const KIND_ICON = { local: LuFolder, "airflow-bundle": LuFolderGit2, "git-checkout": LuGitBranch } as const;
const PROVIDERS = ["local", "github", "gitlab", "bitbucket", "azure", "gitea"] as const;
const GIT_CONN_TYPES = ["git", "github", "gitlab", "http"];

/**
 * Where deploys go. Production Airflow usually reads DAGs from git (GitDagBundle or a git-sync
 * checkout of the DAG folder): pick that repository here and AutoAir pushes (or opens pull
 * requests) instead of writing a local folder. Applied immediately, no restart.
 */
export default function StorageDialog() {
  const access = useAccess();
  const { t } = useTranslation("lifecycle");
  const qc = useQueryClient();
  const meta = useMeta();
  const info = useQuery({ ...getDeployTargetOptions(), staleTime: 0 });
  const [draft, setDraft] = useState<StorageSettings | null>(null);
  const [picked, setPicked] = useState<string | null>(null);
  const refresh = async () => {
    for (const queryKey of [
      getDeployTargetQueryKey(),
      getStorageStatusQueryKey(),
      getMetaQueryKey(),
      listSpecsQueryKey(),
    ])
      await qc.invalidateQueries({ queryKey });
  };
  const test = useMutation(testDeployTargetMutation());
  const save = useMutation({
    ...setDeployTargetMutation(),
    onSuccess: async () => {
      setDraft(null);
      setPicked(null);
      await refresh();
    },
  });
  const reset = useMutation({ ...resetDeployTargetMutation(), onSuccess: refresh });
  const data = info.data;

  const choose = (c: DeployCandidate) => {
    setPicked(c.id);
    setDraft(structuredClone(c.settings));
    test.reset();
    save.reset();
  };
  const git = draft?.git;
  const setGit = (patch: Partial<NonNullable<StorageSettings["git"]>>) =>
    setDraft((d) => (d ? { ...d, git: { ...d.git, ...patch } } : d));

  return (
    <DialogShell title={t("deployTarget.title")} description={t("deployTarget.description")} size="lg">
      {info.isPending ? <Spinner size="sm" /> : null}
      {info.isError ? <Text color="red.500">{errorMessage(info.error)}</Text> : null}
      {data ? (
        <Stack gap={4}>
          <Card.Root size="sm" variant="subtle">
            <Card.Body gap={1}>
              <HStack wrap="wrap">
                <Text fontWeight="semibold" fontSize="sm">
                  {t("deployTarget.current")}
                </Text>
                <Badge>{data.status?.kind ?? data.settings.mode}</Badge>
                {data.settings.mode === "git" ? (
                  <Badge colorPalette="purple">
                    {data.settings.git?.mode === "push" ? t("deployTarget.modePush") : t("deployTarget.modePr")}
                  </Badge>
                ) : null}
                <Badge variant="outline">
                  {data.source === "ui" ? t("deployTarget.sourceUi") : t("deployTarget.sourceSettings")}
                </Badge>
                {data.status ? (
                  <Badge colorPalette={data.status.writable ? "green" : "red"}>
                    {data.status.writable ? t("storage.writable") : t("storage.readOnly")}
                  </Badge>
                ) : null}
              </HStack>
              <Text fontFamily="mono" fontSize="xs" wordBreak="break-all">
                {data.status?.location ?? "—"}
              </Text>
              {data.error ? (
                <Text fontSize="xs" color="red.500">
                  {data.error}
                </Text>
              ) : null}
              {data.source === "ui" ? (
                <Button
                  size="2xs"
                  variant="ghost"
                  alignSelf="start"
                  loading={reset.isPending}
                  onClick={() => reset.mutate({})}
                >
                  <LuRotateCcw /> {t("deployTarget.reset")}
                </Button>
              ) : null}
            </Card.Body>
          </Card.Root>

          <Stack gap={2}>
            <Text fontWeight="semibold" fontSize="sm">
              {t("deployTarget.options")}
            </Text>
            <SimpleGrid minChildWidth="15rem" gap={2}>
              {data.candidates.map((c) => {
                const Icon = KIND_ICON[c.kind];
                return (
                  <Card.Root
                    key={c.id}
                    size="sm"
                    variant={picked === c.id ? "subtle" : "outline"}
                    borderColor={picked === c.id ? "blue.500" : undefined}
                    cursor="pointer"
                    role="button"
                    tabIndex={0}
                    data-testid={`deploy-candidate-${c.kind}`}
                    onClick={() => choose(c)}
                    onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && choose(c)}
                  >
                    <Card.Body gap={1}>
                      <HStack align="start">
                        <Box as="span" mt="2px" flexShrink={0}>
                          <Icon />
                        </Box>
                        <Text fontWeight="semibold" fontSize="sm">
                          {c.kind === "local"
                            ? t("deployTarget.kindLocal")
                            : c.kind === "git-checkout"
                              ? t("deployTarget.kindCheckout")
                              : c.title}
                        </Text>
                      </HStack>
                      <Text
                        fontSize="xs"
                        color="fg.muted"
                        lineClamp={3}
                        wordBreak={c.kind === "local" ? "break-all" : undefined}
                      >
                        {c.kind === "local" ? c.description : t(`deployTarget.hint.${c.kind}`)}
                      </Text>
                      {c.repo ? (
                        <Text fontFamily="mono" fontSize="2xs" wordBreak="break-all">
                          {c.repo}
                          {c.branch ? ` @ ${c.branch}` : ""}
                          {c.subdir ? ` / ${c.subdir}` : ""}
                        </Text>
                      ) : null}
                    </Card.Body>
                  </Card.Root>
                );
              })}
            </SimpleGrid>
            {!data.candidates.some((c) => c.kind !== "local") &&
            (data.policy?.allowed_targets ?? ["git"]).includes("git") ? (
              <Text fontSize="xs" color="fg.muted">
                {t("deployTarget.noGit")}
              </Text>
            ) : null}
          </Stack>

          {draft ? (
            <Stack gap={3} borderWidth="1px" rounded="md" p={3}>
              {draft.mode === "git" && git ? (
                <>
                  {git.use_airflow_bundle ? (
                    <Alert.Root status="info" size="sm">
                      <Alert.Indicator />
                      <Alert.Description fontSize="xs">
                        {t("deployTarget.fromBundle", { name: git.bundle_name ?? "" })}
                      </Alert.Description>
                    </Alert.Root>
                  ) : null}
                  <SegmentGroup.Root
                    size="sm"
                    value={git.mode ?? "pr"}
                    onValueChange={(e) => setGit({ mode: e.value === "push" ? "push" : "pr" })}
                  >
                    <SegmentGroup.Indicator />
                    <SegmentGroup.Items
                      items={[
                        {
                          value: "pr",
                          label: (
                            <HStack gap={1}>
                              <LuGitPullRequest /> {t("deployTarget.modePr")}
                            </HStack>
                          ),
                        },
                        {
                          value: "push",
                          label: (
                            <HStack gap={1}>
                              <LuGitBranch /> {t("deployTarget.modePush")}
                            </HStack>
                          ),
                        },
                      ]}
                    />
                  </SegmentGroup.Root>
                  <Text fontSize="xs" color="fg.muted">
                    {git.mode === "push" ? t("deployTarget.modePushHelp") : t("deployTarget.modePrHelp")}
                  </Text>
                  <SimpleGrid columns={{ base: 1, md: 2 }} gap={2}>
                    <Field.Root>
                      <Field.Label fontSize="xs">{t("deployTarget.provider")}</Field.Label>
                      <NativeSelect.Root size="sm">
                        <NativeSelect.Field
                          value={git.provider ?? "local"}
                          onChange={(e) => setGit({ provider: e.target.value as (typeof PROVIDERS)[number] })}
                        >
                          {PROVIDERS.map((p) => (
                            <option key={p} value={p}>
                              {t(`deployTarget.providers.${p}`)}
                            </option>
                          ))}
                        </NativeSelect.Field>
                      </NativeSelect.Root>
                    </Field.Root>
                    <Field.Root>
                      <Field.Label fontSize="xs">{t("deployTarget.repo")}</Field.Label>
                      <Input
                        size="sm"
                        fontFamily="mono"
                        placeholder={git.use_airflow_bundle ? t("deployTarget.fromBundleShort") : "acme/dags"}
                        value={git.repo ?? ""}
                        onChange={(e) => setGit({ repo: e.target.value || null })}
                      />
                    </Field.Root>
                    <Field.Root>
                      <Field.Label fontSize="xs">{t("deployTarget.branch")}</Field.Label>
                      <Input
                        size="sm"
                        fontFamily="mono"
                        placeholder={git.use_airflow_bundle ? t("deployTarget.fromBundleShort") : "main"}
                        value={git.base_branch ?? ""}
                        onChange={(e) => setGit({ base_branch: e.target.value || null })}
                      />
                    </Field.Root>
                    <Field.Root>
                      <Field.Label fontSize="xs">{t("deployTarget.subdir")}</Field.Label>
                      <Input
                        size="sm"
                        fontFamily="mono"
                        placeholder="dags"
                        value={git.subdir ?? ""}
                        onChange={(e) => setGit({ subdir: e.target.value || null })}
                      />
                    </Field.Root>
                  </SimpleGrid>
                  {data.policy?.forced_conn_id ? (
                    // Admin policy (storage.git.forced_conn_id): not editable here.
                    <Field.Root disabled>
                      <Field.Label fontSize="xs">{t("deployTarget.connection")}</Field.Label>
                      <Input
                        size="sm"
                        fontFamily="mono"
                        value={data.policy.forced_conn_id}
                        readOnly
                        data-testid="forced-conn"
                      />
                      <Field.HelperText fontSize="xs">{t("deployTarget.forcedConnection")}</Field.HelperText>
                    </Field.Root>
                  ) : (
                    <ConnectionPicker
                      label={t("deployTarget.connection")}
                      help={t("deployTarget.connectionHelp")}
                      connTypes={GIT_CONN_TYPES}
                      value={git.conn_id}
                      onChange={(conn_id) => setGit({ conn_id })}
                    />
                  )}
                  {data.policy?.credentials_mode === "per_user" ? (
                    <Text fontSize="xs" color="fg.muted" data-testid="per-user-credentials">
                      {t("deployTarget.perUserCredentials", { template: data.policy.user_conn_id_template })}
                    </Text>
                  ) : null}
                </>
              ) : (
                <Text fontSize="sm">{t("deployTarget.localHelp")}</Text>
              )}
              {test.data ? (
                <Alert.Root status={test.data.ok ? "success" : "error"} size="sm">
                  <Alert.Indicator />
                  <Alert.Description fontSize="xs" wordBreak="break-word">
                    {test.data.message}
                  </Alert.Description>
                </Alert.Root>
              ) : null}
              {save.isError ? (
                <Text fontSize="xs" color="red.500">
                  {errorMessage(save.error)}
                </Text>
              ) : null}
              <HStack justify="end">
                <Button
                  size="sm"
                  variant="outline"
                  loading={test.isPending}
                  onClick={() => test.mutate({ body: draft })}
                >
                  <LuCircleCheck /> {t("deployTarget.test")}
                </Button>
                <Button
                  size="sm"
                  colorPalette="blue"
                  loading={save.isPending}
                  disabled={!access.can("admin:deploy_target")}
                  title={
                    access.can("admin:deploy_target")
                      ? undefined
                      : t("access.denied", { permission: "admin:deploy_target" })
                  }
                  onClick={() => save.mutate({ body: draft })}
                >
                  {t("deployTarget.save")}
                </Button>
              </HStack>
            </Stack>
          ) : null}

          {meta ? (
            <Box>
              <DataList.Root orientation="horizontal" size="sm">
                <DataList.Item>
                  <DataList.ItemLabel>{t("storage.airflow")}</DataList.ItemLabel>
                  <DataList.ItemValue>{meta.airflow_version ?? "—"}</DataList.ItemValue>
                </DataList.Item>
                <DataList.Item>
                  <DataList.ItemLabel>{t("storage.autoair")}</DataList.ItemLabel>
                  <DataList.ItemValue>{meta.autoair_version}</DataList.ItemValue>
                </DataList.Item>
                <DataList.Item>
                  <DataList.ItemLabel>{t("storage.environments")}</DataList.ItemLabel>
                  <DataList.ItemValue>{meta.environments.join(", ") || "—"}</DataList.ItemValue>
                </DataList.Item>
              </DataList.Root>
            </Box>
          ) : null}
        </Stack>
      ) : null}
    </DialogShell>
  );
}
