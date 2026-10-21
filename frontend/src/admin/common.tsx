import { Alert, Box, Card, Heading, HStack, Spinner, Stack, Table, Text } from "@chakra-ui/react";
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { errorMessage } from "../api/setup";

/** Loading / error / content of one admin query (an error never breaks the panel). */
export function QueryState({
  isPending,
  error,
  children,
}: {
  isPending: boolean;
  error: unknown;
  children: ReactNode;
}) {
  const { t } = useTranslation("admin");
  if (isPending) {
    return (
      <HStack gap={2} color="fg.muted" role="status" aria-live="polite">
        <Spinner size="sm" />
        <Text fontSize="sm">{t("loading")}</Text>
      </HStack>
    );
  }
  if (error) {
    return (
      <Alert.Root status="error" size="sm">
        <Alert.Indicator />
        <Alert.Title>{t("error", { message: errorMessage(error) })}</Alert.Title>
      </Alert.Root>
    );
  }
  return <>{children}</>;
}

export function Section({
  title,
  help,
  actions,
  children,
}: {
  title?: ReactNode;
  help?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
}) {
  return (
    <Stack gap={2} as="section">
      {title || actions ? (
        <HStack justify="space-between" wrap="wrap" gap={2}>
          {title ? (
            <Heading as="h3" size="sm">
              {title}
            </Heading>
          ) : (
            <span />
          )}
          {actions}
        </HStack>
      ) : null}
      {help ? (
        <Text fontSize="sm" color="fg.muted">
          {help}
        </Text>
      ) : null}
      {children}
    </Stack>
  );
}

/** A small labelled value card. */
export function StatCard({
  label,
  value,
  hint,
  testId,
}: {
  label: ReactNode;
  value: ReactNode;
  hint?: ReactNode;
  testId?: string;
}) {
  return (
    <Card.Root size="sm" variant="outline" data-testid={testId}>
      <Card.Body gap={1}>
        <Text fontSize="xs" color="fg.muted" textTransform="uppercase" letterSpacing="wide">
          {label}
        </Text>
        <Text fontSize="lg" fontWeight="semibold" wordBreak="break-all">
          {value}
        </Text>
        {hint ? (
          <Text fontSize="xs" color="fg.muted">
            {hint}
          </Text>
        ) : null}
      </Card.Body>
    </Card.Root>
  );
}

/** Scrollable bordered table with an accessible name. */
export function DataTable({ label, testId, children }: { label: string; testId?: string; children: ReactNode }) {
  return (
    <Box overflowX="auto" borderWidth="1px" rounded="md">
      <Table.Root size="sm" variant="line" aria-label={label} data-testid={testId} stickyHeader>
        {children}
      </Table.Root>
    </Box>
  );
}

export function Empty() {
  const { t } = useTranslation("admin");
  return (
    <Text fontSize="sm" color="fg.muted" py={2}>
      {t("empty")}
    </Text>
  );
}
