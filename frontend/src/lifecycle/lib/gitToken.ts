/**
 * Git tokens typed by the user for one write (`storage.git.request_token`, docs/iam.md).
 *
 * The token only ever lives in the React state of the dialog that asked for it and in the
 * `X-AutoAir-Git-Token` header of that one request: never in a zustand store, a draft,
 * local/session storage, the URL, a React Query key or mutation variables.
 */
import { useQuery } from "@tanstack/react-query";

import { getGitCredentialsOptions } from "../../api/gen/@tanstack/react-query.gen";
import type { RequestTokenInfo } from "../../api/gen/types.gen";

const GIT_TOKEN_HEADER = "X-AutoAir-Git-Token";

type GitTokenHeaders = { "X-AutoAir-Git-Token"?: string };

/** The per-call headers carrying `token` (nothing when empty). */
export function gitTokenHeaders(token: string | null | undefined): GitTokenHeaders {
  const value = token?.trim();
  return value ? { [GIT_TOKEN_HEADER]: value } : {};
}

/** `code` of a structured API error detail (`git_token_rejected`, `git_token_required`…). */
export function gitTokenErrorCode(err: unknown): string | null {
  if (!err || typeof err !== "object" || !("detail" in err)) return null;
  const detail = (err as { detail: unknown }).detail;
  if (detail && typeof detail === "object" && typeof (detail as { code?: unknown }).code === "string") {
    const code = (detail as { code: string }).code;
    return code.startsWith("git_token_") ? code : null;
  }
  return null;
}

/** Whether (and how) deploys of the target of `team` take a token (null: not a git target). */
export function useGitTokenInfo(team?: string | null, enabled = true): RequestTokenInfo | null {
  const query = useQuery({
    ...getGitCredentialsOptions(team ? { query: { team } } : undefined),
    enabled,
    retry: false,
    staleTime: 60_000,
  });
  return query.data?.request_token ?? null;
}

/** A required token is missing (the dialog shows the field invalid instead of sending). */
export function tokenMissing(info: RequestTokenInfo | null, token: string): boolean {
  return info?.mode === "required" && !token.trim();
}
