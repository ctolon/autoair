import { useMemo } from "react";
import { Badge, Box, Button, Code, Field, HStack, Stack, Text } from "@chakra-ui/react";
import { useQuery } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { LuSparkles } from "react-icons/lu";
import { useShallow } from "zustand/react/shallow";

import { listDecoratorsOptions } from "../../api/gen/@tanstack/react-query.gen";
import type { PythonTaskNode } from "../../api/gen/types.gen";
import { withoutKey } from "../../lib/formSchema";
import { isLlmDecorator, isOutputModel, isToolset, llmDecoratorKwargs } from "../../lib/llm";
import type { PythonTaskFlowNode } from "../../lib/types";
import { detectReturnKeys, downstreamTasks, effectiveTaskId } from "../../lib/xcom";
import { useAiStatus } from "../../lifecycle/lib/ai";
import { useLifecycle } from "../../lifecycle/store";
import { useBuilder } from "../../store/builder";
import { CodeEditor } from "../../widgets/code";
import {
  FIELD_ATTR,
  Hint,
  JsonField,
  NumberField,
  SectionTitle,
  SelectField,
  StringListField,
  SwitchField,
  TextField,
} from "../../widgets/fields";
import { useAirflowGate } from "../../widgets/airflowMeta";
import { ConnectionPicker } from "../../widgets/AirflowPickers";
import { LlmEditor } from "./LlmEditor";
import { ArgsEditor } from "../../widgets/ValueEditors";
import { VersionBadge } from "../../widgets/VersionBadge";
import { useFieldError, useUpdate } from "./shared";

type Decorator = NonNullable<PythonTaskNode["decorator"]>;

const DECORATORS: Decorator[] = [
  "task",
  "branch",
  "short_circuit",
  "virtualenv",
  "branch_virtualenv",
  "external_python",
  "branch_external_python",
  "sensor",
  "bash",
  "docker",
  "kubernetes",
  "stub",
  "llm",
  "agent",
  "llm_branch",
  "llm_sql",
];

/** decorator_kwargs keys with a dedicated control, per decorator. */
const KNOWN_KWARGS: Record<Decorator, string[]> = {
  task: [],
  branch: [],
  short_circuit: ["ignore_downstream_trigger_rules"],
  virtualenv: ["requirements", "python_version", "system_site_packages"],
  branch_virtualenv: ["requirements", "python_version", "system_site_packages"],
  external_python: ["python"],
  branch_external_python: ["python"],
  sensor: ["poke_interval", "timeout", "mode", "soft_fail"],
  bash: [],
  docker: ["image", "command", "docker_url", "auto_remove"],
  kubernetes: ["image", "namespace", "name", "in_cluster"],
  stub: ["queue"],
  llm: llmDecoratorKwargs("llm"),
  agent: llmDecoratorKwargs("agent"),
  llm_branch: llmDecoratorKwargs("llm_branch"),
  llm_sql: llmDecoratorKwargs("llm_sql"),
};

const IDENT = /^[A-Za-z_][A-Za-z0-9_]*$/;

const pyLiteral = (v: unknown): string => {
  if (v === null || v === undefined) return "None";
  if (typeof v === "boolean") return v ? "True" : "False";
  if (typeof v === "string") return JSON.stringify(v);
  if (typeof v === "object" && (v as { $type?: unknown }).$type === "xcom") return "<XCom>";
  if (isOutputModel(v)) return v.name;
  if (Array.isArray(v) && v.some(isToolset))
    return `[${v.map((x) => (isToolset(x) ? (x.kind === "sql" ? "SQLToolset(…)" : "MCPToolset(…)") : "…")).join(", ")}]`;
  return JSON.stringify(v);
};

