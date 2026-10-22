import { Button, Stack, Text } from "@chakra-ui/react";
import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { LuTrash2 } from "react-icons/lu";

import { listSpecsQueryKey } from "../../api/gen/@tanstack/react-query.gen";
import { deleteSpec } from "../../api/gen/sdk.gen";
import { errorMessage } from "../../api/setup";
import { useBuilder } from "../../store/builder";
import { toaster } from "../../ui/toaster";
import { gitTokenToast } from "../actions";
import { DialogShell } from "../components/DialogShell";
import { GitTokenField } from "../components/GitTokenField";
import { syncDeepLink } from "../lib/deepLink";
import { deleteToast, deleteWentLive, ifMatch } from "../lib/deleteOutcome";
import { gitTokenHeaders, tokenMissing, useGitTokenInfo } from "../lib/gitToken";
import { useLifecycle } from "../store";

/**
 * Delete a deployed DAG from a git target that takes the user's own token
 * (storage.git.request_token optional/required); other targets keep the plain confirm.
 */
export default function DeleteDagDialog({ dagId }: { dagId: string }) {
  const { t } = useTranslation("lifecycle");
  const qc = useQueryClient();
  const close = useLifecycle((s) => s.closeDialog);
  const team = useBuilder((s) => s.dag.team ?? null);
  const info = useGitTokenInfo(team);
  const [token, setToken] = useState("");
  const [invalid, setInvalid] = useState(false);
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    if (tokenMissing(info, token)) {
      setInvalid(true);
      return;
    }
    setBusy(true);
    try {
      // The token goes in this request's header only (no mutation variables, no query key).
      const { data } = await deleteSpec({
        path: { dag_id: dagId },
        headers: { ...gitTokenHeaders(token), ...ifMatch(useBuilder.getState().baseEtag) },
        throwOnError: true,
      });
      await qc.invalidateQueries({ queryKey: listSpecsQueryKey() });
      // A delete proposal (four-eyes) or a pull request leaves the DAG live: keep it open.
      if (deleteWentLive(data)) {
        useBuilder.getState().reset();
        useLifecycle.getState().setSelectedRunId(null);
        syncDeepLink(null);
      }
      toaster.create(deleteToast(data, dagId, t));
      close();
    } catch (e) {
      toaster.create(
        gitTokenToast(e) ?? { type: "error", title: t("toolbar.deleteFailed"), description: errorMessage(e) },
      );
    } finally {
      setToken("");
      setBusy(false);
    }
  };

  return (
    <DialogShell
      title={t("deleteDag.title", { dagId })}
      size="md"
      footer={
        <>
          <Button size="sm" variant="ghost" onClick={close}>
            {t("common.cancel")}
          </Button>
          <Button size="sm" colorPalette="red" loading={busy} onClick={() => void submit()} data-testid="delete-dag">
            <LuTrash2 /> {t("deleteDag.button")}
          </Button>
        </>
      }
    >
      <Stack gap={4}>
        <Text fontSize="sm">{t("deleteDag.body", { dagId })}</Text>
        <GitTokenField
          info={info}
          value={token}
          onChange={(v) => {
            setToken(v);
            if (v.trim()) setInvalid(false);
          }}
          invalid={invalid}
        />
      </Stack>
    </DialogShell>
  );
}
