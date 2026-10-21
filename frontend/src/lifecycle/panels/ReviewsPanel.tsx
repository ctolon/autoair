import { Badge, Button, HStack, Stack, Text, Textarea } from "@chakra-ui/react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { LuCircleCheck, LuMessageSquare, LuTriangleAlert } from "react-icons/lu";

import {
  listSpecReviewsOptions,
  listSpecReviewsQueryKey,
  reviewSpecMutation,
} from "../../api/gen/@tanstack/react-query.gen";
import type { Review } from "../../api/gen/types.gen";
import { errorMessage } from "../../api/setup";
import { useAccess } from "../../lib/access";
import { useBuilder } from "../../store/builder";
import { useMeta } from "../hooks";

const VERDICT = {
  approve: { palette: "green", icon: <LuCircleCheck /> },
  request_changes: { palette: "orange", icon: <LuTriangleAlert /> },
  comment: { palette: "gray", icon: <LuMessageSquare /> },
} as const;

/**
 * Four-eyes review of the saved version: reviewers approve or request changes; with
 * `iam.require_approval` going live needs an approval of that exact version by someone else.
 */
export function ReviewsPanel() {
  const { t, i18n } = useTranslation("lifecycle");
  const qc = useQueryClient();
  const dagId = useBuilder((s) => s.savedDagId);
  const etag = useBuilder((s) => s.baseEtag);
  const dirty = useBuilder((s) => s.dirty);
  const access = useAccess();
  const meta = useMeta();
  const [comment, setComment] = useState("");
  const reviews = useQuery({
    ...listSpecReviewsOptions({ path: { dag_id: dagId ?? "_" } }),
    enabled: !!dagId,
    retry: false,
  });
  const review = useMutation({
    ...reviewSpecMutation(),
    onSuccess: async () => {
      setComment("");
      await qc.invalidateQueries({ queryKey: listSpecReviewsQueryKey({ path: { dag_id: dagId ?? "_" } }) });
    },
  });
  if (!dagId)
    return (
      <Text fontSize="sm" color="fg.muted" p={3}>
        {t("reviews.notSaved")}
      </Text>
    );
  const all = reviews.data ?? [];
  const current = all.filter((r: Review) => r.etag === etag);
  const approvals = current.filter((r) => r.verdict === "approve").length;
  const required = meta?.iam?.deploy.require_approval;
  const send = (verdict: "approve" | "request_changes" | "comment") =>
    etag && review.mutate({ path: { dag_id: dagId }, body: { etag, verdict, comment: comment.trim() || null } });
  return (
    <Stack gap={2} p={3} data-testid="reviews-panel">
      <HStack gap={2} wrap="wrap">
        <Badge colorPalette={approvals ? "green" : required ? "orange" : "gray"}>
          {t("reviews.approvals", { count: approvals })}
        </Badge>
        {required ? (
          <Text fontSize="xs" color="fg.muted">
            {t("reviews.required")}
          </Text>
        ) : null}
      </HStack>
      {dirty ? (
        <Text fontSize="xs" color="orange.fg">
          {t("reviews.unsaved")}
        </Text>
      ) : null}
      {access.can("review") ? (
        <Stack gap={1.5}>
          <Textarea
            size="sm"
            rows={2}
            value={comment}
            placeholder={t("reviews.commentPlaceholder")}
            aria-label={t("reviews.comment")}
            onChange={(e) => setComment(e.target.value)}
          />
          <HStack gap={1}>
            <Button
              size="xs"
              colorPalette="green"
              disabled={!etag || dirty}
              loading={review.isPending}
              onClick={() => send("approve")}
            >
              <LuCircleCheck /> {t("reviews.approve")}
            </Button>
            <Button
              size="xs"
              variant="outline"
              colorPalette="orange"
              disabled={!etag || dirty}
              onClick={() => send("request_changes")}
            >
              {t("reviews.requestChanges")}
            </Button>
            <Button size="xs" variant="ghost" disabled={!etag || !comment.trim()} onClick={() => send("comment")}>
              {t("reviews.commentOnly")}
            </Button>
          </HStack>
          {review.error ? (
            <Text fontSize="xs" color="fg.error">
              {errorMessage(review.error)}
            </Text>
          ) : null}
        </Stack>
      ) : null}
      <Stack as="ul" gap={1.5} listStyleType="none" m={0} p={0}>
        {[...all].reverse().map((r, i) => (
          <HStack as="li" key={`${r.timestamp}-${i}`} align="start" gap={2} opacity={r.etag === etag ? 1 : 0.6}>
            <Badge size="xs" colorPalette={VERDICT[r.verdict as keyof typeof VERDICT]?.palette ?? "gray"}>
              {VERDICT[r.verdict as keyof typeof VERDICT]?.icon} {t(`reviews.verdict.${r.verdict}`)}
            </Badge>
            <Stack gap={0} flex="1" minW={0}>
              <Text fontSize="xs">
                <b>{r.user}</b>{" "}
                <Text as="span" color="fg.muted">
                  {new Date(r.timestamp).toLocaleString(i18n.language)}
                  {r.etag !== etag ? ` · ${t("reviews.olderVersion")}` : ""}
                </Text>
              </Text>
              {r.comment ? (
                <Text fontSize="sm" whiteSpace="pre-wrap">
                  {r.comment}
                </Text>
              ) : null}
            </Stack>
          </HStack>
        ))}
      </Stack>
    </Stack>
  );
}
