---
title: Security model
description: How AutoAir authenticates and authorises users, where DAG code executes, how secrets are handled, what the AI features can do, and a hardening checklist for production.
sidebar_label: Security model
---

AutoAir writes code that Airflow will execute. Its security model is built around three facts:

1. **Airflow is the identity provider.** AutoAir has no user database and no passwords of its own.
2. **A DAG is code.** Whoever can deploy a DAG can, eventually, run code on Airflow workers. AutoAir constrains what can be expressed and who can deploy it, but it is not a sandbox for hostile authors.
3. **AI output is untrusted input.** Model answers, retrieved documents and tool outputs go through the same validation and approval paths as human edits.

To report a vulnerability, see [Security reporting](/docs/security/reporting).

## Authentication

| Mode | Who signs users in | How AutoAir verifies |
|---|---|---|
| Plugin | Airflow's auth manager (session cookie `_token` or `Authorization: Bearer`) | Airflow's `get_user` dependency on every route |
| Standalone | Airflow: the browser holds an Airflow access token (session cookie on the same host, password exchange through `POST /api/auth/login`, or a pasted token) | `GET {airflow}/api/v2/dags?limit=1` with the token, cached for `auth_cache_ttl` seconds (60), never past the token's `exp` |
| Local (`autoair local`) | A random access token per run, handed to the browser in the URL fragment (or your own `--token` / `AUTOAIR_LOCAL__TOKEN`) | Constant-time comparison; the holder gets `local.roles` (Admin). `--no-token` serves without authentication (Viewer, loopback only) |

Properties that follow from this:

- Tokens are Airflow's JWTs; AutoAir forwards them to Airflow and neither re-signs nor re-scopes them. Session lifetime is bounded by Airflow's `[api_auth] jwt_expiration_time`.
- The `HttpOnly` session cookie never reaches JavaScript. Pasted or password-obtained tokens live in one tab's `sessionStorage`: they survive a reload of that tab, but are gone when it closes and are not shared with other tabs. Script running in the page (an XSS) could read them, so where possible sign in through Airflow ([SSO](/docs/admin/sso)), whose cookie scripts cannot read. AutoAir renders no raw HTML from AI answers or documents, loads no remote images from them, and keeps tokens out of URLs, logs and stored documents.
- Standalone password sign-in proxies the username and password to Airflow's `/auth/token` once and never stores them; failed attempts are rate limited per IP and user (shared across replicas with a SQL backend).
- Collaboration tickets are short-lived (60 s), single-use, and bound to the user and DAG. Open co-editing sessions are re-authorised at least every `collab.session_seconds` (default 300). Messages relayed between replicas are HMAC-signed with a key derived from the collaboration secret.
- Language-server WebSocket tickets (`POST /api/lsp/session`) are bound to the user, valid for 60 s and single-use on every replica (their nonce is claimed in the shared state, like collaboration tickets).
- State-changing requests authenticated by a cookie need the `X-AutoAir-CSRF` header and a same-origin (or allowed CORS) `Origin` (`sso.require_csrf_header`). This applies in standalone mode with SSO and in plugin mode, where Airflow 3.1+ accepts its `_token` cookie (`SameSite=Lax`). Bearer-token requests are exempt.
- `autoair serve --no-auth`, `autoair mcp-serve --http --no-auth`, `autoair mcp --http`, `autoair ai-serve --no-auth` and `autoair local --no-token` disable authentication. They exist for local experiments only: they refuse to bind a non-loopback address, answer only requests whose `Host` (and `Origin`, when sent) is a loopback address or the host of `server.public_url` (against DNS rebinding), and give callers `iam.anonymous_roles` (default Viewer, read-only).

Details: [SSO](/docs/admin/sso).

## Authorization

Two layers apply to every request:

1. **AutoAir permissions** (`builder:use`, `spec:read`, `audit:read`, `airflow:read`, `spec:write`, `spec:delete`, `code:execute`, `run:test`, `connection:write`, `catalog:refresh`, `ai:use`, `review`, `deploy`, `deploy:direct_push`, `promote`, `run:trigger`, `run:manage`, `template:write`, `icon:write`, `admin:deploy_target`, `admin:config`, `admin:panel`, `ai:admin`), grouped into built-in roles (Authenticated, Viewer, Author, Reviewer, Deployer, CodeDeployer, Operator, TemplateManager, Admin) and derived from the caller's Airflow roles through the configured strategies (`airflow_permissions`, `role_mapping`, `custom_view`).
2. **Airflow's own permissions.** Routes about one DAG also require Airflow's DAG access (`requires_access_dag`), honouring per-DAG permissions and multi-team. Every call AutoAir proxies to Airflow (pickers, runs, logs, connections) carries the caller's token, so Airflow's RBAC applies again.

