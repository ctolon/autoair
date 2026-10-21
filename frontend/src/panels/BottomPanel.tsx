import { Badge, Box, Spinner, Tabs } from "@chakra-ui/react";
import { Suspense, lazy } from "react";
import { useTranslation } from "react-i18next";
import { useAccess } from "../lib/access";

import type { LiveState } from "../hooks/useLiveChecks";
import { ProblemsTab } from "../lifecycle/panels/ProblemsTab";
import { type BottomTab, useLifecycle } from "../lifecycle/store";
import { useBuilder } from "../store/builder";

// Heavy tabs (CodeMirror, merge view, polling queries) load on first use.
const CodeTab = lazy(() => import("../lifecycle/panels/CodeTab"));
const RunsTab = lazy(() => import("../lifecycle/panels/RunsTab"));
const TaskTab = lazy(() => import("../lifecycle/panels/TaskTab"));
const VersionsTab = lazy(() => import("../lifecycle/panels/VersionsTab"));
const AirflowTab = lazy(() => import("../lifecycle/panels/AirflowTab"));
const AuditTab = lazy(() => import("../lifecycle/panels/AuditTab"));

const TABS: BottomTab[] = ["problems", "code", "runs", "task", "versions", "airflow", "audit"];

export function BottomPanel({ live }: { live: LiveState }) {
  const { t } = useTranslation("lifecycle");
  const tab = useLifecycle((s) => s.bottomTab);
  const setTab = useLifecycle((s) => s.setBottomTab);
  const access = useAccess();
  const problems = useBuilder((s) => s.problems);
  const errors = problems.filter((p) => p.severity === "error").length;
  const runOverlay = useBuilder((s) => s.runOverlay);
  const diffOverlay = useBuilder((s) => s.diffOverlay);
  return (
    <Tabs.Root
      value={tab}
      onValueChange={(e) => TABS.includes(e.value as BottomTab) && setTab(e.value as BottomTab)}
      size="sm"
      h="100%"
      display="flex"
      flexDirection="column"
      lazyMount
    >
      <Tabs.List px={2} overflowX="auto" flexShrink={0}>
        <Tabs.Trigger value="problems">
          {t("tabs.problems")}
          {problems.length ? (
            <Badge colorPalette={errors ? "red" : "orange"} size="xs">
              {problems.length}
            </Badge>
          ) : null}
          {live.checking ? <Spinner size="xs" aria-label={t("tabs.checking")} /> : null}
        </Tabs.Trigger>
        <Tabs.Trigger value="code">{t("tabs.code")}</Tabs.Trigger>
        <Tabs.Trigger value="runs" hidden={!access.feature("airflow") || !access.feature("runs")}>
          {t("tabs.runs")}
          {runOverlay ? (
            <Badge size="xs" colorPalette="blue">
              ●
            </Badge>
          ) : null}
        </Tabs.Trigger>
        <Tabs.Trigger value="task">{t("tabs.task")}</Tabs.Trigger>
        <Tabs.Trigger value="versions">
          {t("tabs.versions")}
          {diffOverlay ? (
            <Badge size="xs" colorPalette="purple">
              ●
            </Badge>
          ) : null}
        </Tabs.Trigger>
        {access.feature("airflow") ? <Tabs.Trigger value="airflow">{t("tabs.airflow")}</Tabs.Trigger> : null}
        {access.can("audit:read") ? <Tabs.Trigger value="audit">{t("tabs.audit")}</Tabs.Trigger> : null}
      </Tabs.List>
      <Box flex="1" minH={0} overflow="auto">
        <Suspense fallback={<Spinner size="sm" m={3} />}>
          <Tabs.Content value="problems" p={0}>
            <ProblemsTab />
          </Tabs.Content>
          <Tabs.Content value="code" p={0} h="100%">
            <CodeTab live={live} />
          </Tabs.Content>
          <Tabs.Content value="runs" p={0}>
            <RunsTab />
          </Tabs.Content>
          <Tabs.Content value="task" p={0} h="100%">
            <TaskTab />
          </Tabs.Content>
          <Tabs.Content value="versions" p={0} h="100%">
            <VersionsTab />
          </Tabs.Content>
          <Tabs.Content value="airflow" p={0}>
            <AirflowTab />
          </Tabs.Content>
          <Tabs.Content value="audit" p={0}>
            <AuditTab />
          </Tabs.Content>
        </Suspense>
      </Box>
    </Tabs.Root>
  );
}
