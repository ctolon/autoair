import { Badge, Box, Code, HStack, Input, NativeSelect, Spinner, Stack, Table, Text } from "@chakra-ui/react";
import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { getConfigOptions } from "../../api/gen/@tanstack/react-query.gen";
import type { ConfigOption } from "../../api/gen/types.gen";
import { errorMessage } from "../../api/setup";
import { Tip } from "../../shell/Tip";
import { DialogShell } from "../components/DialogShell";
import { featureName, literalChoices } from "../lib/configTypes";

const SOURCE_PALETTE: Record<string, string> = {
  default: "gray",
  env: "blue",
  dotenv: "cyan",
  init: "purple",
  secrets: "orange",
};

function show(value: unknown): string {
  if (value === undefined) return "—";
  if (typeof value === "string") return value;
  return JSON.stringify(value);
}

/** Literal types as chips (the current value highlighted); other types as text. */
function TypeCell({ type, value }: { type: string; value: unknown }) {
  const choices = literalChoices(type);
  if (!choices) return <>{type}</>;
  return (
    <HStack gap={1} wrap="wrap">
      {choices.map((c) => {
        const current = c === "None" ? value === null : value === c;
        return (
          <Badge key={c} size="xs" variant={current ? "solid" : "outline"} colorPalette={current ? "blue" : "gray"}>
            {c}
          </Badge>
        );
      })}
    </HStack>
  );
}

/** On/off of every feature flag with its environment variable. */
function FeaturesSummary({ options }: { options: ConfigOption[] }) {
  const { t } = useTranslation("lifecycle");
  const flags = options.filter((o) => o.section === "features" && typeof o.value === "boolean");
  if (!flags.length) return null;
  return (
    <Box borderWidth="1px" rounded="md" p={3} data-testid="config-features">
      <Text fontWeight="semibold" fontSize="sm" mb={2}>
        {t("config.features")}
      </Text>
      <HStack gap={1.5} wrap="wrap">
        {flags.map((o) => (
          <Tip key={o.key} content={`${o.env}=${o.value ? "true" : "false"}\n${o.description}`}>
            <Badge
              size="sm"
              variant={o.value ? "subtle" : "outline"}
              colorPalette={o.value ? "green" : "gray"}
              tabIndex={0}
              data-state={o.value ? "on" : "off"}
            >
              <Box as="span" w="6px" h="6px" rounded="full" bg={o.value ? "green.solid" : "gray.400"} />
              {featureName(o.key)}
              <Text as="span" fontSize="2xs" opacity={0.75}>
                {o.value ? t("config.on") : t("config.off")}
              </Text>
            </Badge>
          </Tip>
        ))}
      </HStack>
    </Box>
  );
}

/**
 * Effective AutoAir configuration, like Airflow's configuration page: every option with its
 * value, where it came from (default / env / file), type and default. Secrets come masked
 * (or are omitted with `expose_config = non-sensitive-only`).
 */