/** `@task.virtualenv(requirements=[...])` + `def fn(a, b, *, extra):` header lines. */
function signatureHeader(spec: {
  decorator?: Decorator | undefined;
  decorator_kwargs?: Record<string, unknown> | undefined;
  multiple_outputs?: boolean | undefined;
  function_name: string;
  arguments?: Record<string, unknown> | undefined;
  extra_parameters?: string[] | undefined;
  mapping?: { expand?: Record<string, unknown> } | null | undefined;
  is_result?: boolean | undefined;
  uses_loop_context?: boolean | undefined;
}): string {
  const dec = spec.decorator ?? "task";
  const kwargs = Object.entries(spec.decorator_kwargs ?? {}).map(([k, v]) => `${k}=${pyLiteral(v)}`);
  if (spec.multiple_outputs) kwargs.push("multiple_outputs=True");
  const decLine = `@task${dec === "task" ? "" : `.${dec}`}${kwargs.length ? `(${kwargs.join(", ")})` : ""}`;
  const params = [
    ...new Set([
      ...Object.keys(spec.arguments ?? {}),
      ...Object.keys(spec.mapping?.expand ?? {}),
      ...(spec.extra_parameters ?? []),
    ]),
  ];
  if (spec.uses_loop_context) params.push("*", "loop=None");
  const result = spec.is_result ? "@result\n" : "";
  return `${result}${decLine}\ndef ${spec.function_name || "fn"}(${params.join(", ")}):`;
}

