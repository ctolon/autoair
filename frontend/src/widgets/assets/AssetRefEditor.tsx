import { Badge, Box, Button, Collapsible, HStack, IconButton, Stack, Text } from "@chakra-ui/react";
import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { LuChevronDown, LuChevronRight, LuDatabase, LuPlus, LuRadio, LuTrash2 } from "react-icons/lu";

import { listAssetsOptions, listQueueSchemesOptions } from "../../api/gen/@tanstack/react-query.gen";
import type { AssetRef, AssetWatcherSpec, QueueScheme } from "../../api/gen/types.gen";
import type { JsonSchema } from "../../lib/formSchema";
import { Autocomplete, type AutocompleteOption } from "../Autocomplete";
import { FIELD_ATTR, Hint, JsonField, SelectField, TextField } from "../fields";
import { SchemaForm } from "../SchemaForm";
import { AssetActions } from "./AssetActions";

const str = (v: unknown): string | undefined => (typeof v === "string" && v ? v : undefined);

type KnownAsset = {
  id?: number | undefined;
  name?: string | undefined;
  uri?: string | undefined;
  group?: string | undefined;
};

/** Assets registered in Airflow (empty when Airflow is unreachable). */
function useKnownAssets(): KnownAsset[] {
  const q = useQuery({ ...listAssetsOptions(), retry: false, staleTime: 60_000 });
  return useMemo(
    () =>
      (q.data ?? [])
        .map((a) => ({
          id: typeof a.id === "number" ? a.id : undefined,
          name: str(a.name),
          uri: str(a.uri),
          group: str(a.group),
        }))
        .filter((a) => a.name || a.uri),
    [q.data],
  );
}

const objectValidator =
  (message: string) =>
  (v: unknown): string | undefined =>
    v && typeof v === "object" && !Array.isArray(v) ? undefined : message;

function hasProperties(schema: Record<string, unknown> | undefined): boolean {
  const props = schema?.properties;
  return !!props && typeof props === "object" && Object.keys(props).length > 0;
}

function WatcherEditor({
  value,
  onChange,
  onRemove,
  schemes,
}: {
  value: AssetWatcherSpec;
  onChange: (v: AssetWatcherSpec) => void;
  onRemove: () => void;
  schemes: QueueScheme[];
}) {
  const { t } = useTranslation("inspector");
  const scheme = schemes.find((s) => s.scheme === value.scheme);
  const options = schemes.map((s) => ({
    value: s.scheme,
    label: s.available ? s.scheme : `${s.scheme} — ${t("assets.installProvider", { provider: s.provider })}`,
    disabled: !s.available && s.scheme !== value.scheme,
  }));
  if (value.scheme && !scheme) options.push({ value: value.scheme, label: value.scheme, disabled: false });
  return (
    <Box borderWidth="1px" rounded="md" p={2}>
      <HStack justify="space-between" mb={1}>
        <HStack gap={1}>
          <LuRadio aria-hidden />
          <Text fontSize="xs" fontWeight="semibold">
            {value.name || t("assets.watcher")}
          </Text>
        </HStack>
        <IconButton
          aria-label={t("assets.removeWatcher", { name: value.name || value.scheme })}
          size="2xs"
          variant="ghost"
          colorPalette="red"
          onClick={onRemove}
        >
          <LuTrash2 />
        </IconButton>
      </HStack>
      <Stack gap={2}>
        <HStack align="start">
          <TextField
            help={t("assets.watcherNameHelp")}
            label={t("assets.watcherName")}
            required
            mono
            value={value.name}
            onChange={(v) => onChange({ ...value, name: v ?? "" })}
          />
          <SelectField
            help={t("assets.schemeHelp")}
            label={t("assets.scheme")}
            value={value.scheme || null}
            emptyLabel={t("assets.pickScheme")}
            options={options}
            onChange={(v) => onChange({ ...value, scheme: v ?? "", kwargs: {} })}
          />
        </HStack>
        {scheme && !scheme.available ? (
          <Hint tone="warning">{t("assets.schemeUnavailable", { provider: scheme.provider })}</Hint>
        ) : null}
        {scheme?.trigger_class ? <Hint>{t("assets.trigger", { cls: scheme.trigger_class })}</Hint> : null}
        {scheme && hasProperties(scheme.kwargs_schema) ? (
          <SchemaForm
            raw
            schema={scheme.kwargs_schema as JsonSchema}
            value={value.kwargs ?? {}}
            onChange={(kwargs) => onChange({ ...value, kwargs })}
          />
        ) : (
          <JsonField
            label={t("assets.kwargs")}
            help={t("assets.kwargsHelp")}
            rows={3}
            value={value.kwargs ?? {}}
            validate={objectValidator(t("assets.mustBeObject"))}
            onChange={(v) => onChange({ ...value, kwargs: (v ?? {}) as Record<string, unknown> })}
          />
        )}
      </Stack>
    </Box>
  );
}