export default function ConfigDialog() {
  const { t } = useTranslation("lifecycle");
  const config = useQuery({ ...getConfigOptions(), retry: false });
  const [query, setQuery] = useState("");
  const [onlyChanged, setOnlyChanged] = useState("all");
  const sections = useMemo(() => {
    const q = query.trim().toLowerCase();
    const out = new Map<string, ConfigOption[]>();
    for (const o of config.data?.options ?? []) {
      if (onlyChanged === "changed" && o.source === "default") continue;
      if (q && ![o.key, o.env, o.description].some((s) => s.toLowerCase().includes(q))) continue;
      out.set(o.section || "core", [...(out.get(o.section || "core") ?? []), o]);
    }
    return [...out.entries()];
  }, [config.data, query, onlyChanged]);

  return (
    <DialogShell title={t("config.title")} description={t("config.help")} size="cover">
      {config.isPending ? <Spinner size="sm" /> : null}
      {config.error ? (
        <Text color="fg.error" fontSize="sm">
          {errorMessage(config.error)}
        </Text>
      ) : null}
      {config.data ? (
        <Stack gap={3}>
          <HStack gap={2} wrap="wrap">
            <Input
              size="sm"
              maxW="320px"
              placeholder={t("config.search")}
              aria-label={t("config.search")}
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
            <NativeSelect.Root size="sm" maxW="200px">
              <NativeSelect.Field
                aria-label={t("config.filter")}
                value={onlyChanged}
                onChange={(e) => setOnlyChanged(e.target.value)}
              >
                <option value="all">{t("config.all")}</option>
                <option value="changed">{t("config.changed")}</option>
              </NativeSelect.Field>
              <NativeSelect.Indicator />
            </NativeSelect.Root>
            {config.data.files.length ? (
              <Text fontSize="xs" color="fg.muted">
                {t("config.files")}: <Code fontSize="xs">{config.data.files.join(", ")}</Code>
              </Text>
            ) : null}
            {config.data.expose === "non-sensitive-only" ? (
              <Badge colorPalette="orange">{t("config.nonSensitive")}</Badge>
            ) : null}
          </HStack>
          <FeaturesSummary options={config.data.options} />
          {sections.map(([section, options]) => (
            <Box key={section}>
              <Text fontWeight="semibold" fontSize="sm" mb={1} fontFamily="mono">
                [{section}]
              </Text>
              <Box overflowX="auto" borderWidth="1px" rounded="md">
                <Table.Root size="sm" variant="line" data-testid={`config-${section}`}>
                  <Table.Header>
                    <Table.Row>
                      <Table.ColumnHeader>{t("config.key")}</Table.ColumnHeader>
                      <Table.ColumnHeader>{t("config.value")}</Table.ColumnHeader>
                      <Table.ColumnHeader>{t("config.type")}</Table.ColumnHeader>
                      <Table.ColumnHeader>{t("config.default")}</Table.ColumnHeader>
                      <Table.ColumnHeader>{t("config.source")}</Table.ColumnHeader>
                    </Table.Row>
                  </Table.Header>
                  <Table.Body>
                    {options.map((o) => (
                      <Table.Row key={o.key} verticalAlign="top">
                        <Table.Cell maxW="320px">
                          <Text fontFamily="mono" fontSize="xs" fontWeight="medium">
                            {o.key}
                          </Text>
                          <Text fontFamily="mono" fontSize="2xs" color="fg.muted">
                            {o.env}
                          </Text>
                          <Text fontSize="xs" color="fg.muted" mt={0.5}>
                            {o.description}
                          </Text>
                        </Table.Cell>
                        <Table.Cell fontFamily="mono" fontSize="xs" maxW="280px" wordBreak="break-all">
                          {o.secret ? (
                            <Badge colorPalette="red" size="xs" me={1}>
                              {t("config.secret")}
                            </Badge>
                          ) : null}
                          {show(o.value)}
                        </Table.Cell>
                        <Table.Cell fontFamily="mono" fontSize="xs">
                          <TypeCell type={o.type} value={o.value} />
                        </Table.Cell>
                        <Table.Cell fontFamily="mono" fontSize="xs" maxW="200px" wordBreak="break-all">
                          {o.default_text}
                        </Table.Cell>
                        <Table.Cell>
                          <Badge
                            size="xs"
                            colorPalette={o.source.startsWith("file:") ? "green" : (SOURCE_PALETTE[o.source] ?? "gray")}
                            title={o.source}
                          >
                            {o.source.startsWith("file:")
                              ? t("config.file")
                              : t(`config.sources.${o.source}`, { defaultValue: o.source })}
                          </Badge>
                        </Table.Cell>
                      </Table.Row>
                    ))}
                  </Table.Body>
                </Table.Root>
              </Box>
            </Box>
          ))}
        </Stack>
      ) : null}
    </DialogShell>
  );
}
