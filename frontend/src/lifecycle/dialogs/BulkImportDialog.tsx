import {
  Badge,
  Box,
  Button,
  Checkbox,
  Field,
  HStack,
  Input,
  NativeSelect,
  SegmentGroup,
  Stack,
  Table,
  Text,
} from "@chakra-ui/react";
import { useMutation } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { LuFolderOpen, LuGitBranch, LuScanSearch, LuUpload } from "react-icons/lu";

import { bulkImportDags, deploySpec } from "../../api/gen/sdk.gen";
import type { BulkImportResult, DeployResult } from "../../api/gen/types.gen";
import { errorMessage } from "../../api/setup";
import {
  type PickedFile,
  defaultSelection,
  duplicateDagIds,
  planDeploys,
  relativePath,
  wantedFile,
} from "../../lib/bulkImport";
import { withPolicy } from "../../lib/sourcePolicy";
import { Hint } from "../../widgets/fields";
import { DialogShell } from "../components/DialogShell";
import { GitTokenField } from "../components/GitTokenField";
import { useMeta } from "../hooks";
import { gitTokenHeaders, tokenMissing, useGitTokenInfo } from "../lib/gitToken";
import { useLifecycle } from "../store";

type Source = "folder" | "git";
type Outcome = { dagId: string; status: DeployResult["status"] | "error"; message?: string | null };

const STATUS_COLOR: Record<string, string> = {
  imported: "green",
  no_dag: "gray",
  excluded: "gray",
  error: "red",
  not_scanned: "orange",
};

async function readFolder(list: FileList | null): Promise<PickedFile[]> {
  const files = Array.from(list ?? []);
  const picked = files
    .map((f) => ({ file: f, path: relativePath(f.webkitRelativePath, f.name) }))
    .filter((f) => wantedFile(f.path));
  return Promise.all(picked.map(async ({ file, path }) => ({ path, source: await file.text() })));
}

/**
 * Import a whole DAG repository (a folder picked in the browser or a git URL) with the Python
 * importer; review the per-file result, then deploy the selected DAGs. Existing DAGs are
 * never overwritten unless "overwrite" is ticked for them (and then only the version that was
 * compared, via base_etag). Needs code:execute + spec:write and features.imports.
 */
