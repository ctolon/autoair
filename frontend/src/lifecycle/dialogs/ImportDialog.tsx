import { Alert, Badge, Box, Button, Field, FileUpload, HStack, Input, Stack, Tabs, Text } from "@chakra-ui/react";
import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { LuDownload, LuFileCode, LuFileUp, LuFolderOpen } from "react-icons/lu";

import {
  importAirflowDagMutation,
  importPythonDagMutation,
  importSpecJsonMutation,
  importYamlDagMutation,
} from "../../api/gen/@tanstack/react-query.gen";
import type { DagSpec, PythonImportResult } from "../../api/gen/types.gen";
import { errorMessage } from "../../api/setup";
import { editorAvailability } from "../../codemode/store";
import { useAccess } from "../../lib/access";
import { openAsRawDag, openImported } from "../actions";
import { DialogShell } from "../components/DialogShell";
import { CodeView } from "../components/lazy";
import { type ImportSource, useLifecycle } from "../store";

function UploadButton({ accept, onText }: { accept: string; onText: (text: string, name: string) => void }) {
  const { t } = useTranslation("lifecycle");
  return (
    <FileUpload.Root
      accept={accept}
      maxFiles={1}
      w="auto"
      onFileAccept={async (e) => {
        const file = e.files[0];
        if (file) onText(await file.text(), file.name);
      }}
    >
      <FileUpload.HiddenInput />
      <FileUpload.Trigger asChild>
        <Button size="xs" variant="outline">
          <LuFileUp /> {t("import.upload")}
        </Button>
      </FileUpload.Trigger>
    </FileUpload.Root>
  );
}

function OpenButton({ spec, onDone }: { spec: DagSpec; onDone: () => void }) {
  const { t } = useTranslation("lifecycle");
  return (
    <Button size="xs" colorPalette="blue" onClick={() => openImported(spec) && onDone()}>
      <LuFolderOpen /> {t("import.openUnsaved")}
    </Button>
  );
}

function PythonResults({ result, onDone }: { result: PythonImportResult; onDone: () => void }) {
  const { t } = useTranslation("lifecycle");
  return (
    <Stack gap={2} aria-live="polite">
      {result.import_errors.map((e, i) => (
        <Alert.Root key={i} status="error" size="sm">
          <Alert.Indicator />
          <Alert.Description fontSize="xs" whiteSpace="pre-wrap">
            {e}
          </Alert.Description>
        </Alert.Root>
      ))}
      {!result.dags.length && !result.import_errors.length ? (
        <Text fontSize="sm" color="fg.muted">
          {t("import.noDags")}
        </Text>
      ) : null}
      {result.dags.map((d) => (
        <Box key={d.dag_id} borderWidth="1px" rounded="md" p={2}>
          <HStack>
            <Text fontFamily="mono" fontSize="sm" fontWeight="semibold" flex="1">
              {d.dag_id}
            </Text>
            {d.warnings.length ? (
              <Badge colorPalette="orange">{t("import.warnings", { count: d.warnings.length })}</Badge>
            ) : null}
            {d.spec ? (
              <OpenButton spec={d.spec} onDone={onDone} />
            ) : (
              <Badge colorPalette="red">{t("import.notImportable")}</Badge>
            )}
          </HStack>
          {d.error ? (
            <Text fontSize="xs" color="red.500" whiteSpace="pre-wrap">
              {d.error}
            </Text>
          ) : null}
          {d.warnings.map((w, i) => (
            <Text key={i} fontSize="xs" color="orange.500">
              • {w}
            </Text>
          ))}
        </Box>
      ))}
    </Stack>
  );
}

function PythonTab({ onDone }: { onDone: () => void }) {
  const { t } = useTranslation("lifecycle");
  const [source, setSource] = useState("");
  const [filename, setFilename] = useState("dag.py");
  const imp = useMutation(importPythonDagMutation());
  const canRaw = editorAvailability(useAccess()).canRaw;
  return (
    <Stack gap={3}>
      <HStack>
        <Text fontSize="sm" flex="1" color="fg.muted">
          {t("import.pythonHelp")}
        </Text>
        <UploadButton
          accept=".py,text/x-python"
          onText={(text, name) => {
            setSource(text);
            setFilename(name);
          }}
        />
      </HStack>
      <CodeView value={source} onChange={setSource} language="python" height="260px" label={t("import.pythonSource")} />
      <HStack>
        <Button
          size="sm"
          colorPalette="blue"
          disabled={!source.trim()}
          loading={imp.isPending}
          onClick={() => imp.mutate({ body: { source, filename } })}
        >
          <LuDownload /> {t("import.analyze")}
        </Button>
        {canRaw ? (
          <Button
            size="sm"
            variant="outline"
            disabled={!source.trim()}
            title={t("import.asRawHelp")}
            data-testid="import-as-raw"
            onClick={() => {
              openAsRawDag(source, filename);
              onDone();
            }}
          >
            <LuFileCode /> {t("import.asRaw")}
          </Button>
        ) : null}
      </HStack>
      {imp.isError ? (
        <Text color="red.500" fontSize="sm">
          {errorMessage(imp.error)}
        </Text>
      ) : null}
      {imp.data ? <PythonResults result={imp.data} onDone={onDone} /> : null}
    </Stack>
  );
}

