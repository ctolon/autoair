---
name: review-change
description: Review a diff or pull request against AutoAir's own rules - correctness, IAM and feature gating, secrets, replica safety, Airflow compatibility, i18n, light/dark, tests and committed generated artefacts. Use when asked to review changes, before handing work back, or before a PR.
---

# Review a change

Collect the change set. The working tree is shared with other agents, so review the files
the change touches, not everything `git status` lists.

```bash
git status --short && git diff --stat            # what changed (untracked files too)
git diff -- <paths>                              # the change itself
```

Report findings ordered by severity (blocker, major, minor, nit), each with
`path:line`, what is wrong, why it matters here, and the fix. Do not report style that ruff,
prettier or eslint already enforce.

## Checklist

**Correctness**
- Logic, edge cases (empty, None, huge input, unicode), error paths, async misuse (blocking
  I/O in `async def`, missing `await`), resource cleanup.
- Tests prove the behaviour, including the failure path. Golden snapshots reviewed if changed.

**Access control**
- New API routes carry `require(P....)` and, if user-facing, `feature("...")`
  (`src/autoair/api/app.py`). Narrowest permission. 403 and feature-off tested.
- New MCP tools are in `TOOL_PERMISSIONS` (and `TOOL_FEATURES`) in `src/autoair/mcp/iam.py`.
- Airflow calls use the caller's token (`GatewayDep`), never a service identity.

**Secrets**
- No token, key or password stored in settings files, documents, audit records, logs or
  error messages. Credentials come from Airflow connections (`conn_id`); per-request git
  tokens stay in memory and run under `secret_mask.masking`.
- New secret settings are `SecretStr` (and need the owner's agreement).

**Network**
- Outbound HTTP through `autoair.net.tls` (`async_client` / `httpx_kwargs`); user-supplied
  URLs checked against SSRF (`autoair.ai.net.check_url`, pinned transports).
- Timeouts and response size caps on every call.

**Replicas and state**
- No process-local state that other replicas must see; persistent state through
  `persistence.document_store` with versioned updates (compare-and-set, never a blind
  `get` + `put`); a side effect that must happen once is claimed first; inventory doc updated.

**Airflow compatibility**
- Works on 3.0; newer APIs feature-detected (skill airflow-compat-check).

**Settings and literals**
- Every new setting has a description and default; `gen_config_docs.py --check`,
  `scan_hardcoded.py --check` and `gen_shared_constants.py --check` pass.

**Frontend**
- Every string in en and tr (`src/i18n/parity.test.ts`); server texts shown in the UI carry
  a code + params with en/tr strings; raw code DAGs (`raw_code`) get no spec-only entry
  points (`SPEC_ONLY_DIALOGS`); semantic Chakra tokens only (`fg.muted`, `bg.panel`, ...),
  correct in light and dark; screenshots for both themes and both modes.
- Panels survive invalid live input: no throw during render, problems shown inline.
- zustand selectors return stable values (`useShallow` only over stable items; no new
  objects in selectors); undoable state goes through the builder store's history.
- Heavy modules (CodeMirror, ELK, admin, AI) stay lazy.

**Generated artefacts**
- API change: `frontend/openapi.json` and `frontend/src/api/gen/` regenerated, not edited.
- Settings change: `docs/configuration-reference.md` regenerated.
- CHANGELOG entry for user-visible changes; docs updated.

## Delegation

For large diffs, run the subagents in parallel: `backend-reviewer`, `frontend-reviewer`,
`security-reviewer`, `airflow-compat-reviewer`. Merge their findings; drop duplicates. The
saved workflow `/review-diff` (`.claude/workflows/review-diff.js`) does the same and then has
an independent agent try to refute each finding; pass the changed paths as its args.