export default function BulkImportDialog() {
  const { t } = useTranslation("lifecycle");
  const close = useLifecycle((s) => s.closeDialog);
  const [source, setSource] = useState<Source>("folder");
  const [files, setFiles] = useState<PickedFile[]>([]);
  const [git, setGit] = useState({ url: "", ref: "", subdir: "", token: "" });
  const [result, setResult] = useState<BulkImportResult | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [confirmed, setConfirmed] = useState<Set<string>>(new Set());
  const [outcomes, setOutcomes] = useState<Outcome[]>([]);
  const folderInput = useRef<HTMLInputElement>(null);
  const meta = useMeta();
  // The deploy target's git token (storage.git.request_token), not the source repository's.
  const tokenInfo = useGitTokenInfo(null, meta?.storage.kind === "git");
  const [deployToken, setDeployToken] = useState("");
  // How the deployed DAG files are written: the imported (hand-written) files kept and
  // patched on later edits, or files generated from the specs.
  const [filePolicy, setFilePolicy] = useState<"auto" | "generate">("auto");
  const [tokenError, setTokenError] = useState(false);

  const scan = useMutation({
    mutationFn: async () => {
      const body =
        source === "git"
          ? {
              git: {
                url: git.url.trim(),
                ref: git.ref.trim() || null,
                subdir: git.subdir.trim() || null,
                token: git.token || null,
              },
            }
          : { files };
      const { data } = await bulkImportDags({ body, throwOnError: true });
      return data;
    },
    onSuccess: (data) => {
      setResult(data);
      setSelected(defaultSelection(data));
      setConfirmed(new Set());
      setOutcomes([]);
    },
  });

  const deploy = useMutation({
    mutationFn: async () => {
      if (!result) return [];
      const { deploys } = planDeploys(result, selected, confirmed);
      const headers = gitTokenHeaders(deployToken);
      setDeployToken("");
      const specs = new Map(result.files.flatMap((f) => (f.dags ?? []).map((d) => [d.dag_id, d.spec] as const)));
      const out: Outcome[] = [];
      for (const plan of deploys) {
        const imported = specs.get(plan.dagId);
        if (!imported) continue;
        const spec = filePolicy === "generate" ? { ...imported, source: withPolicy(imported, "generate") } : imported;
        try {
          const { data } = await deploySpec({
            path: { dag_id: plan.dagId },
            body: { spec, create_only: true, base_etag: plan.baseEtag },
            headers,
            throwOnError: true,
          });
          out.push({ dagId: plan.dagId, status: data.status, message: data.message });
        } catch (err) {
          out.push({ dagId: plan.dagId, status: "error", message: errorMessage(err) });
        }
        setOutcomes([...out]);
      }
      return out;
    },
  });

  const toggle = (set: Set<string>, id: string, on: boolean) => {
    const next = new Set(set);
    if (on) next.add(id);
    else next.delete(id);
    return next;
  };
  const duplicates = result ? duplicateDagIds(result) : new Set<string>();
  const plan = result ? planDeploys(result, selected, confirmed) : { deploys: [], skipped: [] };
  const canScan = source === "git" ? /^https:\/\//.test(git.url.trim()) : files.length > 0;

  return (
    <DialogShell
      title={t("bulkImport.title")}
      description={t("bulkImport.description")}
      size="xl"
      footer={
        <HStack w="full" justify="space-between">
          <Text fontSize="xs" color="fg.muted">
            {result ? t("bulkImport.planned", { count: plan.deploys.length, skipped: plan.skipped.length }) : null}
          </Text>
          <HStack>
            <Button size="sm" variant="ghost" onClick={close}>
              {t("bulkImport.close")}
            </Button>
            <Button
              size="sm"
              colorPalette="blue"
              disabled={!result || plan.deploys.length === 0 || deploy.isPending}
              loading={deploy.isPending}
              onClick={() => {
                if (tokenMissing(tokenInfo, deployToken)) setTokenError(true);
                else deploy.mutate();
              }}
              data-testid="bulk-import-deploy"
            >
              <LuUpload /> {t("bulkImport.deploy", { count: plan.deploys.length })}
            </Button>
          </HStack>
        </HStack>
      }
    >
      <Stack gap={3}>
        <SegmentGroup.Root
          size="sm"
          value={source}
          onValueChange={(e) => setSource((e.value as Source | null) ?? "folder")}
        >
          <SegmentGroup.Indicator />
          <SegmentGroup.Item value="folder">
            <SegmentGroup.ItemText>
              <HStack gap={1}>
                <LuFolderOpen /> {t("bulkImport.folder")}
              </HStack>
            </SegmentGroup.ItemText>
            <SegmentGroup.ItemHiddenInput />
          </SegmentGroup.Item>
          <SegmentGroup.Item value="git">
            <SegmentGroup.ItemText>
              <HStack gap={1}>
                <LuGitBranch /> {t("bulkImport.git")}
              </HStack>
            </SegmentGroup.ItemText>
            <SegmentGroup.ItemHiddenInput />
          </SegmentGroup.Item>
        </SegmentGroup.Root>

        {source === "folder" ? (
          <Stack gap={1}>
            <input
              ref={folderInput}
              type="file"
              hidden
              multiple
              data-testid="bulk-import-folder"
              {...{ webkitdirectory: "", directory: "" }}
              onChange={(e) => void readFolder(e.target.files).then(setFiles)}
            />
            <HStack>
              <Button size="sm" variant="outline" onClick={() => folderInput.current?.click()}>
                <LuFolderOpen /> {t("bulkImport.pickFolder")}
              </Button>
              <Text fontSize="sm" color="fg.muted">
                {files.length ? t("bulkImport.picked", { count: files.length }) : null}
              </Text>
            </HStack>
            <Hint>{t("bulkImport.folderHelp")}</Hint>
          </Stack>
        ) : (
          <Stack gap={2}>
            <Field.Root>
              <Field.Label>{t("bulkImport.url")}</Field.Label>
              <Input
                size="sm"
                fontFamily="mono"
                placeholder="https://github.com/acme/airflow-dags.git"
                value={git.url}
                onChange={(e) => setGit({ ...git, url: e.target.value })}
              />
            </Field.Root>
            <HStack align="start">
              <Field.Root>
                <Field.Label>{t("bulkImport.ref")}</Field.Label>
                <Input
                  size="sm"
                  fontFamily="mono"
                  placeholder="main"
                  value={git.ref}
                  onChange={(e) => setGit({ ...git, ref: e.target.value })}
                />
              </Field.Root>
              <Field.Root>
                <Field.Label>{t("bulkImport.subdir")}</Field.Label>
                <Input
                  size="sm"
                  fontFamily="mono"
                  placeholder="dags"
                  value={git.subdir}
                  onChange={(e) => setGit({ ...git, subdir: e.target.value })}
                />
              </Field.Root>
            </HStack>
            <Field.Root>
              <Field.Label>{t("bulkImport.token")}</Field.Label>
              <Input
                size="sm"
                type="password"
                autoComplete="off"
                value={git.token}
                onChange={(e) => setGit({ ...git, token: e.target.value })}
              />
              <Field.HelperText>{t("bulkImport.tokenHelp")}</Field.HelperText>
            </Field.Root>
          </Stack>
        )}

        <HStack>
          <Button
            size="sm"
            colorPalette="blue"
            variant="subtle"
            disabled={!canScan || scan.isPending}
            loading={scan.isPending}
            onClick={() => scan.mutate()}
            data-testid="bulk-import-scan"
          >
            <LuScanSearch /> {t("bulkImport.scan")}
          </Button>
          <Hint>{t("bulkImport.scanHelp")}</Hint>
        </HStack>
        {scan.isError ? <Hint tone="error">{errorMessage(scan.error)}</Hint> : null}

        {result ? (
          <Stack gap={2} data-testid="bulk-import-result">
            <HStack gap={2} flexWrap="wrap">
              <Badge>{t("bulkImport.sumFiles", { count: result.summary.files })}</Badge>
              <Badge colorPalette="green">{t("bulkImport.sumDags", { count: result.summary.convertible })}</Badge>
              {result.summary.existing ? (
                <Badge colorPalette="orange">{t("bulkImport.sumExisting", { count: result.summary.existing })}</Badge>
              ) : null}
              {result.summary.errors ? (
                <Badge colorPalette="red">{t("bulkImport.sumErrors", { count: result.summary.errors })}</Badge>
              ) : null}
            </HStack>
            <Box overflowX="auto" maxH="50vh" overflowY="auto" borderWidth="1px" rounded="md">
              <Table.Root size="sm" stickyHeader>
                <Table.Header>
                  <Table.Row>
                    <Table.ColumnHeader>{t("bulkImport.file")}</Table.ColumnHeader>
                    <Table.ColumnHeader>{t("bulkImport.status")}</Table.ColumnHeader>
                    <Table.ColumnHeader>{t("bulkImport.dags")}</Table.ColumnHeader>
                  </Table.Row>
                </Table.Header>
                <Table.Body>
                  {result.files
                    .filter((f) => f.status !== "no_dag" && f.status !== "excluded")
                    .map((f) => (
                      <Table.Row key={f.path} verticalAlign="top">
                        <Table.Cell fontFamily="mono" fontSize="xs" wordBreak="break-all">
                          {f.path}
                        </Table.Cell>
                        <Table.Cell>
                          <Badge size="sm" colorPalette={STATUS_COLOR[f.status] ?? "gray"}>
                            {t(`bulkImport.fileStatus.${f.status}`)}
                          </Badge>
                        </Table.Cell>
                        <Table.Cell>
                          <Stack gap={1}>
                            {(f.dags ?? []).map((d) => {
                              const dup = duplicates.has(d.dag_id);
                              const outcome = outcomes.find((o) => o.dagId === d.dag_id);
                              return (
                                <Stack key={d.dag_id} gap={0}>
                                  <HStack gap={2} flexWrap="wrap">
                                    <Checkbox.Root
                                      size="sm"
                                      disabled={!d.spec}
                                      checked={selected.has(d.dag_id)}
                                      onCheckedChange={(e) => setSelected(toggle(selected, d.dag_id, !!e.checked))}
                                    >
                                      <Checkbox.HiddenInput />
                                      <Checkbox.Control />
                                      <Checkbox.Label fontFamily="mono" fontSize="xs">
                                        {d.dag_id}
                                      </Checkbox.Label>
                                    </Checkbox.Root>
                                    {d.exists ? (
                                      <Checkbox.Root
                                        size="sm"
                                        colorPalette="orange"
                                        checked={confirmed.has(d.dag_id)}
                                        onCheckedChange={(e) => setConfirmed(toggle(confirmed, d.dag_id, !!e.checked))}
                                      >
                                        <Checkbox.HiddenInput />
                                        <Checkbox.Control />
                                        <Checkbox.Label fontSize="xs">{t("bulkImport.overwrite")}</Checkbox.Label>
                                      </Checkbox.Root>
                                    ) : null}
                                    {d.warnings.length ? (
                                      <Badge size="xs" colorPalette="yellow" title={d.warnings.join("\n")}>
                                        {t("bulkImport.warnings", { count: d.warnings.length })}
                                      </Badge>
                                    ) : null}
                                    {dup ? (
                                      <Badge size="xs" colorPalette="red">
                                        {t("bulkImport.duplicate")}
                                      </Badge>
                                    ) : null}
                                    {outcome ? (
                                      <Badge
                                        size="xs"
                                        colorPalette={
                                          outcome.status === "deployed"
                                            ? "green"
                                            : outcome.status === "pending_approval"
                                              ? "blue"
                                              : "red"
                                        }
                                        title={outcome.message ?? undefined}
                                      >
                                        {t(`bulkImport.outcome.${outcome.status}`)}
                                      </Badge>
                                    ) : null}
                                  </HStack>
                                  {d.error ? <Hint tone="error">{d.error}</Hint> : null}
                                </Stack>
                              );
                            })}
                            {f.import_errors?.length ? (
                              <Text
                                as="pre"
                                fontSize="2xs"
                                color="fg.error"
                                whiteSpace="pre-wrap"
                                maxH="6em"
                                overflow="auto"
                              >
                                {f.import_errors.join("\n")}
                              </Text>
                            ) : null}
                            {f.message ? <Hint>{f.message}</Hint> : null}
                          </Stack>
                        </Table.Cell>
                      </Table.Row>
                    ))}
                </Table.Body>
              </Table.Root>
            </Box>
            <Hint>
              {t("bulkImport.skippedFiles", {
                count: result.files.filter((f) => f.status === "no_dag" || f.status === "excluded").length,
              })}
            </Hint>
          </Stack>
        ) : null}
        {result ? (
          <Field.Root>
            <Field.Label>{t("bulkImport.filePolicy")}</Field.Label>
            <NativeSelect.Root size="sm">
              <NativeSelect.Field
                value={filePolicy}
                onChange={(e) => setFilePolicy(e.target.value === "generate" ? "generate" : "auto")}
                data-testid="bulk-import-file-policy"
              >
                <option value="auto">{t("bulkImport.filePolicyPreserve")}</option>
                <option value="generate">{t("bulkImport.filePolicyGenerate")}</option>
              </NativeSelect.Field>
              <NativeSelect.Indicator />
            </NativeSelect.Root>
            <Field.HelperText>{t("bulkImport.filePolicyHelp")}</Field.HelperText>
          </Field.Root>
        ) : null}
        {result ? (
          <GitTokenField
            info={tokenInfo}
            value={deployToken}
            onChange={(v) => {
              setDeployToken(v);
              if (v.trim()) setTokenError(false);
            }}
            invalid={tokenError}
          />
        ) : null}
      </Stack>
    </DialogShell>
  );
}
