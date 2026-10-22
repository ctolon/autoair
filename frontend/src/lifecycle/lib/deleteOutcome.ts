import type { DeleteResult } from "../../api/gen/types.gen";

type Translate = (key: string, opts?: Record<string, unknown>) => string;
type Toast = { type: "info" | "success"; title: string; description?: string; duration?: number };

/**
 * Whether a delete took the DAG out of the target now, so the editor closes it. A delete
 * proposal (four-eyes) or a pull request leaves the DAG live: the editor keeps it open.
 */
export function deleteWentLive(res: DeleteResult | null | undefined): boolean {
  const status = res?.status ?? "deleted";
  return status === "deleted" || status === "not_found";
}

/** The toast reporting a delete's outcome. */
export function deleteToast(res: DeleteResult | null | undefined, dagId: string, t: Translate): Toast {
  switch (res?.status) {
    case "pending_approval":
      return {
        type: "info",
        title: t("toolbar.deletePending", { dagId }),
        description: t("toolbar.deletePendingBody"),
        duration: 10_000,
      };
    case "pr_opened":
      return {
        type: "success",
        title: t("toolbar.deletePrOpened", { dagId }),
        description: t("toolbar.deletePrOpenedBody"),
        duration: 10_000,
      };
    default:
      return { type: "info", title: t("toolbar.deleted", { dagId }) };
  }
}

/**
 * The If-Match header of a delete: the etag of the version the editor loaded. The server
 * refuses a delete without it while the DAG exists (428) and one of a version that changed
 * since (409), so a concurrent edit is never deleted unseen.
 */
export function ifMatch(etag: string | null | undefined): { "if-match"?: string } {
  return etag ? { "if-match": `"${etag}"` } : {};
}
