import { Badge, Button, Collapsible, HStack, Link, Menu, Portal, Spinner, Stack, Text } from "@chakra-ui/react";
import { useQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import { useTranslation } from "react-i18next";
import { LuBookOpen, LuChevronDown, LuExternalLink, LuSparkles } from "react-icons/lu";
import { problemText } from "../../lib/problemMessages";

import { getOperatorOptions } from "../../api/gen/@tanstack/react-query.gen";
import type { OperatorDetail } from "../../api/gen/types.gen";
import { errorMessage } from "../../api/setup";
import type { JsonSchema } from "../../lib/formSchema";
import type { OperatorFlowNode } from "../../lib/types";
import { Hint, JsonField } from "../../widgets/fields";
import { ParamsForm } from "../../widgets/ParamsForm";
import { Docstring } from "./DocsTab";
import { HITL_PARAMS, HitlEditor } from "./HitlEditor";
import { LlmEditor } from "./LlmEditor";
import { LLM_PARAMS, isLlmSchema } from "../../lib/llm";
import { useNodeProblems, useUpdate } from "./shared";
import { useNodeTemplate } from "./TemplateBanner";

export function useOperatorDetail(fqn: string) {
  return useQuery({ ...getOperatorOptions({ path: { fqn } }), staleTime: 300_000 });
}

function OperatorHeader({ detail }: { detail: OperatorDetail }) {
  const { t } = useTranslation("inspector");
  return (
    <Stack gap={1}>
      <HStack gap={1} wrap="wrap">
        <Badge size="xs" variant="outline">
          {detail.provider}
          {detail.provider_version ? ` ${detail.provider_version}` : ""}
        </Badge>
        {detail.is_sensor ? (
          <Badge size="xs" colorPalette="purple">
            {t("operator.sensor")}
          </Badge>
        ) : null}
        {detail.hitl ? (
          <Badge size="xs" colorPalette="teal">
            {t("operator.hitl")}
          </Badge>
        ) : null}
        {detail.deprecated ? (
          <Badge size="xs" colorPalette="orange">
            {t("operator.deprecated")}
          </Badge>
        ) : null}
        {detail.required_airflow ? (
          <Badge size="xs" variant="outline" title={t("operator.requiredAirflow")}>
            Airflow {detail.required_airflow}
          </Badge>
        ) : null}
      </HStack>
      {detail.summary ? (
        <Text fontSize="xs" color="fg.muted">
          {detail.summary}
        </Text>
      ) : null}
      {detail.builder_supported === false ? (
        <Hint tone="warning">{detail.unsupported_reason ?? t("operator.unsupported")}</Hint>
      ) : null}
      {detail.requires_python_code ? (
        <Hint>
          {t(detail.equivalent_decorator ? "operator.pythonRefsAlt" : "operator.pythonRefs", {
            params: (detail.python_ref_params ?? []).join(", "),
            decorator: detail.equivalent_decorator === "task" ? "@task" : `@task.${detail.equivalent_decorator ?? ""}`,
          })}
        </Hint>
      ) : null}
    </Stack>
  );
}

/** Only http(s) documentation links are rendered. */
const safeUrl = (url: string | null | undefined) => (url && /^https?:\/\//i.test(url) ? url : null);

/**
 * Package metadata extras: the documentation link and the examples menu. Applying an example
 * merges its params into the node's (one undoable edit); params locked by a template stay.
 */
function OperatorExtras({
  detail,
  params,
  locked,
  onApply,
}: {
  detail: Pick<OperatorDetail, "docs_url" | "examples">;
  params: Record<string, unknown>;
  locked?: ReadonlySet<string>;
  onApply: (params: Record<string, unknown>) => void;
}) {
  const { t } = useTranslation("inspector");
  const docs = safeUrl(detail.docs_url);
  const examples = (detail.examples ?? []).filter((e) => e && typeof e.title === "string");
  if (!docs && !examples.length) return null;
  return (
    <HStack gap={2} wrap="wrap">
      {examples.length ? (
        <Menu.Root>
          <Menu.Trigger asChild>
            <Button size="xs" variant="outline" data-testid="operator-examples">
              <LuSparkles /> {t("operator.examples", { count: examples.length })} <LuChevronDown />
            </Button>
          </Menu.Trigger>
          <Portal>
            <Menu.Positioner>
              <Menu.Content maxW="sm">
                {examples.map((ex, i) => (
                  <Menu.Item
                    key={`${i}-${ex.title}`}
                    value={`example-${i}`}
                    onSelect={() => {
                      const next: Record<string, unknown> = { ...params };
                      for (const [k, v] of Object.entries(ex.params ?? {})) if (!locked?.has(k)) next[k] = v;
                      onApply(next);
                    }}
                  >
                    <Stack gap={0}>
                      <Text fontSize="sm">{ex.title}</Text>
                      {ex.description ? (
                        <Text fontSize="xs" color="fg.muted">
                          {ex.description}
                        </Text>
                      ) : null}
                    </Stack>
                  </Menu.Item>
                ))}
              </Menu.Content>
            </Menu.Positioner>
          </Portal>
        </Menu.Root>
      ) : null}
      {docs ? (
        <Link
          href={docs}
          target="_blank"
          rel="noopener noreferrer"
          fontSize="xs"
          color="fg.info"
          data-testid="operator-docs-url"
        >
          <LuExternalLink /> {t("operator.docsLink")}
        </Link>
      ) : null}
    </HStack>
  );
}

/** "Parameters" tab of an operator node. */
export function OperatorParams({ node }: { node: OperatorFlowNode }) {
  const update = useUpdate<"operator">(node.id);
  const { operator, params = {} } = node.data.spec;
  const problems = useNodeProblems(node.id);
  const errors = useMemo(() => {
    const out: Record<string, string> = {};
    for (const p of problems)
      if ((p.code === "param" || p.code.startsWith("template_")) && p.field && !out[p.field])
        out[p.field] = problemText(p);
    return out;
  }, [problems]);
  const { locks } = useNodeTemplate(node.id);
  const lockedParams = useMemo(
    () => new Set([...locks.keys()].filter((p) => p.startsWith("params.")).map((p) => p.slice("params.".length))),
    [locks],
  );
  return (
    <OperatorParamsForm
      nodeId={node.id}
      operator={operator}
      params={params}
      errors={errors}
      locked={lockedParams}
      onChange={(v) => update({ params: v })}
    />
  );
}

/**
 * An operator's parameter editor without the canvas: catalog header, examples, docs, HITL
 * and LLM editors, the params form and the raw JSON. Used by the inspector and by the
 * operator template editor (`nodeId` only feeds upstream XCom suggestions).
 */
export function OperatorParamsForm({
  nodeId,
  operator,
  params,
  errors,
  locked,
  onChange,
  allowXcom = true,
}: {
  nodeId: string;
  operator: string;
  params: Record<string, unknown>;
  errors: Record<string, string>;
  locked?: ReadonlySet<string>;
  onChange: (params: Record<string, unknown>) => void;
  allowXcom?: boolean;
}) {
  const { t } = useTranslation("inspector");
  const detail = useOperatorDetail(operator);
  const lockedParams = locked;
  const update = (patch: { params: Record<string, unknown> }) => onChange(patch.params);
  const d = detail.data;
  const schema = d?.params_schema as JsonSchema | undefined;
  const hitlHandled = useMemo(
    () => (d?.hitl && schema ? HITL_PARAMS.filter((p) => !!schema.properties?.[p]) : []),
    [d?.hitl, schema],
  );
  const llmHandled = useMemo(
    () => (schema && isLlmSchema(schema.properties) ? LLM_PARAMS.filter((p) => !!schema.properties?.[p]) : []),
    [schema],
  );

  return (
    <Stack gap={3}>
      {detail.isPending ? <Spinner size="sm" /> : null}
      {detail.isError ? <Hint tone="error">{errorMessage(detail.error)}</Hint> : null}
      {d ? <OperatorHeader detail={d} /> : null}
      {d ? (
        <OperatorExtras detail={d} params={params} locked={lockedParams} onApply={(v) => update({ params: v })} />
      ) : null}
      {d?.docstring ? (
        <Collapsible.Root>
          <Collapsible.Trigger asChild>
            <Button size="xs" variant="ghost" px={1} alignSelf="start">
              <LuBookOpen /> {t("operator.showDocs")} <LuChevronDown />
            </Button>
          </Collapsible.Trigger>
          <Collapsible.Content>
            <Docstring text={d.docstring} title={t("docs.docstring")} />
          </Collapsible.Content>
        </Collapsible.Root>
      ) : null}
      {d && schema && d.hitl ? (
        <HitlEditor
          nodeId={nodeId}
          schema={schema}
          value={params}
          errors={errors}
          onChange={(v) => update({ params: v })}
        />
      ) : null}
      {d && schema && llmHandled.length ? (
        <LlmEditor
          fields={new Set<string>(llmHandled)}
          value={params}
          errors={errors}
          required={schema.required ?? []}
          onChange={(v) => update({ params: v })}
        />
      ) : null}
      {d && schema ? (
        <ParamsForm
          schema={schema}
          value={params}
          onChange={(v) => update({ params: v })}
          isSensor={d.is_sensor}
          deferrable={d.deferrable_param}
          connectionTypes={d.connection_types ?? []}
          exclude={[...hitlHandled, ...llmHandled]}
          errors={errors}
          locked={lockedParams}
          allowXcom={allowXcom}
        />
      ) : null}
      <Collapsible.Root>
        <Collapsible.Trigger asChild>
          <Button size="xs" variant="ghost" px={1} alignSelf="start">
            <LuChevronDown /> {t("operator.rawParams")}
          </Button>
        </Collapsible.Trigger>
        <Collapsible.Content>
          <JsonField
            label="params"
            help={t("operator.rawParamsHelp")}
            rows={6}
            value={params}
            validate={(v) => (v && typeof v === "object" && !Array.isArray(v) ? undefined : t("common.mustBeObject"))}
            onChange={(v) => update({ params: (v ?? {}) as Record<string, unknown> })}
          />
        </Collapsible.Content>
      </Collapsible.Root>
    </Stack>
  );
}
