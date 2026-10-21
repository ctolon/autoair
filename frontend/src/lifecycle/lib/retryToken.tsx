import { useState } from "react";

import { useBuilder } from "../../store/builder";
import { GitTokenField } from "../components/GitTokenField";
import { useMeta } from "../hooks";
import { tokenMissing, useGitTokenInfo } from "./gitToken";

/**
 * The git token a retried deploy needs (storage.git.request_token): typed again here, since
 * the first one was dropped right after the deploy that ran into the conflict.
 */
export function useRetryToken() {
  const meta = useMeta();
  const team = useBuilder((s) => s.dag.team ?? null);
  const info = useGitTokenInfo(team, meta?.storage.kind === "git");
  const [token, setToken] = useState("");
  const [invalid, setInvalid] = useState(false);
  const field = (
    <GitTokenField
      info={info}
      value={token}
      onChange={(v) => {
        setToken(v);
        if (v.trim()) setInvalid(false);
      }}
      invalid={invalid}
    />
  );
  /** The secret for `deploy()`, or `null` when a required token is missing. */
  const take = (): { gitToken: string } | null => {
    if (tokenMissing(info, token)) {
      setInvalid(true);
      return null;
    }
    const gitToken = token;
    setToken("");
    return { gitToken };
  };
  return { field, take };
}