function DecoratorKwargs({ node }: { node: PythonTaskFlowNode }) {
  const { t } = useTranslation("inspector");
  const update = useUpdate<"python_task">(node.id);
  const spec = node.data.spec;
  const dec = spec.decorator ?? "task";
  const kw = spec.decorator_kwargs ?? {};
  const error = useFieldError(node.id, "decorator_kwargs");
  const set = (k: string, v: unknown) => {
    const empty = v === undefined || v === null || v === "" || (Array.isArray(v) && !v.length);
    update({ decorator_kwargs: empty ? withoutKey(kw, k) : { ...kw, [k]: v } });
  };
  const str = (k: string) => (typeof kw[k] === "string" ? (kw[k] as string) : "");
  const num = (k: string) => (typeof kw[k] === "number" ? (kw[k] as number) : null);
  const known = KNOWN_KWARGS[dec];
  const others = Object.fromEntries(Object.entries(kw).filter(([k]) => !known.includes(k)));

  return (
    <Stack gap={2} {...{ [FIELD_ATTR]: "decorator_kwargs" }}>
      {error ? <Hint tone="error">{error}</Hint> : null}
      {dec === "virtualenv" || dec === "branch_virtualenv" ? (
        <>
          <StringListField
            label={t("python.requirements")}
            help={t("python.requirementsHelp")}
            mono
            placeholder="pandas==2.2.*"
            value={Array.isArray(kw.requirements) ? kw.requirements.map(String) : []}
            onChange={(v) => set("requirements", v)}
          />
          <HStack align="start">
            <TextField
              help={t("python.pythonVersionHelp")}
              label={t("python.pythonVersion")}
              mono
              placeholder="3.12"
              value={str("python_version")}
              onChange={(v) => set("python_version", v)}
            />
            <Box pt={6}>
              <SwitchField
                help={t("python.systemSitePackagesHelp")}
                label={t("python.systemSitePackages")}
                checked={kw.system_site_packages === true}
                onChange={(v) => set("system_site_packages", v || undefined)}
              />
            </Box>
          </HStack>
        </>
      ) : null}
      {dec === "external_python" || dec === "branch_external_python" ? (
        <TextField
          label={t("python.pythonPath")}
          help={t("python.pythonPathHelp")}
          required
          mono
          placeholder="/opt/venvs/etl/bin/python"
          value={str("python")}
          onChange={(v) => set("python", v)}
        />
      ) : null}
      {dec === "docker" ? (
        <>
          <TextField
            help={t("python.imageHelp")}
            label={t("python.image")}
            required
            mono
            placeholder="python:3.12-slim"
            value={str("image")}
            onChange={(v) => set("image", v)}
          />
          <TextField
            help={t("python.dockerUrlHelp")}
            label={t("python.dockerUrl")}
            mono
            placeholder="unix://var/run/docker.sock"
            value={str("docker_url")}
            onChange={(v) => set("docker_url", v)}
          />
          <TextField
            label={t("python.command")}
            help={t("python.commandHelp")}
            mono
            value={str("command")}
            onChange={(v) => set("command", v)}
          />
          <SwitchField
            help={t("python.autoRemoveHelp")}
            label={t("python.autoRemove")}
            checked={kw.auto_remove === "success" || kw.auto_remove === true}
            onChange={(v) => set("auto_remove", v ? "success" : undefined)}
          />
        </>
      ) : null}
      {dec === "kubernetes" ? (
        <>
          <TextField
            help={t("python.imageHelp")}
            label={t("python.image")}
            required
            mono
            placeholder="python:3.12-slim"
            value={str("image")}
            onChange={(v) => set("image", v)}
          />
          <HStack align="start">
            <TextField
              help={t("python.namespaceHelp")}
              label={t("python.namespace")}
              mono
              placeholder="default"
              value={str("namespace")}
              onChange={(v) => set("namespace", v)}
            />
            <TextField
              help={t("python.podNameHelp")}
              label={t("python.podName")}
              mono
              value={str("name")}
              onChange={(v) => set("name", v)}
            />
          </HStack>
          <SwitchField
            help={t("python.inClusterHelp")}
            label={t("python.inCluster")}
            checked={kw.in_cluster !== false}
            onChange={(v) => set("in_cluster", v ? undefined : false)}
          />
        </>
      ) : null}
      {dec === "sensor" ? (
        <>
          <HStack align="start">
            <NumberField
              dtype="timedelta (s)"
              help={t("python.pokeIntervalHelp")}
              label={t("python.pokeInterval")}
              min={1}
              placeholder="60"
              value={num("poke_interval")}
              onChange={(v) => set("poke_interval", v)}
            />
            <NumberField
              dtype="timedelta (s)"
              help={t("python.sensorTimeoutHelp")}
              label={t("python.sensorTimeout")}
              min={1}
              placeholder="604800"
              value={num("timeout")}
              onChange={(v) => set("timeout", v)}
            />
          </HStack>
          <SelectField
            help={t("python.modeHelp")}
            label={t("python.mode")}
            value={str("mode") || null}
            emptyLabel={t("python.modeDefault")}
            options={[
              { value: "poke", label: t("python.modes.poke") },
              { value: "reschedule", label: t("python.modes.reschedule") },
            ]}
            onChange={(v) => set("mode", v)}
          />
          <SwitchField
            help={t("python.softFailHelp")}
            label={t("python.softFail")}
            checked={kw.soft_fail === true}
            onChange={(v) => set("soft_fail", v || undefined)}
          />
          <Hint>{t("python.sensorHint")}</Hint>
        </>
      ) : null}
      {dec === "stub" ? (
        <TextField
          label={t("python.queue")}
          help={t("python.queueHelp")}
          required
          mono
          placeholder="java"
          value={str("queue")}
          onChange={(v) => set("queue", v)}
        />
      ) : null}
      {isLlmDecorator(dec) ? (
        <>
          <Hint>{t("llm.decoratorHint")}</Hint>
          <ConnectionPicker
            label={t("llm.llmConnection")}
            help={t("llm.llmConnectionHelp")}
            required
            field="decorator_kwargs.llm_conn_id"
            placeholder="pydanticai_default"
            value={str("llm_conn_id")}
            onChange={(v) => set("llm_conn_id", v)}
          />
          {dec === "llm_sql" ? (
            <ConnectionPicker
              label={t("llm.dbConnection")}
              value={str("db_conn_id")}
              onChange={(v) => set("db_conn_id", v)}
            />
          ) : null}
          <TextField
            label={t("llm.modelId")}
            help={t("llm.modelIdHelp")}
            mono
            placeholder="openai:gpt-5"
            value={str("model_id")}
            onChange={(v) => set("model_id", v)}
          />
          <LlmEditor fields={new Set(known)} value={kw} onChange={(v) => update({ decorator_kwargs: v })} />
        </>
      ) : null}
      {dec === "short_circuit" ? (
        <SwitchField
          label={t("python.ignoreDownstreamTriggerRules")}
          help={t("python.ignoreDownstreamTriggerRulesHelp")}
          checked={kw.ignore_downstream_trigger_rules !== false}
          onChange={(v) => set("ignore_downstream_trigger_rules", v ? undefined : false)}
        />
      ) : null}
      <JsonField
        label={t("python.otherKwargs")}
        help={t("python.otherKwargsHelp")}
        rows={2}
        value={Object.keys(others).length ? others : undefined}
        validate={(v) => (v && typeof v === "object" && !Array.isArray(v) ? undefined : t("common.mustBeObject"))}
        onChange={(v) => {
          const knownPart = Object.fromEntries(Object.entries(kw).filter(([k]) => known.includes(k)));
          update({ decorator_kwargs: { ...knownPart, ...((v as Record<string, unknown> | undefined) ?? {}) } });
        }}
      />
    </Stack>
  );
}

