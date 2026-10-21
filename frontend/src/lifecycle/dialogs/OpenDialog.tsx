import {
  Badge,
  Box,
  Button,
  chakra,
  HStack,
  Input,
  InputGroup,
  NativeSelect,
  Spinner,
  Stack,
  Text,
} from "@chakra-ui/react";
import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { LuFilePlus, LuSearch, LuWorkflow } from "react-icons/lu";

import { listSpecsOptions } from "../../api/gen/@tanstack/react-query.gen";
import type { SpecSummary } from "../../api/gen/types.gen";
import { errorMessage } from "../../api/setup";
import { RawBadge } from "../../codemode/RawCodeView";
import { useBuilder } from "../../store/builder";
import { newDag, openDag } from "../actions";
import { DialogShell } from "../components/DialogShell";
import { useLifecycle } from "../store";

function matches(s: SpecSummary, q: string): boolean {
  if (!q) return true;
  const hay = [s.dag_id, s.display_name ?? "", s.description ?? "", ...(s.tags ?? [])].join(" ").toLowerCase();
  return q
    .toLowerCase()
    .split(/\s+/)
    .every((w) => hay.includes(w));
}

type TypeFilter = "all" | "spec" | "raw";

const typeOf = (s: SpecSummary) => (s.document_type === "raw" ? "raw" : "spec");

/** Searchable list of the stored specs. */
export default function OpenDialog() {
  const { t, i18n } = useTranslation("lifecycle");
  const specs = useQuery(listSpecsOptions());
  const current = useBuilder((s) => s.savedDagId);
  const pendingPr = useBuilder((s) => s.pendingPrUrl);
  const [q, setQ] = useState("");
  const [type, setType] = useState<TypeFilter>("all");
  const [busy, setBusy] = useState<string | null>(null);
  const close = useLifecycle((s) => s.closeDialog);
  const rows = useMemo(
    () => (specs.data ?? []).filter((s) => matches(s, q) && (type === "all" || typeOf(s) === type)),
    [specs.data, q, type],
  );
  const dateFmt = useMemo(
    () => new Intl.DateTimeFormat(i18n.language, { dateStyle: "medium", timeStyle: "short" }),
    [i18n.language],
  );

  const open = async (dagId: string) => {
    setBusy(dagId);
    const res = await openDag(dagId);
    setBusy(null);
    if (res === "opened") close();
  };

  return (
    <DialogShell
      title={t("open.title")}
      description={t("open.description")}
      size="lg"
      footer={
        <Button
          size="sm"
          variant="outline"
          onClick={() => {
            close();
            newDag();
          }}
        >
          <LuFilePlus /> {t("menu.new")}
        </Button>
      }
    >
      <Stack gap={3}>
        <HStack gap={2}>
          <InputGroup startElement={<LuSearch />} flex="1">
            <Input
              size="sm"
              autoFocus
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder={t("open.search")}
              aria-label={t("open.search")}
            />
          </InputGroup>
          <NativeSelect.Root size="sm" w="auto" minW="150px">
            <NativeSelect.Field
              aria-label={t("open.typeFilter")}
              data-testid="open-type-filter"
              value={type}
              onChange={(e) => setType(e.target.value as TypeFilter)}
            >
              <option value="all">{t("open.typeAll")}</option>
              <option value="spec">{t("open.typeVisual")}</option>
              <option value="raw">{t("open.typeRaw")}</option>
            </NativeSelect.Field>
            <NativeSelect.Indicator />
          </NativeSelect.Root>
        </HStack>
        {specs.isPending ? <Spinner size="sm" /> : null}
        {specs.isError ? (
          <Text color="red.500" fontSize="sm">
            {errorMessage(specs.error)}
          </Text>
        ) : null}
        {specs.data && !rows.length ? (
          <Text fontSize="sm" color="fg.muted">
            {specs.data.length ? t("open.noMatch") : t("open.empty")}
          </Text>
        ) : null}
        <Stack as="ul" gap={1} listStyleType="none" m={0} p={0} aria-label={t("open.title")}>
          {rows.map((s) => (
            <li key={s.dag_id}>
              <chakra.button
                type="button"
                w="100%"
                textAlign="start"
                borderWidth="1px"
                rounded="md"
                px={3}
                py={2}
                _hover={{ bg: "bg.muted" }}
                _focusVisible={{ outline: "2px solid", outlineColor: "blue.500" }}
                aria-current={s.dag_id === current ? "true" : undefined}
                onClick={() => void open(s.dag_id)}
                disabled={!!busy}
              >
                <HStack gap={2} wrap="wrap">
                  <Text fontFamily="mono" fontSize="sm" fontWeight="semibold">
                    {s.dag_id}
                  </Text>
                  {s.display_name ? (
                    <Text fontSize="sm" color="fg.muted">
                      {s.display_name}
                    </Text>
                  ) : null}
                  {typeOf(s) === "raw" ? (
                    <RawBadge size="xs" />
                  ) : (
                    <Badge size="xs" variant="subtle" title={t("open.typeVisualHelp")}>
                      <LuWorkflow /> {t("open.typeVisual")}
                    </Badge>
                  )}
                  {s.dag_id === current ? <Badge colorPalette="blue">{t("open.current")}</Badge> : null}
                  {s.dag_id === current && pendingPr ? <Badge colorPalette="purple">{t("status.prOpen")}</Badge> : null}
                  {busy === s.dag_id ? <Spinner size="xs" /> : null}
                  <Box flex="1" />
                  {(s.tags ?? []).map((tag) => (
                    <Badge key={tag} size="xs" variant="outline">
                      {tag}
                    </Badge>
                  ))}
                </HStack>
                {s.description ? (
                  <Text fontSize="xs" color="fg.muted" lineClamp={2}>
                    {s.description}
                  </Text>
                ) : null}
                <Text fontSize="xs" color="fg.muted">
                  {typeOf(s) === "raw"
                    ? t("open.metaRaw", { updated: dateFmt.format(new Date(s.updated_at)) })
                    : t("open.meta", {
                        count: s.task_count,
                        mode: s.output_mode,
                        updated: dateFmt.format(new Date(s.updated_at)),
                      })}
                  {s.etag ? ` · ${s.etag.slice(0, 8)}` : ""}
                </Text>
              </chakra.button>
            </li>
          ))}
        </Stack>
      </Stack>
    </DialogShell>
  );
}