Governance features on top: pull-request mode for git targets, `deploy:direct_push` granted only through trusted grants (never by wildcard, never to anonymous users), four-eyes approvals (`iam.require_approval`: nobody approves a proposal they made or a version they saved, and anonymous users never satisfy it), and a mandatory context block plus git trailers on every commit. A live delete needs `deploy` as well as `spec:delete`. MCP writes follow the same rules as REST: they open a pull request or store a proposal where REST would.

Storing hand-written Python needs `code:raw`: a raw code DAG, and a spec whose `source.code` (the kept hand-written file) is new or changed, on every save path (REST deploy, code mode apply, MCP save), whatever the source policy, because a later deploy may write that code as the DAG file.

In standalone mode the `Authenticated` baseline (`builder:use`, `airflow:read`, `ai:use`, `connection:write`, `run:trigger`, `run:manage`) is granted to every signed-in user by the `airflow_permissions` strategy; the Airflow calls behind these permissions still carry the caller's token, so Airflow's RBAC decides what they do.

Standalone mode cannot evaluate Airflow's per-DAG or configuration permissions locally; Airflow still enforces its RBAC on proxied calls, but for strict governance prefer plugin mode or `role_mapping` with verified token claims. MCP over HTTP resolves the caller with the same strategies, and every tool needs the permission of its REST counterpart (unclassified tools need `admin:config`); over stdio the server runs with the rights of the local user who started it. Full model: [IAM](/docs/admin/iam).

## Code execution boundaries

Code generation itself never executes user input: specs are typed models, and code is generated as a Python AST. Values typed into forms become literals; they cannot inject statements. Custom node kinds must also emit AST.

Some features do execute DAG code **on the AutoAir host**:

| Feature | What runs | Permission | Isolation |
|---|---|---|---|
| Import check (`/validate?import_check=true`, MCP `import_check_dag_spec`, deploy with `import_check_on_deploy`) | Imports the generated DAG file with a DagBag: module-level code, inline functions, decorators | `code:execute` (deploys skip it for users without it when `iam.import_check_requires_code_execute=true`, the default) | Warm worker subprocesses (`dagbag_pool`), timeout `import_check_timeout` (60 s), scrubbed environment; or a separate, locked-down [validator service](/docs/deployment/validator-service) (`validator.url`) with resource limits and no egress |
| Python DAG import (`/interchange/python/import`, MCP `import_python_dag`) | Executes an uploaded Python DAG file | `code:execute`, `allow_python_code` | Subprocess, timeout `python_import_timeout` (90 s), scrubbed environment |
| Code mode apply | Re-imports the edited DAG file into a spec | `spec:write`, `code:execute` (+ `code:raw` when the file differs from the document's `source.code`), `features.python_code` | As above |
| Test runs (`/runner/test-task`, `/runner/test-dag`) | Executes tasks, including operators that call external systems | `run:test` and `code:execute`, `features.test_runs`; `mode=db` also `admin:config` and `runner.allow_db_mode` (off by default) | On the [validator service](/docs/deployment/validator-service#test-runs) when `validator.url` is set (not `dag.test()`); else a fresh interpreter in its own process group with a hard timeout (`runner.default_task_timeout` 120 s, `runner.default_dag_timeout` 300 s, at most `runner.max_timeout` 600 s), scrubbed environment, its own PID namespace where the kernel allows (`runner.process_isolation`), `PR_SET_NO_NEW_PRIVS`, non-dumpable, and an audit hook against reading other processes' `/proc` entries ([the local sandbox](/docs/user-guide/runs#the-local-sandbox)) |
| IDE language servers (`features.ide_language_server`, off by default) | basedpyright or pylsp and bash-language-server analyse code | `builder:use` | Per-session processes with `lsp.memory_limit_mb`, `lsp.cpu_limit_seconds`, session caps (`lsp.max_sessions`, `lsp.max_sessions_per_user`) |
| ShellCheck, ruff | Static analysis of code text | `builder:use` | Subprocess with a timeout, scrubbed environment |

These subprocesses are **not a security sandbox**: they run as the same OS user, on the same network, with access to the same files as AutoAir. They prevent accidents (hangs, leaked environment secrets), not attacks. If authors are not trusted:

- withhold `code:execute` and `run:test` from them (validation remains static),
- run import checks and test runs in the [validator service](/docs/deployment/validator-service), so DAG code is imported and executed in a separate container without credentials (import checks also without network access),
- or run AutoAir standalone in a dedicated container with no credentials beyond what it needs, a read-only file system where possible, and an egress policy,
- and keep `allow_python_code=false` / `features.python_code=false` if inline Python is not needed.

### Environment scrubbing (`sandbox.*`)

Processes that run DAG code (import checks in the warm pool or a one-shot subprocess, the validator service's workers, Python DAG imports, code mode apply, blueprint schema previews, source-preserving round-trip checks, test runs, the IDE language servers) do not inherit AutoAir's secrets, Airflow's secrets or cloud credentials. Removed from their environment:

| Setting | Default | Removes |
|---|---|---|
| `sandbox.secret_env_pattern` | `TOKEN\|SECRET\|PASSWORD\|PASSWD\|API_KEY\|PRIVATE_KEY\|CREDENTIAL\|EXTRA_HEADERS` | `AUTOAIR_*` variables whose name matches (case-insensitive) |
| `sandbox.secret_env_patterns_extra` | `[]` | More `AUTOAIR_*` name patterns |
| `sandbox.scrub_env` | `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `GOOGLE_API_KEY` | These variables, any prefix |
| `sandbox.scrub_env_extra` | `[]` | More variables, for example `["VAULT_ADDR"]` |
| `sandbox.credential_env_pattern` | see below | Any variable whose name matches (case-insensitive) |
| `sandbox.credential_env_patterns_extra` | `[]` | More patterns over any variable name |
| `validator.secret_conn_id` | `autoair_validator` | `AIRFLOW_CONN_AUTOAIR_VALIDATOR`, the validator's HMAC key |

The default `sandbox.credential_env_pattern` removes:

- Airflow's secrets: `AIRFLOW__*` names containing `SECRET`, `FERNET`, `SQL_ALCHEMY_CONN`, `PASSWORD`, `PASSWD`, `TOKEN`, `PRIVATE_KEY`, `BROKER_URL`, `RESULT_BACKEND`, `ENGINE_ARGS` or `BASIC_AUTH`, names ending in `_KWARGS` or `_DSN`, and every `AIRFLOW__*_CMD` and `AIRFLOW__*_SECRET` indirection. Examples: `AIRFLOW__DATABASE__SQL_ALCHEMY_CONN`, `AIRFLOW__CORE__FERNET_KEY`, `AIRFLOW__API_AUTH__JWT_SECRET`, `AIRFLOW__API__SECRET_KEY`, `AIRFLOW__CELERY__BROKER_URL`.
- Connections and variables from the environment: `AIRFLOW_CONN_*`, `AIRFLOW_VAR_*`.
- Credential-like names with any prefix: a `SECRET`, `PASSWORD`, `PASSWD`, `TOKEN`, `API_KEY`, `ACCESS_KEY`, `PRIVATE_KEY`, `CREDENTIAL(S)`, `CONNECTION_STRING`, `SAS_TOKEN`, `ACCOUNT_KEY`, `SESSION_TOKEN`, `SECURITY_TOKEN`, `DSN`, `BASIC_AUTH`, `WEBHOOK(_URL)` or `BROKER_URL` word, or ending in `_PASS` / `_PWD` (`AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`, `AZURE_CLIENT_SECRET`, `AZURE_STORAGE_CONNECTION_STRING`, `VAULT_TOKEN`, `GITHUB_TOKEN`, `GOOGLE_APPLICATION_CREDENTIALS`).
- Any variable whose value is a URL with a password (`redis://:pw@host`, `https://user:pw@index/simple`), whatever its name; this includes an authenticated proxy URL.
- Cloud and database credentials by name: `AWS_ACCESS_KEY_ID`, `AWS_WEB_IDENTITY_TOKEN_FILE`, `AWS_CONTAINER_AUTHORIZATION_TOKEN*`, `AZURE_CLIENT_CERTIFICATE_*`, `AZURE_FEDERATED_TOKEN_FILE`, `AZURE_USERNAME`, `AZURE_STORAGE_KEY`, `CLOUDSDK_AUTH_*`, `PGPASSWORD`, `PGPASSFILE`, `MYSQL_PWD`, `DATABASE_URL`.

Non-secret Airflow configuration (`AIRFLOW_HOME`, `AIRFLOW__CORE__DAGS_FOLDER`, `AIRFLOW__CORE__EXECUTOR`, ...) and non-secret cloud settings (`AWS_DEFAULT_REGION`, `AZURE_TENANT_ID`) are passed through, so the DagBag loads. A DAG that reads a variable or connection at parse time, or a task test that needs a connection, gets it back through `sandbox.env_passthrough` (fnmatch patterns, for example `["AIRFLOW_VAR_ENVIRONMENT", "AIRFLOW_CONN_TEST_*"]`); AutoAir's own secrets and the validator key are removed regardless.

Exception: a DAG test run with `mode=db` (or `auto` when `db` is allowed for the caller) calls `dag.test()`, which writes the run to Airflow's metadata database from inside the child process. That child keeps `AIRFLOW__DATABASE__*` and `AIRFLOW__CORE__FERNET_KEY*`, so its DAG code can reach the metadata database and decrypt every connection; it gets a random per-run `AIRFLOW__API_AUTH__JWT_SECRET`, never the real one. This mode is therefore off unless `runner.allow_db_mode=true`, and then needs `admin:config` as well; `auto` runs in memory for everyone else ([dag.test() mode](/docs/user-guide/runs#dagtest-mode)).

Only the environment is scrubbed, except for test runs (below). Values Airflow reads from `airflow.cfg` in `AIRFLOW_HOME`, mounted secret files and anything else the AutoAir user can read stay readable by DAG code in import checks and Python imports. Keep secrets out of a config file that user can read, or use the [validator service](/docs/deployment/validator-service) for import checks. In plugin mode the Airflow API server's own process is not made non-dumpable (`server.harden_process` applies to AutoAir's own servers only, and AutoAir does not change Airflow's process), so any process of the same OS user can read the API server's full environment through `/proc/<pid>/environ`. With `validator.url` set, import checks and test runs (except `dag.test()`) run in the validator instead. Without it, a local test run is started in its own PID namespace with a fresh `/proc`, which hides the API server, where the kernel allows it (`runner.process_isolation`, default `auto` in every mode). On hosts without unprivileged user namespaces (Ubuntu 24.04+ by default, most containers) the run still executes, protected only by the audit hook, and is marked **Not fully isolated** (`process_isolated: false` in the result and the audit record, one server warning per process): a subprocess such as `cat /proc/$PPID/environ` could read the API server's environment. Set `runner.process_isolation=required` to refuse such runs. Test runs also get a copy of `airflow.cfg` without its secrets, cannot read the real one or `airflow.db`, and cannot write into the DAG folders. Local import checks in the warm pool get the parts of this that cannot break parsing a DAG file: the audit hook against reading other processes' `/proc` entries, other workers' and runs' folders and Airflow's secret files (`runner.audit_hook`), read-only DAG folders, and a private `AIRFLOW_HOME` with the secret-free `airflow.cfg` copy; no PID namespace. For plugin deployments, set `validator.url`, and grant `code:execute` only to users you trust with the API server's credentials.

Git commands (git deploy targets with `provider=local` or `gitea`, and repository bulk imports) get only what git, ssh and curl read: `PATH`, `HOME`, `USER`, `LOGNAME`, `SHELL`, `LANG`, `LC_*`, `TZ`, `TMPDIR`/`TEMP`/`TMP`, `XDG_CONFIG_HOME`, `XDG_CACHE_HOME`, `SSH_AUTH_SOCK`, `SSL_CERT_FILE`, `SSL_CERT_DIR`, `CURL_CA_BUNDLE`, the proxy variables (unless `outbound` disables `trust_env`), `GIT_*` and the Windows system variables. Add more with `storage.git.env_passthrough` (for example `["KRB5CCNAME"]`). The token used for a push travels in `GIT_CONFIG_*` variables of that one process; SSH keys from Airflow connections are written to owner-only files named after the key's hash, so users that share a clone cache never use each other's key.

### What DAG authors can express

| Control | Default | Effect |
|---|---|---|
| `allow_python_code` / `features.python_code` | `true` | Python task bodies, inline functions, embedded script files and Python references |
| `allow_privileged_operators` + `policy.privileged_operators` | `true`; `BashOperator`, `BashSensor` | Operators that run arbitrary commands |
| `python_ref_allow` | `["*"]` | Which Python objects may be referenced |
| `operator_allow`, `operator_deny`, `forbidden_operators` | all allowed | Palette and validation allow/deny lists |
| Policy rules | none | Organisation rules evaluated on validation and deploy ([Policy](/docs/admin/policy)) |

Remember that operators such as `KubernetesPodOperator`, `SSHOperator` or `DockerOperator` also run arbitrary code; add them to `policy.privileged_operators_extra` if that matters in your environment.

### Deploy targets

AutoAir never overwrites files it did not create, refuses to overwrite hand-edited generated files unless forced, and uses optimistic locking. Git targets in PR mode put a human review between an author and the branch Airflow reads.

## Secrets

| Secret | Handling |
|---|---|
| Airflow connections and variables | Never read or returned by AutoAir's API (Airflow's REST API does not return passwords). Pickers list ids only. Per-user git credentials are resolved server-side for one write and dropped |
| Git credentials | Environment (`storage.git.token`, GitHub App key) or an Airflow connection. The deploy-target dialog and API refuse to store tokens or keys |
| AutoAir settings marked secret | `SecretStr`: never logged; `GET /api/config` masks them (`expose_config=true`) or omits them (`non-sensitive-only`); the endpoint is off by default and admin-only |
| Per-user AI keys | Encrypted at rest with Fernet (`ai.user_llm.encryption_key`); never returned by the API |
| Admin-defined MCP server headers | Encrypted at rest, never returned |
| Hard-coded secrets in DAGs | Validation warns on literal values of parameters whose name contains `password`, `passwd`, `secret`, `token`, `api_key`, `access_key`, `private_key` (each also with `-` or no separator) or `credential` (names ending in `_conn_id`, `_id`, `_name`, `_path`, `_file`, `_field`, `_url` or `_ref`, and `token_url`, are exempt), and on secret-looking assignments in inline code. Patterns: `policy.secret_param_pattern`, `policy.secret_param_patterns_extra`, `policy.secret_param_exclude_pattern`, `policy.secret_param_exclude_patterns_extra`, `policy.secret_assignment_pattern`, `policy.secret_assignment_patterns_extra` |

Set `AUTOAIR_COLLAB_SECRET`, `AUTOAIR_AI__USER_LLM__ENCRYPTION_KEY` and `AUTOAIR_AI__SERVICE__SHARED_SECRET` explicitly from a secret store in production, and back them up with the database.

## AI risks

AI features are optional (`features.ai`). When enabled:

| Risk | Mitigation |
|---|---|
| Sensitive data sent to a model provider | Secrets are redacted from everything sent to a model (`ai.guardrails.redact_secrets`, plus site patterns); optional PII masking; self-hosted models for full control |
| Prompt injection through retrieved documents, logs or tool outputs | `ai.guardrails.prompt_injection` (`flag` by default, `block` available); retrieved content is wrapped as data, and tag-like text that could close the wrapper is neutralised; AI Markdown loads no remote images (they are shown as links), so an injected image URL cannot carry data away |
| Agents taking actions | Tool allow-lists per AI role (`ai.guardrails.tool_allowlist`) and per assistant; [tool-call approval](/docs/ai/tool-approval) before every call that changes something or runs code on the server (`import_python_dag`, `deploy_dry_run`; those also need `code:execute`), bound to the exact arguments shown; external MCP tools wait unless an administrator lists them in the server's `read_only_tools` (the server's `readOnlyHint` is not trusted); proposals are validated server-side and applied only after the user approves them in the editor; deploys go through the normal pipeline |
| Poisoned shared memory | Every `remember` call waits for approval; global and team memories need `admin:config`; users add and delete only their own personal memories |
| Generated code presented as valid | `ai.guardrails.check_generated_code`: writers must pass validation, ruff and ShellCheck |
| SSRF through user-defined model endpoints | `ai.user_llm.allowed_base_urls`, `allow_private_networks=false` by default |
| Online research | Off by default (`ai.research.online=false`); when on, limited to `ai.research.allowed_domains` with size and rate limits |
| MCP clients writing | `save_dag_spec`, `delete_dag_spec`, `trigger_dag` need `confirm=true`; `ai.mcp.allow_writes=false` makes MCP read-only. Over HTTP each tool needs the permission of its REST counterpart (`src/autoair/mcp/iam.py`); stdio runs with the starting user's rights |
| Cost and abuse | Rate limits (`limits.rate_limits.ai`), agent step, token and cost budgets, AI audit log |

Guardrails reduce risk; they are not an execution boundary. Details: [Guardrails](/docs/ai/guardrails) and [AI architecture](/docs/ai/overview).

## HTTP and network

- Standalone sends a Content-Security-Policy, `X-Content-Type-Options`, `Referrer-Policy` and frame-ancestors by default (`server.security_headers`); HSTS with `server.hsts_seconds`.
- CORS is closed by default (`cors_origins=[]`); `*` never sends credentials.
- Forwarded headers are trusted only from `server.forwarded_allow_ips`.
- Request bodies are capped (`server.max_body_bytes`, 16 MiB); expensive routes are rate limited per user and role.
- All outgoing clients share the `outbound` TLS settings ([reference](/docs/reference/configuration#outbound)); `outbound.verify=false` logs a warning for every client.

See [TLS and HTTP headers](/docs/admin/tls-and-headers).

## Audit

Deploys, pull requests, direct pushes, deletes, promotions, proposals, approvals and denied actions are written to the audit trail with user, DAG, spec etags and validation result; AI calls are written to the AI audit log. Commits carry `X-AutoAir-User`, `X-AutoAir-Dag`, `X-AutoAir-Spec-Etag`, `X-AutoAir-Validation`, `X-AutoAir-Version` and the other trailers listed in [Git bundles](/docs/deployment/git-bundles), so `git log` shows who deployed what. See [Audit](/docs/admin/audit).

## Hardening checklist

- [ ] Run the plugin, or `autoair serve` without `--no-auth`; never expose `autoair local` beyond `127.0.0.1` without `--token`; do not share its per-run sign-in link.
- [ ] Terminate TLS; set `AUTOAIR_SERVER__HSTS_SECONDS` once every client uses HTTPS; restrict `AUTOAIR_SERVER__FORWARDED_ALLOW_IPS` to your proxies.
- [ ] Plugin mode: run the [validator service](/docs/deployment/validator-service) (`validator.url`, `fail_mode=closed`) so import checks and test runs never execute in the Airflow API server; keep `runner.allow_db_mode=false`.
- [ ] Map Airflow roles to AutoAir roles explicitly (`iam.strategies`, `iam.role_mapping`); give `code:execute`, `run:test` and `deploy` only to people who may run code on your platform.
- [ ] Use git PR mode for production (`storage.git.mode=pr`), keep `deploy:direct_push` to a small group, and consider `AUTOAIR_IAM__REQUIRE_APPROVAL=true`.
- [ ] Decide on `allow_python_code`, `allow_privileged_operators`, `python_ref_allow` and `policy.privileged_operators_extra` for your threat model.
- [ ] Add platform credentials present in the AutoAir container to `AUTOAIR_SANDBOX__SCRUB_ENV_EXTRA`.
- [ ] Keep `AUTOAIR_EXPOSE_CONFIG` at `false` or `non-sensitive-only`.
- [ ] Set `AUTOAIR_COLLAB_SECRET`, `AUTOAIR_AI__USER_LLM__ENCRYPTION_KEY` and `AUTOAIR_AI__SERVICE__SHARED_SECRET` from a secret store; rotate with a plan.
- [ ] Use a dedicated PostgreSQL database or schema for AutoAir with its own role and `ssl_mode=verify-full`.
- [ ] Prefer GitHub App credentials or per-user connections (`storage.git.credentials_mode=per_user`) over a long-lived shared token; use `storage.git.forced_conn_id` to pin the connection.
- [ ] If AI is enabled: choose the provider deliberately, keep `ai.research.online=false` unless needed, review `ai.guardrails.tool_allowlist`, set `ai.mcp.allow_writes=false` if agents should only read, and consider the separate AI service with an egress policy.
- [ ] Pin marketplace sources with `public_key` and keep `require_signature=true`.
- [ ] Ship logs (`AUTOAIR_LOGGING__FORMAT=json`), monitor the audit trail, and test restores ([Backup and restore](/docs/admin/backup-restore)).

## Related pages

- [IAM](/docs/admin/iam)
- [SSO](/docs/admin/sso)
- [Policy](/docs/admin/policy)
- [Security reporting](/docs/security/reporting)