function BranchHint({ node }: { node: PythonTaskFlowNode }) {
  const { t } = useTranslation("inspector");
  const { nodes, edges } = useBuilder(useShallow((s) => ({ nodes: s.nodes, edges: s.edges })));
  const ids = downstreamTasks(nodes, edges, node.id).map((n) => effectiveTaskId(nodes, n.id));
  return (
    <Box bg="bg.subtle" rounded="md" p={2}>
      <Text fontSize="xs">{t("python.branchHint")}</Text>
      {ids.length ? (
        <HStack wrap="wrap" gap={1} mt={1}>
          {ids.map((id) => (
            <Code key={id} fontSize="xs">
              {JSON.stringify(id)}
            </Code>
          ))}
        </HStack>
      ) : (
        <Hint tone="warning">{t("python.branchNoDownstream")}</Hint>
      )}
    </Box>
  );
}

/** "Code" tab of a Python (TaskFlow) task. */
export function PythonEditor({ node }: { node: PythonTaskFlowNode }) {
  const { t } = useTranslation("inspector");
  const update = useUpdate<"python_task">(node.id);
  const spec = node.data.spec;
  const codeError = useFieldError(node.id, "code");
  const fnError = useFieldError(node.id, "function_name");
  const decError = useFieldError(node.id, "decorator");
  const decorators = useQuery({ ...listDecoratorsOptions(), retry: false, staleTime: 300_000 });
  const installed = new Map((decorators.data ?? []).map((d) => [d.name, d]));
  const dec = spec.decorator ?? "task";
  const keys = spec.multiple_outputs ? detectReturnKeys(spec.code) : [];
  const badParams = (spec.extra_parameters ?? []).filter((p) => !IDENT.test(p));
  const gate = useAirflowGate();
  const ai = useAiStatus();
  const inLoop = useBuilder((s) => {
    const parent = s.nodes.find((n) => n.id === node.parentId);
    return parent?.type === "task_group" && !!parent.data.spec.loop;
  });
  // Select the store's own problem objects (stable references, so useShallow can compare them)
  // and derive editor diagnostics outside the selector: building new objects inside it made
  // every read look like a change → "Maximum update depth exceeded" as soon as lint reported.
  const codeProblems = useBuilder(
    useShallow((s) => s.problems.filter((p) => p.node_id === node.id && p.field === "code" && p.line != null)),
  );
  const lint = useMemo(
    () =>
      codeProblems.map((p) => ({
        line: p.line ?? 1,
        column: p.column ?? null,
        message: p.message.replace(/^line \d+: /, ""),
        severity: p.severity,
      })),
    [codeProblems],
  );

  return (
    <Stack gap={2}>
      <SelectField
        label={t("python.decorator")}
        field="decorator"
        error={decError}
        value={dec}
        options={DECORATORS.map((d) => {
          // "task" is core; others need their provider (unknown while the catalog loads).
          const available = d === "task" || !decorators.data || installed.has(d);
          const info = installed.get(d);
          return {
            value: d,
            label: `@task${d === "task" ? "" : `.${d}`}${available ? "" : ` — ${t("common.unavailable")}`}${info ? ` (${info.provider})` : ""}`,
            disabled: !available && d !== dec,
          };
        })}
        help={t(`python.decorators.${dec}`)}
        onChange={(v) => update({ decorator: v ?? "task" })}
      />
      {dec === "branch" || dec === "branch_virtualenv" || dec === "branch_external_python" ? (
        <BranchHint node={node} />
      ) : null}
      {dec === "bash" ? <Hint>{t("python.bashHint")}</Hint> : null}
      <DecoratorKwargs node={node} />

      <SectionTitle>{t("python.function")}</SectionTitle>
      <TextField
        help={t("python.functionNameHelp")}
        label={t("python.functionName")}
        field="function_name"
        mono
        required
        value={spec.function_name}
        error={fnError}
        onChange={(v) => update({ function_name: v ?? "" })}
      />
      <ArgsEditor
        nodeId={node.id}
        title={t("python.arguments")}
        fieldPrefix="arguments"
        help={t("python.argumentsHelp")}
        value={spec.arguments ?? {}}
        onChange={(args) => update({ arguments: args })}
      />
      <StringListField
        label={t("python.extraParameters")}
        help={t("python.extraParametersHelp")}
        field="extra_parameters"
        mono
        error={badParams.length ? t("python.invalidIdentifiers", { names: badParams.join(", ") }) : undefined}
        value={spec.extra_parameters ?? []}
        onChange={(v) => update({ extra_parameters: v })}
      />
      <Field.Root invalid={!!codeError} {...{ [FIELD_ATTR]: "code" }}>
        <HStack w="100%" justify="space-between">
          <Field.Label fontSize="xs">{t("python.body")}</Field.Label>
          {ai?.configured ? (
            <Button
              size="2xs"
              variant="ghost"
              title={t("python.aiCodeHelp")}
              onClick={() => useLifecycle.getState().openDialog({ kind: "aiTaskCode", nodeId: node.id })}
            >
              <LuSparkles /> {t("python.aiCode")}
            </Button>
          ) : null}
        </HStack>
        <Box w="100%" borderWidth="1px" rounded="md" overflow="hidden">
          <Box
            as="pre"
            fontFamily="mono"
            fontSize="xs"
            px={2}
            py={1}
            bg="bg.muted"
            color="fg.muted"
            whiteSpace="pre-wrap"
            aria-label={t("python.signature")}
          >
            {signatureHeader(spec)}
          </Box>
          <CodeEditor
            language="python"
            diagnostics={lint}
            pythonLsp={{
              functionName: spec.function_name,
              arguments: [
                ...Object.keys(spec.arguments ?? {}),
                ...Object.keys(spec.mapping?.expand ?? {}),
                ...(spec.extra_parameters ?? []),
              ],
              usesLoopContext: !!spec.uses_loop_context,
            }}
            ariaLabel={t("python.body")}
            minHeight="200px"
            maxHeight="520px"
            invalid={!!codeError}
            value={spec.code}
            onChange={(code) => update({ code })}
          />
        </Box>
        {codeError ? (
          <Field.ErrorText>{codeError}</Field.ErrorText>
        ) : (
          <Field.HelperText fontSize="xs">{t("python.bodyHelp")}</Field.HelperText>
        )}
      </Field.Root>
      {dec === "task" ? (
        <HStack gap={2} align="start">
          <SwitchField
            label={t("python.isResult")}
            help={t("python.isResultHelp")}
            field="is_result"
            checked={!!spec.is_result}
            onChange={(v) => update({ is_result: v })}
          />
          {!gate.supports("3.3.0") ? <VersionBadge min="3.3" /> : null}
        </HStack>
      ) : null}
      {inLoop || spec.uses_loop_context ? (
        <SwitchField
          label={t("python.usesLoopContext")}
          help={t("python.usesLoopContextHelp")}
          field="uses_loop_context"
          checked={!!spec.uses_loop_context}
          onChange={(v) => update({ uses_loop_context: v })}
        />
      ) : null}
      <SwitchField
        label={t("python.multipleOutputs")}
        help={t("python.multipleOutputsHelp")}
        field="multiple_outputs"
        checked={!!spec.multiple_outputs}
        onChange={(v) => update({ multiple_outputs: v })}
      />
      {spec.multiple_outputs ? (
        keys.length ? (
          <HStack wrap="wrap" gap={1}>
            <Text fontSize="xs" color="fg.muted">
              {t("python.detectedKeys")}
            </Text>
            {keys.map((k) => (
              <Badge key={k} size="xs" variant="outline" fontFamily="mono">
                {k}
              </Badge>
            ))}
          </HStack>
        ) : (
          <Hint>{t("python.noKeysDetected")}</Hint>
        )
      ) : null}
    </Stack>
  );
}