function AirflowTab({ initial, onDone }: { initial?: string | undefined; onDone: () => void }) {
  const { t } = useTranslation("lifecycle");
  const [dagId, setDagId] = useState(initial ?? "");
  const imp = useMutation(importAirflowDagMutation());
  return (
    <Stack gap={3}>
      <Text fontSize="sm" color="fg.muted">
        {t("import.airflowHelp")}
      </Text>
      <Field.Root>
        <Field.Label>{t("import.airflowDagId")}</Field.Label>
        <HStack w="100%">
          <Input
            size="sm"
            fontFamily="mono"
            value={dagId}
            onChange={(e) => setDagId(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && dagId.trim() && imp.mutate({ path: { dag_id: dagId.trim() } })}
          />
          <Button
            size="sm"
            colorPalette="blue"
            disabled={!dagId.trim()}
            loading={imp.isPending}
            onClick={() => imp.mutate({ path: { dag_id: dagId.trim() } })}
          >
            <LuDownload /> {t("import.fetch")}
          </Button>
        </HStack>
      </Field.Root>
      {imp.isError ? (
        <Text color="red.500" fontSize="sm">
          {errorMessage(imp.error)}
        </Text>
      ) : null}
      {imp.data ? <PythonResults result={imp.data} onDone={onDone} /> : null}
    </Stack>
  );
}

function YamlTab({ onDone }: { onDone: () => void }) {
  const { t } = useTranslation("lifecycle");
  const [source, setSource] = useState("");
  const imp = useMutation(importYamlDagMutation());
  return (
    <Stack gap={3}>
      <HStack>
        <Text fontSize="sm" flex="1" color="fg.muted">
          {t("import.yamlHelp")}
        </Text>
        <UploadButton accept=".yaml,.yml,application/yaml,text/yaml" onText={(text) => setSource(text)} />
      </HStack>
      <CodeView value={source} onChange={setSource} language="yaml" height="260px" label={t("import.yamlSource")} />
      <HStack>
        <Button
          size="sm"
          colorPalette="blue"
          disabled={!source.trim()}
          loading={imp.isPending}
          onClick={() => imp.mutate({ body: { source } })}
        >
          <LuDownload /> {t("import.analyze")}
        </Button>
      </HStack>
      {imp.isError ? (
        <Text color="red.500" fontSize="sm">
          {errorMessage(imp.error)}
        </Text>
      ) : null}
      {imp.data ? (
        <Stack gap={2} aria-live="polite">
          {imp.data.issues.map((i, n) => (
            <Text key={n} fontSize="xs" color={i.severity === "error" ? "red.500" : "orange.500"}>
              • {i.message}
            </Text>
          ))}
          <HStack>
            <Text fontFamily="mono" fontSize="sm" flex="1">
              {imp.data.spec.dag_id}
            </Text>
            <OpenButton spec={imp.data.spec} onDone={onDone} />
          </HStack>
        </Stack>
      ) : null}
    </Stack>
  );
}

function SpecTab({ onDone }: { onDone: () => void }) {
  const { t } = useTranslation("lifecycle");
  const [source, setSource] = useState("");
  const imp = useMutation(importSpecJsonMutation());
  return (
    <Stack gap={3}>
      <HStack>
        <Text fontSize="sm" flex="1" color="fg.muted">
          {t("import.specHelp")}
        </Text>
        <UploadButton accept=".json,application/json" onText={(text) => setSource(text)} />
      </HStack>
      <CodeView value={source} onChange={setSource} language="json" height="260px" label={t("import.specSource")} />
      <HStack>
        <Button
          size="sm"
          colorPalette="blue"
          disabled={!source.trim()}
          loading={imp.isPending}
          onClick={() => imp.mutate({ body: { source } })}
        >
          <LuDownload /> {t("import.analyze")}
        </Button>
      </HStack>
      {imp.isError ? (
        <Text color="red.500" fontSize="sm" whiteSpace="pre-wrap">
          {errorMessage(imp.error)}
        </Text>
      ) : null}
      {imp.data ? (
        <HStack aria-live="polite">
          <Text fontFamily="mono" fontSize="sm" flex="1">
            {imp.data.dag_id}
          </Text>
          <OpenButton spec={imp.data} onDone={onDone} />
        </HStack>
      ) : null}
    </Stack>
  );
}

/** Import a DAG from Python source, from Airflow, from Airflow YAML or from an AutoAir spec file. */
export default function ImportDialog({ source, dagId }: { source: ImportSource; dagId?: string | undefined }) {
  const { t } = useTranslation("lifecycle");
  const close = useLifecycle((s) => s.closeDialog);
  return (
    <DialogShell title={t("import.title")} description={t("import.description")} size="xl">
      <Tabs.Root defaultValue={source} size="sm" lazyMount>
        <Tabs.List>
          <Tabs.Trigger value="python">{t("import.python")}</Tabs.Trigger>
          <Tabs.Trigger value="airflow">{t("import.airflow")}</Tabs.Trigger>
          <Tabs.Trigger value="yaml">{t("import.yaml")}</Tabs.Trigger>
          <Tabs.Trigger value="spec">{t("import.spec")}</Tabs.Trigger>
        </Tabs.List>
        <Tabs.Content value="python">
          <PythonTab onDone={close} />
        </Tabs.Content>
        <Tabs.Content value="airflow">
          <AirflowTab initial={dagId} onDone={close} />
        </Tabs.Content>
        <Tabs.Content value="yaml">
          <YamlTab onDone={close} />
        </Tabs.Content>
        <Tabs.Content value="spec">
          <SpecTab onDone={close} />
        </Tabs.Content>
      </Tabs.Root>
    </DialogShell>
  );
}
