import {
  Badge,
  Box,
  Button,
  Card,
  Code,
  HStack,
  Input,
  InputGroup,
  SimpleGrid,
  Spinner,
  Stack,
  Text,
} from "@chakra-ui/react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { LuBadgeCheck, LuDownload, LuRefreshCw, LuSearch, LuShieldAlert } from "react-icons/lu";
import { useAccess } from "../../lib/access";

import {
  installMarketplaceEntryMutation,
  listMarketplaceOptions,
  listMarketplaceQueryKey,
  listTemplatesQueryKey,
} from "../../api/gen/@tanstack/react-query.gen";
import type { ListedEntry } from "../../api/gen/types.gen";
import { errorMessage } from "../../api/setup";
import { toaster } from "../../ui/toaster";

const matches = (e: ListedEntry, q: string) =>
  !q ||
  q
    .toLowerCase()
    .split(/\s+/)
    .every((w) =>
      [e.id, e.title, e.description ?? "", e.category ?? "", ...(e.tags ?? [])].join(" ").toLowerCase().includes(w),
    );

/** Signed template indexes configured by the administrator (AUTOAIR_MARKETPLACE_SOURCES). */
export function MarketplacePanel({ onInstalled }: { onInstalled: (templateId: string) => void }) {
  const { t } = useTranslation("lifecycle");
  const qc = useQueryClient();
  const [q, setQ] = useState("");
  const [refresh, setRefresh] = useState(false);
  const listing = useQuery({ ...listMarketplaceOptions({ query: { refresh } }), retry: false });
  const canInstall = useAccess().can("template:write");
  const install = useMutation({
    ...installMarketplaceEntryMutation(),
    onSuccess: async (tpl) => {
      await qc.invalidateQueries({ queryKey: listTemplatesQueryKey() });
      await qc.invalidateQueries({ queryKey: listMarketplaceQueryKey() });
      toaster.create({ type: "success", title: t("marketplace.installed", { title: tpl.title }) });
      onInstalled(tpl.id);
    },
    onError: (err) =>
      toaster.create({ type: "error", title: t("marketplace.installFailed"), description: errorMessage(err) }),
  });
  const entries = useMemo(() => (listing.data?.entries ?? []).filter((e) => matches(e, q)), [listing.data, q]);

  if (listing.isPending) return <Spinner size="sm" />;
  if (listing.isError) return <Text color="red.500">{errorMessage(listing.error)}</Text>;
  const sources = listing.data.sources;

  return (
    <Stack gap={3}>
      <HStack wrap="wrap">
        <InputGroup startElement={<LuSearch />} flex="1" minW="200px">
          <Input
            size="sm"
            value={q}
            placeholder={t("marketplace.search")}
            aria-label={t("marketplace.search")}
            onChange={(e) => setQ(e.target.value)}
          />
        </InputGroup>
        <Button size="sm" variant="ghost" onClick={() => setRefresh((r) => !r)} aria-label={t("marketplace.refresh")}>
          <LuRefreshCw /> {t("marketplace.refresh")}
        </Button>
      </HStack>
      {!sources.length ? (
        <Text fontSize="sm" color="fg.muted">
          {t("marketplace.noSources")}
        </Text>
      ) : null}
      {sources.map((s) => (
        <HStack key={s.name} fontSize="xs" gap={2}>
          {s.ok && s.verified ? <LuBadgeCheck color="green" /> : <LuShieldAlert color={s.ok ? "orange" : "red"} />}
          <Text fontWeight="semibold">{s.name}</Text>
          <Text color="fg.muted" truncate>
            {s.ok ? t(s.verified ? "marketplace.verified" : "marketplace.unsigned", { count: s.entries }) : s.error}
          </Text>
        </HStack>
      ))}
      <SimpleGrid columns={{ base: 1, md: 2, xl: 3 }} gap={2}>
        {entries.map((e) => (
          <Card.Root key={`${e.source}/${e.id}`} size="sm" variant="outline" data-testid="marketplace-entry">
            <Card.Body gap={1}>
              <HStack>
                <Text fontWeight="semibold" fontSize="sm" flex="1" truncate>
                  {e.title}
                </Text>
                <Badge size="xs" colorPalette={e.kind === "dag" ? "blue" : e.kind === "fragment" ? "purple" : "gray"}>
                  {t(`marketplace.kind.${e.kind}`)}
                </Badge>
              </HStack>
              {e.description ? (
                <Text fontSize="xs" color="fg.muted" lineClamp={2}>
                  {e.description}
                </Text>
              ) : null}
              <HStack gap={1} wrap="wrap" fontSize="2xs" color="fg.muted">
                <Text>v{e.version}</Text>
                {e.author ? <Text>· {e.author}</Text> : null}
                <Text>· {e.source}</Text>
                {e.min_airflow ? <Badge size="xs">Airflow ≥ {e.min_airflow}</Badge> : null}
              </HStack>
              <Box pt={1}>
                {e.kind === "extension" ? (
                  <Code fontSize="2xs" title={t("marketplace.pipHint")}>
                    pip install {e.package}
                  </Code>
                ) : (
                  <Button
                    size="xs"
                    variant={e.installed ? "outline" : "solid"}
                    loading={install.isPending && install.variables?.body.entry_id === e.id}
                    disabled={!canInstall}
                    title={canInstall ? undefined : t("access.denied", { permission: "template:write" })}
                    onClick={() => install.mutate({ body: { source: e.source, entry_id: e.id } })}
                  >
                    <LuDownload /> {t(e.installed ? "marketplace.update" : "marketplace.install")}
                  </Button>
                )}
              </Box>
            </Card.Body>
          </Card.Root>
        ))}
      </SimpleGrid>
    </Stack>
  );
}