/** Event-driven scheduling watchers (`AssetWatcher(trigger=MessageQueueTrigger(...))`). */
function WatchersEditor({
  value,
  onChange,
  assetName,
}: {
  value: AssetWatcherSpec[];
  onChange: (v: AssetWatcherSpec[]) => void;
  assetName?: string | null | undefined;
}) {
  const { t } = useTranslation("inspector");
  const q = useQuery({ ...listQueueSchemesOptions(), retry: false, staleTime: 300_000 });
  const schemes = q.data ?? [];
  const add = () => {
    const first = schemes.find((s) => s.available);
    const base = (assetName || "asset").replace(/[^A-Za-z0-9_]+/g, "_");
    onChange([...value, { name: `${base}_watcher_${value.length + 1}`, scheme: first?.scheme ?? "", kwargs: {} }]);
  };
  return (
    <Stack gap={2}>
      <HStack justify="space-between">
        <Text fontSize="xs" fontWeight="medium">
          {t("assets.watchers")}
        </Text>
        <Button size="2xs" variant="outline" onClick={add}>
          <LuPlus /> {t("assets.addWatcher")}
        </Button>
      </HStack>
      {!value.length ? <Hint>{t("assets.watchersHelp")}</Hint> : null}
      {value.map((w, i) => (
        <WatcherEditor
          key={i}
          value={w}
          schemes={schemes}
          onChange={(nw) => onChange(value.map((x, j) => (j === i ? nw : x)))}
          onRemove={() => onChange(value.filter((_, j) => j !== i))}
        />
      ))}
    </Stack>
  );
}

/** One asset reference: name (with Airflow suggestions), URI, group, extra and watchers. */
export function AssetRefEditor({
  value,
  onChange,
  label,
  field,
  allowWatchers = true,
}: {
  value: AssetRef;
  onChange: (v: AssetRef) => void;
  label?: string;
  field?: string;
  allowWatchers?: boolean;
}) {
  const { t } = useTranslation("inspector");
  const known = useKnownAssets();
  const options: AutocompleteOption[] = known.map((a) => ({
    value: a.name ?? a.uri ?? "",
    description: [a.uri, a.group].filter(Boolean).join(" · ") || undefined,
  }));
  const set = (patch: Partial<AssetRef>) => onChange({ ...value, ...patch });
  const noIdentity = !value.name && !value.uri;
  const advancedCount = (value.group ? 1 : 0) + (Object.keys(value.extra ?? {}).length ? 1 : 0);
  // The asset as registered in Airflow (by name, else URI): only those can get events.
  const registered = known.find(
    (a) =>
      a.id !== undefined &&
      ((!!value.name && a.name === value.name) || (!value.name && !!value.uri && a.uri === value.uri)),
  );
  return (
    <Stack gap={2} {...(field ? { [FIELD_ATTR]: field } : {})}>
      <Autocomplete
        label={label ?? t("assets.name")}
        mono
        value={value.name}
        options={options}
        placeholder="my_asset"
        error={noIdentity ? t("assets.nameOrUri") : undefined}
        onChange={(v) => set({ name: v })}
        onSelect={(o) => {
          const match = known.find((a) => (a.name ?? a.uri) === o.value);
          set({
            name: match?.name ?? o.value,
            uri: value.uri || match?.uri || null,
            group: value.group || match?.group || null,
          });
        }}
      />
      <TextField
        help={t("assets.uriHelp")}
        label={t("assets.uri")}
        mono
        placeholder="s3://bucket/key"
        value={value.uri}
        onChange={(v) => set({ uri: v })}
      />
      <Collapsible.Root>
        <Collapsible.Trigger asChild>
          <Button size="2xs" variant="ghost" px={0}>
            <LuChevronDown /> {t("assets.advanced")}
            {advancedCount ? <Badge size="xs">{advancedCount}</Badge> : null}
          </Button>
        </Collapsible.Trigger>
        <Collapsible.Content>
          <Stack gap={2} pt={1}>
            <TextField
              help={t("assets.groupHelp")}
              label={t("assets.group")}
              placeholder="asset"
              value={value.group}
              onChange={(v) => set({ group: v })}
            />
            <JsonField
              help={t("assets.extraHelp")}
              label={t("assets.extra")}
              rows={3}
              value={value.extra ?? {}}
              validate={objectValidator(t("assets.mustBeObject"))}
              onChange={(v) => set({ extra: (v ?? {}) as Record<string, unknown> })}
            />
          </Stack>
        </Collapsible.Content>
      </Collapsible.Root>
      {registered?.id !== undefined ? (
        <AssetActions assetId={registered.id} name={registered.name ?? registered.uri ?? ""} />
      ) : null}
      {allowWatchers ? (
        <WatchersEditor
          value={value.watchers ?? []}
          assetName={value.name}
          onChange={(watchers) => set({ watchers })}
        />
      ) : null}
    </Stack>
  );
}

