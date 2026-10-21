import { Button, HStack, Stack, Text } from "@chakra-ui/react";
import { useMutation } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import { LuCode, LuDownload, LuFileCode, LuFilePlus } from "react-icons/lu";
import { useAccess } from "../../lib/access";

import { importAirflowDagMutation } from "../../api/gen/@tanstack/react-query.gen";
import { errorMessage } from "../../api/setup";
import { editorAvailability, useCodeMode } from "../../codemode/store";
import { useBuilder } from "../../store/builder";
import { openAsRawDag, openImported } from "../actions";
import { DialogShell } from "../components/DialogShell";
import { useLifecycle } from "../store";

/** Opened for a DAG AutoAir does not manage (e.g. the AutoAir tab of a hand-written DAG). */
export default function UnmanagedDialog({ dagId }: { dagId: string }) {
  const access = useAccess();
  const { t } = useTranslation("lifecycle");
  const dismiss = () => useLifecycle.getState().setUnmanagedDagId(null);
  const imp = useMutation({
    ...importAirflowDagMutation(),
    onSuccess: (res) => {
      const found = res.dags.find((d) => d.dag_id === dagId && d.spec) ?? res.dags.find((d) => d.spec);
      if (found?.spec && openImported(found.spec)) dismiss();
    },
  });
  const notImportable = imp.data && !imp.data.dags.some((d) => d.spec);
  const codeEditor = editorAvailability(access).code && editorAvailability(access).canEditCode;
  // The DAG exists only as Python and cannot be converted: edit its original code instead.
  const openInCodeEditor = (source: string) => {
    useBuilder.getState().reset(dagId);
    useCodeMode.getState().setMode("code");
    useCodeMode.getState().edit(source, null);
    dismiss();
  };
  return (
    <DialogShell
      title={t("unmanaged.title", { dagId })}
      description={t("unmanaged.description")}
      size="md"
      onClose={dismiss}
      footer={
        <>
          <Button
            size="sm"
            variant="ghost"
            onClick={() => {
              useBuilder.getState().reset(dagId);
              dismiss();
            }}
          >
            <LuFilePlus /> {t("unmanaged.startNew")}
          </Button>
          <Button
            size="sm"
            colorPalette="blue"
            loading={imp.isPending}
            hidden={!access.feature("imports")}
            disabled={!access.can("code:execute")}
            title={access.can("code:execute") ? undefined : t("access.denied", { permission: "code:execute" })}
            onClick={() => imp.mutate({ path: { dag_id: dagId } })}
          >
            <LuDownload /> {t("unmanaged.import")}
          </Button>
        </>
      }
    >
      <Stack gap={2}>
        <Text fontSize="sm">{t("unmanaged.help")}</Text>
        {imp.isError ? (
          <Text fontSize="sm" color="red.500">
            {errorMessage(imp.error)}
          </Text>
        ) : null}
        {imp.data?.source && editorAvailability(access).canRaw ? (
          <HStack justify="space-between" gap={2} borderWidth="1px" rounded="md" p={2}>
            <Text fontSize="xs">{t("unmanaged.rawHelp")}</Text>
            <Button
              size="xs"
              variant="outline"
              data-testid="open-as-raw"
              onClick={() => {
                openAsRawDag(imp.data.source ?? "", `${dagId}.py`);
                dismiss();
              }}
            >
              <LuFileCode /> {t("unmanaged.openAsRaw")}
            </Button>
          </HStack>
        ) : null}
        {notImportable && imp.data?.source && codeEditor && !editorAvailability(access).canRaw ? (
          <HStack justify="space-between" gap={2} borderWidth="1px" rounded="md" p={2}>
            <Text fontSize="xs">{t("unmanaged.codeHelp")}</Text>
            <Button
              size="xs"
              variant="outline"
              data-testid="open-in-code"
              onClick={() => openInCodeEditor(imp.data.source ?? "")}
            >
              <LuCode /> {t("unmanaged.openInCode")}
            </Button>
          </HStack>
        ) : null}
        {notImportable
          ? [...(imp.data?.import_errors ?? []), ...(imp.data?.dags ?? []).map((d) => d.error ?? d.warnings.join("; "))]
              .filter(Boolean)
              .map((m, i) => (
                <Text key={i} fontSize="xs" color="red.500" whiteSpace="pre-wrap">
                  {m}
                </Text>
              ))
          : null}
        {imp.data?.dags
          .find((d) => d.spec)
          ?.warnings.map((w, i) => (
            <Text key={i} fontSize="xs" color="orange.500">
              • {w}
            </Text>
          ))}
      </Stack>
    </DialogShell>
  );
}
