import { Badge, Button, Field, HStack, Input, Stack, Switch, Table, Text } from "@chakra-ui/react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { LuLock, LuRotateCcw } from "react-icons/lu";

import {
  adminFeaturesOptions,
  adminFeaturesQueryKey,
  adminSetFeatureMutation,
  getMetaQueryKey,
} from "../../api/gen/@tanstack/react-query.gen";
import type { FeatureFlagState } from "../../api/gen/types.gen";
import { errorMessage } from "../../api/setup";
import { toaster } from "../../ui/toaster";
import { DataTable, QueryState, Section } from "../common";
import { formatTime } from "../format";

/** Minimum length of the audited reason (the API refuses shorter ones). */
const MIN_REASON = 3;

function OnOff({ on, testId }: { on: boolean | null | undefined; testId?: string }) {
  const { t } = useTranslation("admin");
  if (on === null || on === undefined) {
    return (
      <Text as="span" color="fg.muted" data-testid={testId}>
        —
      </Text>
    );
  }
  return (
    <Badge colorPalette={on ? "green" : "gray"} variant="subtle" data-testid={testId}>
      {on ? t("overview.on") : t("overview.off")}
    </Badge>
  );
}

/** Feature flags switched at runtime (`admin:config`); locked flags are read-only. */
export function FeaturesTab({ canWrite }: { canWrite: boolean }) {
  const { t, i18n } = useTranslation("admin");
  const client = useQueryClient();
  const q = useQuery(adminFeaturesOptions());
  const [reason, setReason] = useState("");
  const writable = canWrite && !!q.data?.can_write;
  const reasonOk = reason.trim().length >= MIN_REASON;
  const change = useMutation({
    ...adminSetFeatureMutation(),
    onSuccess: (view, vars) => {
      client.setQueryData(adminFeaturesQueryKey(), view);
      // /api/meta reports the new value at once: the whole UI follows the change.
      void client.invalidateQueries({ queryKey: getMetaQueryKey() });
      toaster.create({ type: "success", title: t("features.saved", { name: vars.path.name }) });
    },
    onError: (err) => toaster.create({ type: "error", title: t("error", { message: errorMessage(err) }) }),
  });
  const apply = (flag: FeatureFlagState, value: boolean | null) =>
    change.mutate({ path: { name: flag.name }, body: { value, reason: reason.trim() } });

  const flags = q.data?.flags ?? [];
  return (
    <Section
      help={t("features.help", { seconds: q.data?.refresh_seconds ?? 0 })}
      actions={
        q.data ? (
          <Badge variant="outline" colorPalette={q.data.shared ? "blue" : "gray"}>
            {q.data.shared ? t("features.shared") : t("features.local")}
          </Badge>
        ) : null
      }
    >
      <QueryState isPending={q.isPending} error={q.error}>
        <Stack gap={3}>
          {writable ? (
            <Field.Root required invalid={reason.length > 0 && !reasonOk} maxW="xl">
              <Field.Label>{t("features.reason")}</Field.Label>
              <Input
                size="sm"
                value={reason}
                maxLength={500}
                placeholder={t("features.reasonPlaceholder")}
                onChange={(e) => setReason(e.target.value)}
                data-testid="admin-features-reason"
              />
              <Field.HelperText>{t("features.reasonHelp")}</Field.HelperText>
            </Field.Root>
          ) : (
            <Text fontSize="sm" color="fg.muted">
              {t("features.readOnly")}
            </Text>
          )}
          <DataTable label={t("tabs.features")} testId="admin-features-table">
            <Table.Header>
              <Table.Row>
                <Table.ColumnHeader>{t("features.flag")}</Table.ColumnHeader>
                <Table.ColumnHeader>{t("features.configured")}</Table.ColumnHeader>
                <Table.ColumnHeader>{t("features.override")}</Table.ColumnHeader>
                <Table.ColumnHeader>{t("features.effective")}</Table.ColumnHeader>
                <Table.ColumnHeader whiteSpace="nowrap">{t("features.lastChange")}</Table.ColumnHeader>
                <Table.ColumnHeader textAlign="end">{t("features.actions")}</Table.ColumnHeader>
              </Table.Row>
            </Table.Header>
            <Table.Body>
              {flags.map((f) => {
                const busy = change.isPending && change.variables?.path.name === f.name;
                const disabled = !writable || !!f.locked || !!f.unavailable || !reasonOk || change.isPending;
                const target = f.override ?? f.configured;
                return (
                  <Table.Row key={f.name} data-testid={`admin-feature-${f.name}`}>
                    <Table.Cell minW="14rem">
                      <Text fontWeight="medium" fontFamily="mono" fontSize="sm">
                        {f.name}
                      </Text>
                      <Text fontSize="xs" color="fg.muted" lineClamp={2} title={f.description ?? undefined}>
                        {f.description}
                      </Text>
                      <HStack gap={1} mt={1} wrap="wrap">
                        {f.locked ? (
                          <Badge colorPalette="orange" variant="subtle" size="sm" data-testid="feature-locked">
                            <LuLock /> {t(`features.locked.${f.locked}`)}
                          </Badge>
                        ) : null}
                        {f.unavailable ? (
                          <Badge
                            colorPalette="red"
                            variant="subtle"
                            size="sm"
                            title={f.unavailable}
                            data-testid="feature-unavailable"
                          >
                            {t("features.unavailable")}
                          </Badge>
                        ) : f.constrained ? (
                          <Badge colorPalette="yellow" variant="subtle" size="sm">
                            {t("features.constrained")}
                          </Badge>
                        ) : null}
                      </HStack>
                    </Table.Cell>
                    <Table.Cell>
                      <OnOff on={f.configured} />
                    </Table.Cell>
                    <Table.Cell>
                      <OnOff on={f.override} testId="feature-override" />
                    </Table.Cell>
                    <Table.Cell>
                      <OnOff on={f.effective} testId="feature-effective" />
                    </Table.Cell>
                    <Table.Cell fontSize="xs" color="fg.muted" maxW="16rem">
                      {f.updated_at ? (
                        <>
                          <Text>
                            {formatTime(f.updated_at, i18n.language)}
                            {f.updated_by ? ` · ${f.updated_by}` : ""}
                          </Text>
                          {f.reason ? (
                            <Text lineClamp={2} title={f.reason}>
                              {f.reason}
                            </Text>
                          ) : null}
                        </>
                      ) : (
                        "—"
                      )}
                    </Table.Cell>
                    <Table.Cell textAlign="end">
                      <HStack justify="flex-end" gap={2}>
                        <Switch.Root
                          size="sm"
                          checked={target}
                          disabled={disabled}
                          onCheckedChange={(e) => apply(f, e.checked)}
                          data-testid={`feature-switch-${f.name}`}
                        >
                          <Switch.HiddenInput aria-label={t("features.toggle", { name: f.name })} />
                          <Switch.Control />
                        </Switch.Root>
                        {f.override !== null && f.override !== undefined && !f.locked ? (
                          <Button
                            size="xs"
                            variant="ghost"
                            disabled={disabled}
                            loading={busy}
                            onClick={() => apply(f, null)}
                            title={t("features.resetHelp")}
                          >
                            <LuRotateCcw /> {t("features.reset")}
                          </Button>
                        ) : null}
                      </HStack>
                    </Table.Cell>
                  </Table.Row>
                );
              })}
            </Table.Body>
          </DataTable>
        </Stack>
      </QueryState>
    </Section>
  );
}
