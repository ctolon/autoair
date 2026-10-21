import { Button, Field, Input, Stack, Text } from "@chakra-ui/react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { LuCopy } from "react-icons/lu";

import { useBuilder } from "../../store/builder";
import { loadAsUnsaved } from "../actions";
import { DialogShell } from "../components/DialogShell";
import { syncDeepLink } from "../lib/deepLink";
import { useLifecycle } from "../store";

const DAG_ID = /^[A-Za-z0-9_.-]{1,250}$/;

/** Copy the current DAG under a new dag_id (opened as a new, unsaved DAG). */
export default function DuplicateDialog() {
  const { t } = useTranslation("lifecycle");
  const close = useLifecycle((s) => s.closeDialog);
  const current = useBuilder((s) => s.dag.dag_id);
  const [dagId, setDagId] = useState(`${current}_copy`);
  const valid = DAG_ID.test(dagId) && dagId !== current;
  const duplicate = () => {
    const spec = useBuilder.getState().toSpec();
    loadAsUnsaved({ ...spec, dag_id: dagId, display_name: null }, { keepIdentity: false });
    syncDeepLink(null);
    close();
  };
  return (
    <DialogShell
      title={t("duplicate.title")}
      size="sm"
      footer={
        <Button size="sm" colorPalette="blue" disabled={!valid} onClick={duplicate}>
          <LuCopy /> {t("duplicate.confirm")}
        </Button>
      }
    >
      <Stack gap={2}>
        <Text fontSize="sm" color="fg.muted">
          {t("duplicate.help", { dagId: current })}
        </Text>
        <Field.Root invalid={!valid}>
          <Field.Label>{t("duplicate.newId")}</Field.Label>
          <Input
            fontFamily="mono"
            autoFocus
            value={dagId}
            onChange={(e) => setDagId(e.target.value.trim())}
            onKeyDown={(e) => e.key === "Enter" && valid && duplicate()}
          />
          <Field.ErrorText>{t("duplicate.invalid")}</Field.ErrorText>
        </Field.Root>
      </Stack>
    </DialogShell>
  );
}
