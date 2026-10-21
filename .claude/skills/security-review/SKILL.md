---
name: security-review
description: Security review of an AutoAir change against the project's threat model - authentication and IAM, CSRF and SSO cookies, secrets and masking, code execution boundaries, SSRF, AI guardrails and sandboxed file access, OPA fail mode, injection into generated code or git. Use for changes to auth, iam, api, mcp, ai, storage/git, net, codegen, importer, runner or deploy files.
---

# Security review

Threat model and hardening: `SECURITY.md` (scope), `website/docs/security/overview.md`,
`docs/iam.md`, `docs/sso.md`, `docs/configuration.md` (git tokens typed per deploy),
`docs/architecture/state-and-scaling.md`.

## Model in brief

- **AutoAir has no user database.** Airflow authenticates every user; AutoAir derives roles
  (`iam/resolver.py`, strategies `airflow_permissions`, `role_mapping`, `custom_view`) and
  checks fine-grained permissions (`iam/model.py`) with `require(...)`. Airflow calls forward
  the caller's token, so Airflow RBAC still applies.
- **Four-eyes**: `iam.require_approval` routes deploys through proposals and reviews
  (`iam/proposals.py`); `deploy:direct_push` bypasses it and is a privileged permission.
- **Browser sessions**: SSO sets a session cookie (`iam/sso.py`); cookie-authenticated writes
  require the `X-AutoAir-CSRF` header. WebSocket collaboration uses short-lived tickets that
  require DAG edit permission (`collab.py`).
- **AI service**: a separate `autoair ai-serve` trusts an HMAC-signed identity
  (`ai.service.shared_secret`, replay window in `ai/access.py`) or verifies Airflow tokens.
- **Code execution**: only where documented (`code:execute`, DagBag import checks, task/DAG
  test runs in a child process with a scrubbed environment, `sandbox.*`). Codegen emits AST,
  never source text from user values.

## Checklist

1. **AuthZ**: every new route/tool/WebSocket gated; deny by default (unknown MCP tools need
   `admin:config`); no permission check inside UI code only.
2. **CSRF**: state-changing endpoints reachable with a cookie session check the CSRF header;
   no `GET` with side effects.
3. **Secrets**: none stored (use `conn_id`); `SecretStr` for settings; masked in logs, audit,
   errors and `GET /api/config` (`secret_mask.py`, `install_log_masking`); request tokens
   (`X-AutoAir-Git-Token`) accepted only over HTTPS or loopback, sent only to `https://`, never
   cached, pickled or returned.
4. **Injection**: user values reach generated Python only as AST literals; YAML via a
   serializer; git arguments validated (`check_branch`) and passed as argv, never through a
   shell; Jinja handled with `jinja2.sandbox.SandboxedEnvironment` (`validation/jinja.py`,
   `scaffold/`).
5. **SSRF**: URLs from users or config (Airflow, git hosts, marketplace, AI, MCP, research)
   resolved once and pinned (`ai/net.py`: `check_url`, `PinnedAsyncTransport`); private ranges
   refused unless explicitly allowed (`allow_private`); a client that follows redirects must
   re-check each hop; TLS/proxy via `net/tls.py`.
6. **AI**: guardrails pipeline (`ai/guardrails.py`: secret redaction, PII masking, injection
   shield, budgets, tool allowlist) on input and output; tool results treated as untrusted;
   file access only through `ai/assistants/fs_sandbox.py` (allow-listed roots, `O_NOFOLLOW`,
   globs, size limits, scrubbing); writes proposed as diffs and applied through the normal
   deploy pipeline.
7. **Policy/OPA**: `policy.opa_fail_mode` defaults to `open` (warning when OPA is down);
   `closed` turns it into an error. A change must not silently switch a fail-closed path to
   fail-open.
8. **Resource limits**: request sizes, pagination, timeouts, rate limits
   (`limits.rate_limits`); no unbounded reads of user-supplied archives or repos.
9. **Marketplace**: Ed25519 signatures verified (`marketplace.py`); `require_signature`
   honoured.
10. **Dependencies**: new packages justified, pinned with a floor, optional extra when not
    core.

Report: severity, exploit scenario (who, what access, what gain), affected deployment modes
(plugin, `serve`, `ai-serve`, `local`, MCP stdio/HTTP), fix. Never include real secrets in
the report. Security issues found in released code go to the owner, not to a public issue.
