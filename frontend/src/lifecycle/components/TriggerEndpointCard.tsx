import { Box, Button, Code, HStack, Stack, Tabs, Text } from "@chakra-ui/react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { LuCheck, LuCopy, LuWebhook } from "react-icons/lu";

import { useBuilder } from "../../store/builder";
import { useMeta } from "../hooks";
import { paramDefaults } from "../lib/params";
import { buildSnippet, dagRunsUrl, SNIPPET_KINDS, type SnippetKind } from "../lib/triggerSnippets";

function CopyButton({ text, label }: { text: string; label: string }) {
  const { t } = useTranslation("lifecycle");
  const [copied, setCopied] = useState(false);
  return (
    <Button
      size="2xs"
      variant="ghost"
      aria-label={label}
      onClick={() => {
        void navigator.clipboard?.writeText(text).then(
          () => {
            setCopied(true);
            setTimeout(() => setCopied(false), 1500);
          },
          () => setCopied(false),
        );
      }}
    >
      {copied ? <LuCheck /> : <LuCopy />} {copied ? t("endpoint.copied") : t("endpoint.copy")}
    </Button>
  );
}

/**
 * How to trigger the deployed DAG from outside (webhook-style): Airflow's REST endpoint with
 * curl / Python / JS / CLI snippets, the conf prefilled from the DAG params' defaults.
 */
export function TriggerEndpointCard({ dagId }: { dagId: string }) {
  const { t } = useTranslation("lifecycle");
  const meta = useMeta();
  const params = useBuilder((s) => s.dag.params);
  const env = useBuilder((s) => s.environment);
  const envDefaults = useBuilder((s) => {
    const overlay = env ? s.dag.environments?.[env] : undefined;
    return overlay && typeof overlay === "object" && "params_defaults" in overlay
      ? (overlay as { params_defaults?: Record<string, unknown> }).params_defaults
      : undefined;
  });
  const [kind, setKind] = useState<SnippetKind>("curl");
  // Plugin mode runs inside the Airflow UI: its origin is Airflow.
  const baseUrl =
    meta?.airflow_public_url ??
    (meta?.mode === "plugin" && typeof window !== "undefined" ? window.location.origin : "http://localhost:8080");
  const conf = useMemo(() => ({ ...paramDefaults(params), ...envDefaults }), [params, envDefaults]);
  const target = { baseUrl, dagId, conf };
  const url = dagRunsUrl(baseUrl, dagId);
  return (
    <Box borderWidth="1px" rounded="md" p={3} data-testid="trigger-endpoint">
      <Stack gap={2}>
        <HStack gap={2}>
          <LuWebhook />
          <Text fontWeight="semibold" fontSize="sm">
            {t("endpoint.title")}
          </Text>
        </HStack>
        <HStack gap={2} wrap="wrap">
          <Code fontSize="xs" wordBreak="break-all">
            POST {url}
          </Code>
          <CopyButton text={url} label={t("endpoint.copyUrl")} />
        </HStack>
        <Tabs.Root size="sm" variant="line" value={kind} onValueChange={(e) => setKind(e.value as SnippetKind)}>
          <Tabs.List>
            {SNIPPET_KINDS.map((k) => (
              <Tabs.Trigger key={k} value={k}>
                {t(`endpoint.kinds.${k}`)}
              </Tabs.Trigger>
            ))}
          </Tabs.List>
        </Tabs.Root>
        <Box position="relative">
          <Code
            as="pre"
            display="block"
            fontSize="xs"
            whiteSpace="pre-wrap"
            wordBreak="break-all"
            p={2}
            pr={20}
            data-testid="trigger-snippet"
          >
            {buildSnippet(kind, target)}
          </Code>
          <Box position="absolute" top={1} right={1}>
            <CopyButton text={buildSnippet(kind, target)} label={t("endpoint.copySnippet")} />
          </Box>
        </Box>
        <Text fontSize="xs" color="fg.muted">
          {t(kind === "cli" ? "endpoint.cliNote" : "endpoint.permissionNote", { baseUrl })}
        </Text>
      </Stack>
    </Box>
  );
}