/** Compact list of asset references (task inlets / outlets); rows expand into the full editor. */
export function AssetListEditor({
  label,
  help,
  value,
  onChange,
  field,
  allowWatchers = false,
}: {
  label: string;
  help?: string;
  value: AssetRef[];
  onChange: (v: AssetRef[]) => void;
  field?: string;
  allowWatchers?: boolean;
}) {
  const { t } = useTranslation("inspector");
  const [open, setOpen] = useState<number | null>(null);
  return (
    <Stack gap={1} {...(field ? { [FIELD_ATTR]: field } : {})}>
      <HStack justify="space-between">
        <Text fontSize="xs" fontWeight="medium">
          {label}
        </Text>
        <Button
          size="2xs"
          variant="outline"
          onClick={() => {
            onChange([...value, { name: "" }]);
            setOpen(value.length);
          }}
        >
          <LuPlus /> {t("assets.addAsset")}
        </Button>
      </HStack>
      {help ? <Hint>{help}</Hint> : null}
      {value.map((a, i) => {
        const expanded = open === i;
        const title = a.name || a.uri || t("assets.unnamed");
        return (
          <Box key={i} borderWidth="1px" rounded="md">
            <HStack px={2} py={1} gap={1}>
              <IconButton
                aria-label={expanded ? t("assets.collapse", { name: title }) : t("assets.expand", { name: title })}
                aria-expanded={expanded}
                size="2xs"
                variant="ghost"
                onClick={() => setOpen(expanded ? null : i)}
              >
                {expanded ? <LuChevronDown /> : <LuChevronRight />}
              </IconButton>
              <LuDatabase aria-hidden />
              <Text fontSize="xs" fontFamily="mono" truncate flex="1" title={a.uri ?? undefined}>
                {title}
              </Text>
              {a.uri && a.name ? (
                <Text fontSize="2xs" color="fg.muted" truncate maxW="40%">
                  {a.uri}
                </Text>
              ) : null}
              {a.watchers?.length ? <Badge size="xs">{a.watchers.length}</Badge> : null}
              <IconButton
                aria-label={t("assets.removeAsset", { name: title })}
                size="2xs"
                variant="ghost"
                colorPalette="red"
                onClick={() => {
                  onChange(value.filter((_, j) => j !== i));
                  setOpen(null);
                }}
              >
                <LuTrash2 />
              </IconButton>
            </HStack>
            {expanded ? (
              <Box px={2} pb={2}>
                <AssetRefEditor
                  value={a}
                  allowWatchers={allowWatchers}
                  onChange={(na) => onChange(value.map((x, j) => (j === i ? na : x)))}
                />
              </Box>
            ) : null}
          </Box>
        );
      })}
    </Stack>
  );
}
