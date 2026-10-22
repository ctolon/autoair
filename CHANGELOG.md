# Changelog

All notable changes to AutoAir are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Until 1.0.0,
minor versions may contain breaking changes; they are listed under **Changed** or **Removed**.

## [Unreleased]

## [0.2.3] - 2026-10-22

### Security
- Standalone token verification accepts only Airflow's own answer: a `200` with the DAG list.
  A redirect (an SSO proxy or a wrong `airflow_url` sending the check to a login page), another
  2xx or an unexpected body used to count as a verified token, so a forged JWT's role claims
  could be trusted. Such answers now fail closed with a 502 that names the URL.

- Processes that run DAG code (import checks, the validator's workers, Python imports, blueprint previews, test runs, language servers) no longer inherit Airflow's secrets or cloud credentials: `AIRFLOW__DATABASE__SQL_ALCHEMY_CONN`, `AIRFLOW__CORE__FERNET_KEY`, `AIRFLOW__API_AUTH__JWT_SECRET`, every `AIRFLOW__*SECRET*`/`*_CMD` setting, `AIRFLOW_CONN_*`, `AIRFLOW_VAR_*`, `AWS_*` keys, `GOOGLE_APPLICATION_CREDENTIALS`, `AZURE_*` secrets and other credential-like names are removed (`sandbox.credential_env_pattern`, new). Before, only `AUTOAIR_*` secrets were. Variables whose value is a URL with a password are removed whatever their name. DAG test runs with `mode` `db`/`auto` keep the metadata-DB settings and Fernet key `dag.test()` needs, with a random per-run JWT secret instead of Airflow's. Upgrade note: DAGs that read a variable or connection from the environment at parse time, and task tests that need one, get it back with `sandbox.env_passthrough` (new, fnmatch patterns, e.g. `["AIRFLOW_VAR_ENVIRONMENT"]`).
- Git commands of `provider=local`/`gitea` targets and repository bulk imports inherit only what git, ssh and curl read (`PATH`, `HOME`, locale, `TMPDIR`, `SSH_AUTH_SOCK`, CA and proxy variables, `GIT_*`), not the whole server environment; `storage.git.env_passthrough` (new) adds names such as `KRB5CCNAME`. SSH private keys from Airflow connections were cached in one `id_autoair` file per clone cache and never rewritten, so a user whose connection shared that cache (Gitea per-user credentials, a rotated key) pushed with the first user's key; each key now gets its own owner-only file named after its hash.
- Recycling a merged pull-request branch moved it with `git push --force`, discarding a commit pushed to it meanwhile; it now pushes with `--force-with-lease` on the head it saw (Gitea / local git; Azure DevOps uses the ref update's compare-and-swap, GitHub, GitLab and Bitbucket re-check the head first) and starts over when the branch moved.
- Deploy targets fail closed when AutoAir's state record (`.autoair/state/<dag_id>.json`) is unreadable: no file of that DAG is replaced or deleted (409 naming the file and how to recover) instead of the record being ignored, which disabled the hand-written and hand-edited (ejected) file protection. Without a state record, an existing DAG file that differs from the new one counts as hand-edited and is replaced or deleted only with force (an untouched loader stub excepted).
- The hard-coded secret scan (`policy.secret_assignment_pattern`) also finds secrets passed inline, such as keyword arguments (`Connection(password="...")`), statements after `;` and quoted command-line options; it was anchored to the start of a line. Deployments that override the pattern keep their own.
- The hard-coded secret scan also finds secrets in dict literals and `dict()` calls in Python code (inline functions, `@task` bodies, raw code DAGs, hand-written parts of source-preserving deploys): an entry whose key matches `policy.secret_param_pattern` (and not the exclude pattern) and whose value is a string literal, such as `{"token": "ghp_..."}` or `dict(password="...")`, is a `secret` warning on the key's line. Values that are not plain literals (variables, `os.environ[...]`, `Variable.get(...)`, f-strings), Jinja or format placeholders, empty or short strings, environment variable names (`GITHUB_TOKEN`) and placeholders (`<your-token>`, `changeme`, `********`) are not reported.
- Standalone `autoair serve` with the default `airflow_permissions` strategy: Airflow's DAG and configuration permissions cannot be checked there, and an unchecked permission counted as granted, so every user with a valid Airflow token (an Airflow Viewer included) got the Author, Deployer and Admin roles. An unchecked permission now grants only `iam.airflow_permission_roles.unverified` (new, default `["Viewer"]`); plugin mode, where Airflow answers these checks, is unchanged. Upgrade note: standalone deployments that relied on this grant roles explicitly with `iam.strategies=["role_mapping"]` (the verified token's role claim, alone or together with `airflow_permissions`) or `iam.default_roles`.
- Servers without authentication (`autoair serve --no-auth`, `autoair mcp-serve --http --no-auth`, `autoair mcp --http` / `autoair-mcp --http`, `autoair ai-serve --no-auth`, `autoair local --no-token`) refuse to bind a non-loopback address and answer only requests whose `Host` (and `Origin`, when sent) is a loopback address or the host of `server.public_url`, against DNS rebinding and cross-site pages. `iam.anonymous_roles` defaults to `["Viewer"]` (was `["Admin"]`). Upgrade note: development setups that let anonymous callers edit and deploy set `AUTOAIR_IAM__ANONYMOUS_ROLES='["Admin"]'`.
- `autoair local` is authenticated by default: it creates a random access token on every start, prints a sign-in link and opens the UI with it (`/ui/#token=...`, kept in the URL fragment, which never reaches a server). The token holder gets `local.roles` (new, default `["Admin"]`), so the local user keeps editing and deploying; other programs and web pages on the machine can no longer use the API. `--token` / `AUTOAIR_LOCAL__TOKEN` still set the token; the new `--no-token` serves without authentication with `iam.anonymous_roles` (Viewer).
- Test runs (`POST /runner/test-task`, `/runner/test-dag`) execute the DAG's Python on the server and now need `code:execute` in addition to `run:test`, like every other code-running route. The built-in roles are unaffected (every role with `run:test` also holds `code:execute`); custom roles granting `run:test` alone lose test runs.
- Plugin mode: state-changing requests authenticated by Airflow's `_token` cookie (Airflow 3.1+) now need the `X-AutoAir-CSRF` header and a same-origin or allowed `Origin`, as standalone cookie sessions already did. `sso.require_csrf_header` controls it in both modes. The bundled UI already sends the header; scripts that use the browser cookie instead of a bearer token must add it.
- Saving a template with hand-written DAG code (a raw code DAG, `spec.raw_code`, or a kept Python file, `spec.source.code`) through `POST /api/templates` or a marketplace install now needs `code:raw`, like saving that spec. Upgrade note: TemplateManager alone can no longer publish such templates.
- Collaboration tickets are bound to the requesting user and the room, open a single connection (claimed in the shared state store, so this holds across replicas) and are valid for 60 s by default (`collab_ticket_ttl`, was 600).
- Language-server WebSocket tickets (`POST /api/lsp/session`) could open any number of sockets within their 60 s TTL; like collaboration tickets they now name the user and open one socket, claimed in the shared state store (`documents` collection `lsp_tickets`, or `<state>/lsp-tickets/`), so a leaked ticket cannot be replayed on any replica.
- `autoair mcp --http` (and `autoair-mcp --http`) served every tool without authentication and without the loopback and DNS-rebinding protections of the other unauthenticated servers. It now refuses a non-loopback `--host`, checks `Host` and `Origin` like `mcp-serve --http --no-auth`, and gives callers `iam.anonymous_roles` (default Viewer, read-only). Upgrade note: a local agent that saves or deploys through it needs `AUTOAIR_IAM__ANONYMOUS_ROLES='["Admin"]'` (or use stdio, `autoair mcp`, which acts as the local user).
- Storing hand-written Python in a spec (`source.code`, the file a DAG was imported from or edited as in the code mode) needs `code:raw` when the code is new or differs from the stored spec's, whatever the source policy: `PUT /api/specs/{dag_id}` answers 403, the code mode apply (`POST /api/interchange/python/sync`) answers 403 when the applied file differs from the document's code, and MCP `save_dag_spec` refuses the save. Before, only a deploy that wrote that code as the DAG file needed it, so a user without `code:raw` could store code that a later deploy by someone else wrote. This replaces the deploy's `source_refused` answer for such users (the Deployer role included) with the save-time 403; deploying the generated file instead now means saving with the source policy `generate` and without `source.code`. Upgrade note: custom roles that edit in the code mode or import Python DAGs and deploy them need `code:raw` (Author, CodeDeployer and Admin have it).
- Four-eyes approvals (`iam.require_approval`): AutoAir now records who saved each stored spec version (shared state: `documents` collection `spec_version_authors`, or `<proposals URL>-authors/`), so the saver of a version can no longer approve it (403) and their approval never counts towards putting it live, whoever deploys it; a version saved anonymously never satisfies four-eyes. Before, a version stored without a proposal (a pull request, a save made before approvals were required) could be approved by its own author and deployed by someone else. Versions saved before this release have no record and keep the previous rule (the deploying user counts as the author).
- MCP `validate_dag_spec` imported (executed) the generated DAG with `import_check=true` while annotated read-only, so MCP clients ran it without asking. The import check is now its own tool, `import_check_dag_spec` (`RUNS_CODE`, needs `code:execute`); `validate_dag_spec` only checks statically and ignores `import_check` (its result's `note` says so). Upgrade note: agents that passed `import_check=true` call `import_check_dag_spec`.
- Live-editing connections are closed after `collab.session_seconds` (new, default 300); the browser reconnects with a new ticket, which re-checks DAG edit permission, so revoked permissions end co-editing within that interval.
- Messages relayed between replicas (`collab.sync` database or redis) are signed with HMAC-SHA256 under a key derived from the collaboration secret; unsigned or foreign messages are dropped. Upgrade note: every replica must share the collaboration secret (as tickets already required); during a rolling upgrade, messages from replicas still on the old version are dropped until they restart.
- DAG deletes follow the deploy governance. On git targets they follow the target's mode and `iam.force_pr_without_direct_push` (a pull request removes the file); a delete that goes live needs `deploy` as well as `spec:delete`, and with `iam.require_approval` another user's approval: without one it is stored as a delete proposal (HTTP 202, `status: "pending_approval"`), executed with `POST /specs/{dag_id}/proposals/{etag}/delete`. Before, a delete went straight to the branch. Upgrade note: custom roles with `spec:delete` but without `deploy` can no longer delete live; they still can through a pull request.
- A deploy proposal is approved, deployed or executed only while the stored spec is still the version it was made from: otherwise `POST /specs/{dag_id}/reviews` (approve), `POST .../proposals/{etag}/deploy` and `POST .../proposals/{etag}/delete` answer `409` naming both etags, and `GET .../proposals/{etag}` reports `stale`. A proposal without a `base_etag` used to overwrite the DAG blindly once it existed (a proposal for a then-new DAG, or one stored by 0.2.2 or earlier); it is now refused the same way. Upgrade note: pending proposals of existing DAGs from 0.2.2 must be proposed again.
- MCP writes follow the same rules as the REST deploy and delete routes: a confirmed `save_dag_spec`, `save_raw_dag_code` or `delete_dag_spec` over HTTP opens a pull request where REST would (a PR-mode target, or `iam.force_pr_without_direct_push` for a caller without `deploy:direct_push`); a write that goes live needs `deploy`, and under `iam.require_approval` it needs another user's approval of that version, else the tool stores a deploy or delete proposal like REST (`status: pending_approval`, `proposal_etag`). Results carry `status` (`saved`, `deleted`, `pr_opened`, `pending_approval`). Like `PUT /specs`, opening a pull request or a proposal needs only `spec:write` (`spec:delete` for deletes), so these tools now show for Authors.
- AI agents: tools that run Python on the AutoAir server (`import_python_dag`, `deploy_dry_run`) were read-only tools and ran without approval, so prompt-injected text in a log or document could make the agent execute code. They form a new tool class `execute`: their calls wait for approval (the `write` class gates them too, so existing `ai.approvals` rules apply), and they are offered only to callers with `code:execute`, whatever `ai.guardrails.tool_allowlist` says. The MCP tool `import_python_dag` is no longer annotated read-only, so MCP clients ask before calling it.
- External MCP servers' `readOnlyHint` is no longer trusted: a compromised server could mark a destructive tool read-only to skip the approval. Only tools an admin lists in the server's new `read_only_tools` (fnmatch, configuration and the **MCP servers** tab) run without approval or reach the admin assistant. Upgrade note: tools that ran directly because the server marked them read-only now wait for approval until you add them to `read_only_tools`.
- AI memory: global and team memories reach every user's prompts, so adding them (`remember` with scope `global` or `team`, MCP and the assistants) and deleting them (`forget`) need `admin:config`; `forget` deletes a personal memory only for its owner (before, anyone with `ai:use` could delete any memory). Every `remember` call of AutoAir's assistants waits for approval; `ai.approvals.*.exempt` no longer applies to it. Upgrade note: rules that exempted `remember` now get an approval card.
- SSH private keys from git connections are cached as `<cache_dir>/keys/id_autoair-<hash>` (mode 0600) for the git CLI; files of keys nobody used for `storage.git.ssh_key_cache_days` (new, default 30; 0 keeps them) are removed whenever a new key file is written. Only AutoAir's own key files owned by the server's user are removed, never the key being used; using a key refreshes its age, and a removed key is written again on its next use.
- Untrusted text wrapped for the model (documents, logs, tool results) could escape the `<untrusted-data>` block with a closing tag that had no `>` (the 0.2.2 ReDoS fix left unclosed tag starts in place). Every remaining `<untrusted-data` / `</untrusted-data` start is now neutralised, still in linear time.
- Tool-call approval is bound to the exact arguments: the card shows how many characters are cut and offers **Show full arguments** (`GET /api/ai/approvals/{id}/calls/{call_id}/arguments`, secrets masked), and an approval carries the arguments' SHA-256 (`arguments_sha256`); the run resumes only when the stored call still has exactly those arguments (409 and `<feature>:tool_approval_mismatch` in the audit log otherwise). API clients that approve calls must send `arguments_sha256` from the pending call; approvals paused by 0.2.2 can only be denied.
- AI answers, troubleshooting reports, knowledge-base and documentation previews and other rendered Markdown no longer load remote images; they are shown as links, so a prompt-injected image URL cannot send conversation data to another server (inline raster `data:` images still display).
- Plugin mode: DAG code in a test run could read the Airflow API server's environment (its database URL, Fernet and JWT secrets, connections) through `/proc/$PPID/environ`, because the API server's process stays dumpable and AutoAir does not change it. Test runs now (1) run on the validator service when `validator.url` (or the new `validator.run_url`) is set, through a new signed `POST /v1/run` with the same request and response signing, body limit, timeouts, metrics (`autoair_validator_runs_total`, `autoair_validator_run_seconds`) and a per-run log line as `/v1/check`, at most `validator.max_concurrent_runs` (new, 2) at a time and `validator.max_run_seconds` (new, 600) long; and (2) locally, start in their own PID, user and mount namespaces with a fresh `/proc` where the kernel allows (`runner.process_isolation`, new, `auto`/`required`/`off`), with `PR_SET_NO_NEW_PRIVS`, no core dumps, non-dumpable, and an audit hook that denies reading other processes' `/proc/<pid>` entries and other runs' folders (`runner.audit_hook`, new, on). Subprocesses, network and writes stay allowed in test runs (the "run" profile of `validation/sandbox.py`; import checks keep the stricter "check" profile). Where unprivileged user namespaces are restricted (Ubuntu 24.04+, most containers) `auto` falls back to the audit hook, which does not cover subprocesses, and each run's log says so: use the validator in plugin mode. Upgrade notes: upgrade the validator together with the server (an older validator answers 404 for `/v1/run` and test runs fail with "upgrade it"); with a validator configured, test runs go there by default (`validator.test_runs`, new, `true`), where they have network unless `validator.run_network` (new) is `false`, but the shipped NetworkPolicies deny all egress, so tasks that call services fail until you deploy a second validator for runs with its own egress policy (Helm `networkPolicy.egress`, new) and set `validator.run_url`; when the validator cannot be asked, `fail_mode` `open` and `closed` both fail the test run (it never falls back to the local server) and only `local` runs it locally.
- Local test runs no longer see Airflow's secrets in `airflow.cfg` or write into DAG folders: each run gets a private `AIRFLOW_HOME` with a copy of `airflow.cfg` without its secrets (Airflow's `sensitive_config_values` and options named like passwords, secrets or `_cmd` indirections), reading the real `airflow.cfg`, `airflow.db`, `webserver_config.py` and the simple auth manager's password file is denied, and Airflow's DAG and plugin folders and AutoAir's DAG folder are read-only (audit hook; in the PID namespace also mounts, for subprocesses too, together with a mask over other runs' folders). Before, a test could read the Fernet key from `airflow.cfg` or drop a DAG file that Airflow parsed without review. `runner.process_isolation` defaults to `auto` in every mode, the Airflow plugin included: where the host does not allow the namespaces the run still executes with the audit hook, read-only DAG folders and the secret-free `airflow.cfg` copy, its result shows a non-blocking **Not fully isolated** notice ("configure validator.url for isolated test runs"), and the server logs one warning per process; `required` stays available and refuses such runs. The `test_run` audit record and the run result (new `where` and `process_isolated` fields of `TaskTestResult` / `DagTestResult`) carry where the code ran and whether it had its own PID namespace. Validator test runs are retried only when they surely did not run (no second run after a timeout, a lost connection or a proxy 502/504, and no local fallback then), `RunRequest` ids must match the spec's key pattern, and `validator.runs_enabled` (new) lets an import-check-only validator refuse `/v1/run`. Upgrade note: on hosts without unprivileged user namespaces (Ubuntu 24.04+, most containers) local test runs, in the plugin too, are only partly isolated and say so: configure `validator.url` for isolated test runs, or set `AUTOAIR_RUNNER__PROCESS_ISOLATION=required` to refuse them; tests that relied on secrets from `airflow.cfg` or wrote into the DAG folder now fail.
- Local import checks (the warm DagBag pool used without `validator.url`) could read the server's environment through `/proc/$PPID/environ` (in the plugin, the Airflow API server's database URL, Fernet and JWT secrets) and Airflow's secrets in `airflow.cfg`, and write into DAG folders. Pool workers now get the protections of local test runs that cannot break parsing a DAG file: the run profile of the audit hook (`runner.audit_hook`: no reading other processes' `/proc/<pid>` entries, other workers' and runs' folders or Airflow's secret files, no writes into Airflow's DAG and plugin folders or AutoAir's output folder), non-dumpable workers, and a private `AIRFLOW_HOME` with a secret-free copy of `airflow.cfg` (Airflow's DAG and plugin folders are kept). The pool stays warm; it gets no PID namespace. Upgrade note: a DAG file that reads `airflow.cfg` secrets, the metadata DB or another process's `/proc` entry at parse time now fails the local import check.
- DAG test runs with `mode=db` (`dag.test()`) hand the DAG code the metadata DB connection and the Fernet key. They are now off by default (`runner.allow_db_mode`, new, `false`, read per request) and, when allowed, need `admin:config` in addition to `run:test` and `code:execute` (audited 403 otherwise); `mode=auto` picks `dag.test()` only for those callers and otherwise runs in memory. `GET /api/meta` reports `test_runs` (`on_validator`, `db_mode_unavailable`) and the test dialog disables **dag.test() (writes a run)** with the reason. Every test run is recorded in the audit trail (`test_run`). Upgrade note: set `AUTOAIR_RUNNER__ALLOW_DB_MODE=true` to keep `dag.test()` for administrators; API clients that send `mode=db` otherwise get 403, and `mode=auto` now runs in memory for them.

### Changed
- Cron and interval schedules with `data_interval: false` are generated as explicit
  `CronTriggerTimetable` / `DeltaTriggerTimetable` instead of a bare cron string or
  `timedelta`, whose meaning Airflow's `[scheduler] create_cron_data_intervals` /
  `create_delta_data_intervals` decided. Regenerated DAG files change accordingly.
- Spec reads (`GET /api/specs`, `GET /api/specs/{dag_id}`, version history and diffs) take
  an `environment` query parameter and default to `default_environment`, so git targets read
  that environment's branch. `server.airflow_list_max_entries` (50000) bounds paged Airflow
  lists.

- Object-storage targets (`storage.mode=object`) serialise writers of one DAG with a lease in AutoAir's state backend (`storage.object_lock_seconds`, `storage.object_lock_wait_seconds`, new): two replicas that loaded the same version can no longer both save; the second gets a conflict. The lease is shared across hosts on a SQL `database.backend`. On `file://` URLs objects are written to a temporary file and renamed into place.
- `DELETE /api/specs/{dag_id}` returns `status` (`deleted`, `not_found`, `pending_approval`, `pr_opened`), `proposal_etag` and `message`; deploy proposals carry `action` (`deploy` or `delete`).
- Overwriting an existing DAG needs the etag it was loaded with: `PUT /api/specs/{dag_id}` without `base_etag` or `If-Match` answers `428 Precondition Required` (the `ETag` header and the message carry the current etag) instead of silently replacing a concurrent edit; creating a new DAG needs none. MCP `save_dag_spec` and `save_raw_dag_code` take `base_etag` (the dry run returns `current_etag`, a save the new `etag`) and refuse an overwrite without it, returning `current_etag` so the agent can retry. The editor sends the etag as before; deploying a new document onto a dag_id that already exists opens the merge dialog. Upgrade note: API clients that overwrite without an etag send the one from `GET /api/specs/{dag_id}`, or set `AUTOAIR_STORAGE__REQUIRE_BASE_ETAG=false` (new `storage.require_base_etag`, default `true`).
- Deleting an existing DAG needs the etag it was loaded with as well: `DELETE /api/specs/{dag_id}` without `If-Match` answers `428 Precondition Required` (the `ETag` header and the message carry the current etag) and a stale one `409`, after the permission checks and before a delete proposal is stored. MCP `delete_dag_spec` takes `base_etag` (its dry run returns `current_etag`) and refuses a delete without it or with a stale one, returning `current_etag`. The editor sends the etag of the loaded version, also when a renamed DAG's old files are removed. Upgrade note: API clients that delete without an etag send `If-Match` with the etag from `GET /api/specs/{dag_id}`, or set `AUTOAIR_STORAGE__REQUIRE_BASE_ETAG=false`, which now also relaxes deletes.
- MCP saves to a git target in PR mode are allowed under `iam.require_approval`: the pull request is the review. MCP writes that would go live under four-eyes, or push while pull requests are forced, are no longer refused: they store a proposal or open a pull request (see Security).
- The production Kustomize overlay (`overlays/production/settings/settings.env`) and the Helm example values (`deploy/helm/values-autoair.yaml`) set `validator.fail_mode=closed`: deploys are refused while the validator service cannot be asked. The application default stays `open`. Upgrade note: copies of the production overlay that should keep deploying while the validator is down set `AUTOAIR_VALIDATOR__FAIL_MODE=open` in their `settings.env`.

### Fixed
- Python DAG import: positional TaskFlow arguments (`multiply(7)`) are bound to their
  parameters instead of falling back to the defaults; keyword-only and positional-only
  parameters and their literal defaults are kept; `*args` / `**kwargs` and non-literal
  defaults are reported instead of being dropped silently.
- Python DAG import: TaskFlow bodies keep the DAG file's imports they use (`import math`
  at the top of the file), so the regenerated task no longer fails with NameError and the
  imported spec validates without F821; other module-level names are reported.
- Python DAG import: `CronTriggerTimetable(interval=..., run_immediately=True)` keeps its
  data window and `run_immediately` (read from the Task SDK's private attribute where
  needed).
- Inline functions that bind the same import alias to different objects (`floor as calc`,
  `ceil as calc`) no longer share one module-level import: the later function imports inside
  its own body.
- Version diffs tell an absent key from an explicit null (`execution_timeout: null`
  overrides a DAG default): `FieldChange` gains `before_present` / `after_present`, and the
  diff view shows "(not set)".
- A DAG saved only to the default environment's git branch is listed and opened (it was 404
  and missing from the list).
- Airflow connections, variables, pools and import errors are read page by page until
  `total_entries` (Airflow clamps the page size), not only the first page.
- Object storage deploys: a writer whose write lease expired mid-save no longer undoes the
  save of the replica that took the lease over (its spec could revert while the DAG file kept
  the newer version). It takes the lease back, waits for that save to finish, and restores
  only paths that still hold its own content; undo after any other failure is conditional the
  same way.

- DAG test runs with `mode=db` (`dag.test()`) failed on every Airflow version: on 3.1+ with "the dag is not serialized" (the generated DAG lives outside every configured DAG bundle, so `dag.test()` never found it), on 3.0 every task with an execution-API validation error (Airflow 3.0's Task SDK client sends JSON without a `Content-Type`, which current FastAPI rejects), and on 3.2 every task with "'State' object has no attribute 'svcs_registry'" (the in-process execution API's lifespan ended when its object was garbage-collected). The run now writes the DAG to the metadata DB under the first configured bundle when it is not serialized yet (a deployed DAG of the same id is left alone; the DAG processor later marks the test DAG stale) and works around both execution-API issues on the versions that have them.
- **Test selected task…** / **Test whole DAG…**, **Trigger DAG** (raw JSON) and **Backfill**: with invalid conf JSON the run buttons stayed enabled and sent the last valid conf; they are now disabled while the JSON does not parse or is not an object, with the reason next to the field (and next to the run buttons in the test dialog).
- A deploy to a local folder or object storage whose DAG file write failed left the new spec, etag and history entry in place with the old DAG file; the files already written are now restored, so nothing advances.
- A rendering error outside the builder (start-up, sign-in, providers) now shows a recoverable message with **Try again** and **Reload page** in the plugin and the standalone UI, instead of an empty page.
- The four-eyes rule is computed in one place (`autoair.iam.proposals`); the deploy route, the proposal routes, the admin overview and MCP used to compute it differently. Proposals without an author, and anonymous callers, never satisfy an approval, and the admin overview shows `require_approval` as off when `features.iam` is off.
- Backup and restore docs: the PostgreSQL examples used an undefined `$AUTOAIR_DB_URL`; they now derive the URL from `AUTOAIR_DATABASE__URL` (and `AUTOAIR_DATABASE__PASSWORD`).
- Python import: task `retry_delay` and `execution_timeout`, `retries=0` (or any value equal to Airflow's default) overriding `default_args`, edge labels (`a >> Label("x") >> b`, also on edges from or to task groups), `dagrun_timeout`, `is_paused_upon_creation`, `fail_fast`, `max_consecutive_failed_dag_runs`, `render_template_as_native_obj`, `owner_links`, task `inlets`, `executor`, `executor_config`, `retry_exponential_backoff`, a param's `description` and length/pattern keywords, and XCom references nested in a list or dict argument were dropped; code-mode apply then lost them too. Constructor arguments of an operator whose class lists a mixin before the operator base were dropped as well. Arguments the spec has no field for (`email`, `do_xcom_push`, task callbacks, ...) and operator classes defined in the DAG file are now import warnings.
- Generated code for a target Airflow before 3.2 turned a `retry_exponential_backoff` multiplier other than 2 silently into `True` (base-2 backoff, the only one those versions have); validation now reports it as an `airflow_version` warning on the task's field.
- Generated code: a Python task whose function name equals a module the file imports (`os`, `hashlib`, `tempfile`, `pendulum`, `datetime`, or a name an inline function imports) shadowed the module, which broke parsing or the task at run time; the function is renamed in the code and validation warns (`function_name_reserved`). Durations that are infinite, not a number or larger than `datetime.timedelta` holds are a `duration` validation error instead of a DAG that fails with `OverflowError`. Start and end dates in a DAG timezone lost their microseconds. `retry_exponential_backoff` is generated as a bool for target Airflow versions before 3.2, which reject a number.
- Airflow YAML export wrote `default_args`, which Airflow's YAML importer rejects; they are now a `default_args` task template that every task extends. Task `retry_delay` / `execution_timeout`, `dagrun_timeout` and `timedelta` arguments of `HITLOperator` and the external task sensor are written as duration strings (`5m`) instead of being refused; `retry_exponential_backoff`, `executor_config`, `render_template_as_native_obj` and `rerun_with_latest_version` are exported, OpenLineage settings are reported, and importing restores the DAG timezone, the duration fields and `run_immediately`.
- dag-factory export: dependencies on task groups are exported as dependencies of the group's tasks, `end_date` and the remaining task settings are written, disabled tasks are left out, and every setting the file cannot carry (edge labels, params, callbacks, deadlines, unsupported schedules, setup/teardown, `expand_kwargs`, ...) is a warning instead of being dropped silently.

## [0.2.2] - 2026-10-22

### Added
- Contributors: pre-commit hooks (`uv run pre-commit install`) for ruff, the generated
  configuration reference, the hard-coded audit, version agreement, prettier, eslint, the MDX
  check and actionlint, plus mypy before push; `pre-commit` is in the dev dependencies.

### Security
- CI and release workflows pin third-party GitHub Actions (setup-uv, Docker build actions, PyPI
  publish) to full commit SHAs, so a moved tag cannot change what builds and publishes AutoAir
  releases; Dependabot keeps the pins current. `astral-sh/setup-uv` moves to v10 (Node 24).
- AI prompt-injection shield: removing `<untrusted-data>` tags from untrusted text now takes
  linear time; a document full of unclosed tags could stall a request for seconds.
- Deploy target dialog: the suggested git provider comes from the remote's parsed hostname,
  matched on whole domain labels, so a look-alike host (`bitbucket.org.example.com`) or a
  provider name in the URL path no longer selects that provider's API.
- Docker Compose demo (`tls` profile): the generated server key is readable by its owner
  only (mode 0600 instead of 0640).

## [0.2.1] - 2026-10-22

### Fixed
- Deploy target dialog: object storage targets can be chosen like git and local ones. Airflow's
  `S3DagBundle` and `GCSDagBundle` entries are discovered, and an object storage prefix can be
  entered by hand (fsspec URL plus optional non-secret storage options; credentials still come
  from the server environment or `AUTOAIR_STORAGE__STORAGE_OPTIONS`). Before, switching to
  object storage was possible only through the API. `storage.allowed_targets` still applies.
- Container image: `pip check` passes again. The Airflow base image ships `litellm` (an optional
  dependency of Vertex AI evaluation), which pins `openai<3`, while `autoair[ai-openai]` needs
  `openai>=3`. The image now removes `litellm`, which AutoAir does not use, and the build and
  the CI smoke test run `pip check`. Vertex AI evaluation with third-party judge models needs
  `litellm` and is not available in this image.
- `ai.llm_conn_id` finds connections stored in the Airflow metadata database again. Outside a
  task the Task SDK's `BaseHook.get_connection` does not read the metadata DB, so on Airflow
  3.1+ the plugin, `autoair serve` and `autoair ai-serve` reported "conn_id isn't defined" for
  connections created in the Airflow UI or CLI. All connection lookups (LLM, git, knowledge
  base, validator key) now share one helper that checks env vars, the configured secrets
  backend and the metadata DB first, then the Task SDK. The
  `AIRFLOW__SECRETS__BACKEND=airflow.secrets.metastore.MetastoreBackend` workaround is no
  longer needed.
- Python DAG import (import dialog, code mode, bulk import, MCP `import_python_dag`) keeps the
  `python_callable` of classic `PythonOperator`, `BranchPythonOperator`,
  `ShortCircuitOperator` and `PythonSensor` tasks (and other callable operator arguments).
  Before, the callable was dropped and the deploy failed with "python_callable missing". A
  function defined in the DAG file becomes an inline function (its source without decorators,
  with the file's imports it uses), a function imported from another module an import
  reference; a lambda, a `functools.partial` or a function that uses other names of the file
  is reported in the import warnings. Source-preserving deploys of such files compare these
  functions as code, so a canvas change is patched into the file instead of being refused.

## [0.2.0] - 2026-10-22

### Added
- Kustomize manifests for Kubernetes (`deploy/kustomize`): a hardened base for `autoair serve`
  (non-root, read-only root file system, no service account token, probes on `/api/health` and
  `/api/ready`, PodDisruptionBudget, topology spread), Components for PostgreSQL state with an
  `autoair db upgrade` init container, the git deploy target, the validator service (no egress),
  the AI service, a separate MCP server, a Gateway API HTTPRoute (the production default) or an
  Ingress, HorizontalPodAutoscaler,
  NetworkPolicies and Prometheus ServiceMonitors, and `dev`, `production` and `airflow-plugin`
  overlays. Secrets are referenced, never included; credentials stay Airflow connections. CI
  builds every overlay and validates it with kubeconform. New page: Deployment → Kubernetes
  with Kustomize.
- **AutoAir docs MCP**: AutoAir's own documentation (the website pages and the configuration
  reference) is packaged in the wheel at build time (`hatch_build.py`, `autoair.docs.build`) and
  served to coding agents by the MCP tools `search_autoair_docs`, `read_autoair_doc` and
  `list_autoair_docs`: offline, matched to the installed version, without Airflow, the AI extra
  or a knowledge-base store. `autoair docs-mcp` (stdio, or `--http`) serves only these tools,
  e.g. `uvx --from 'autoair[mcp]' autoair docs-mcp`; they are also on every AutoAir MCP endpoint
  (no AutoAir permission, feature flag `features.autoair_docs`). Setup for Claude Code, Cursor,
  OpenCode and Codex CLI: website page *AI → Docs MCP*.
- Knowledge base: built-in source kind and component `autoair` (AutoAir's packaged
  documentation, no network), so `kb_search` and the assistants cite AutoAir pages.
- Isolated import checks: `autoair validator-serve` runs the DagBag import check in a separate
  validator service, so the DAG code it imports (raw-code DAGs, Python task bodies) no longer
  executes in the Airflow API server or `autoair serve` (Python DAG import and test runs still
  do). Set `validator.url`
  (`AUTOAIR_VALIDATOR__URL`); requests are HMAC-signed with the password of the Airflow
  connection `validator.secret_conn_id` (no new secret setting). Workers get a fresh interpreter
  per check, memory/CPU/file-size/open-file limits, no new privileges, a network namespace where
  the kernel allows it and a Python audit hook (no subprocesses, sockets, DNS, writes outside
  their scratch folder or reads of other processes' `/proc`). Retries, a health probe and
  `validator.fail_mode` (`open` by default: an `import_service` warning and no import check
  while the validator is down; `closed` blocks deploys instead; `local`). `/health`, `/ready`, `/metrics`
  (`autoair_validator_*`) on the validator, `autoair_import_checks_total` on the main server.
  New Helm chart `deploy/helm/autoair-validator` (Deployment, Service, PodDisruptionBudget,
  NetworkPolicy denying all egress, read-only non-root pods) and Compose overlay
  `deploy/compose/docker-compose.validator.yml`. New page: Deployment → Validator service.
- Astronomer Blueprint steps: templates of `airflow-blueprint` (installed packages with the
  `airflow_blueprint.blueprints` entry point, `blueprints.template_dirs`, `blueprints.modules`)
  appear in the palette's *Blueprints (Astronomer)* section and become `kind: blueprint` nodes
  with a config form generated from the template's Pydantic model, version selection, a task
  preview (inspector and collapsible node) and validation against the config schema. Python and
  loader output call `Template().render(Config(...))`; the YAML output mode reports them as
  unsupported, the dag-factory export skips them. The Python importer folds `render()` calls
  back into blueprint steps. New extra `autoair[blueprint]`, feature switch
  `features.blueprints`, settings section `blueprints`, routes `/api/blueprints/*` and the MCP
  tool `list_blueprints`. Guide: User guide → Blueprint steps.
- Airflow 3.4: `plugin.dag_tab_tags` (`AUTOAIR_PLUGIN__DAG_TAB_TAGS`) shows the **AutoAir**
  DAG tab only on DAGs with one of the listed tags, through the plugin's `applies_to`
  scoping. Empty (default) keeps the tab on every DAG page; ignored before Airflow 3.4.
- Validation warns about task groups that depend on each other in a cycle
  (`task_group_cycle`), which Airflow 3.4 deprecates and Airflow 3.5 is planned to reject.
- Editing existing Python DAGs keeps the hand-written file: each DAG has a source policy
  (`auto`, `generate`, `preserve`, `strict-preserve`; DAG settings and the deploy dialog).
  Imported and code-mode DAGs default to preserve: a deploy applies only the change to the
  existing file with a LibCST patch (keyword arguments, TaskFlow bodies, added and removed
  tasks and dependencies, renames), verifies it with a round-trip import and the DagBag
  check, and never regenerates the file on its own. The deploy dialog previews the DAG file
  as a unified diff (`POST /api/specs/{dag_id}/source-preview`, MCP `preview_dag_source`);
  refused changes explain why and offer raw code or a deliberate switch to `generate`.
  A stopped deploy says why by the file's state (the hand-written file AutoAir keeps, a file
  changed outside AutoAir, or one AutoAir did not create); "Keep as raw code DAG" and
  "Overwrite the file" follow the user's `code:raw` and `deploy:overwrite_ejected`; a source
  policy picked in the deploy dialog changes the DAG only when it deploys (Cancel keeps it).
  Code mode and the Code tab show the hand-written file. New settings `preserve.*`, feature flags
  `source_preserve` and `strict_source_preserve`. `libcst` is a new core dependency.
- Generated DAG files carry a fingerprint line (`# autoair:fingerprint sha256=…`), so a file
  edited outside AutoAir is recognised on folder, object-storage and git targets even without
  AutoAir's state. Such a deploy stops and offers to apply the change to the edited file
  (merged with the outside edits); `preserve.ejected_files` (`block`, `warn`, `overwrite`)
  sets the behaviour. New permission `deploy:overwrite_ejected` (Admin; never from a
  wildcard) for a confirmed overwrite.
- Tool-call approval for AI agents (pydantic-ai deferred tools): when the developer or admin
  assistant or a troubleshooting analysis calls a tool that changes something (`remember`,
  external MCP tools not marked `readOnlyHint`), the run pauses with an approval card showing
  the tool and its masked arguments; the user who started the run approves or denies each
  call (optionally with a reason) and the run resumes in the same answer or analysis.
  Approvals are stored with the state (multi-replica safe, decided at most once, expiring
  after `ai.approvals.ttl_seconds`) and audited. New settings `ai.approvals.*`, new endpoints
  `GET /api/ai/approvals`, `GET /api/ai/approvals/{id}` and
  `POST /api/ai/approvals/{id}/stream`. New page: AI → Tool-call approval.
- Large DAGs: above 150 nodes the canvas can zoom out to an overview of the whole graph
  (**Fit view**), and below 40 % zoom task cards turn into light, colour-tinted boxes with
  their name (problems, run states, diff and selection stay visible). Benchmarks for large
  DAGs: `scripts/gen_large_dag.py` (synthetic specs), `scripts/bench_large_dag.py` (server
  timings) and `frontend/e2e/large-graph.spec.ts` (load, layout, frame rate, memory). New
  page: User guide → Large DAGs (measured limits and tuning).
- `preserve.allow_force_overwrite` (`AUTOAIR_PRESERVE__ALLOW_FORCE_OVERWRITE`, default
  `false`): the "Overwrite (force)" choice is offered and accepted only when it is `true`, for
  the hand-written file AutoAir keeps for a DAG, a DAG file changed outside AutoAir and `force`
  deploys of the loader and YAML output modes and raw code. The caller still needs
  `deploy:overwrite_ejected` and an explicit confirmation; `/api/meta` reports
  `allow_force_overwrite`, and the source-plan and conflict dialogs hide the overwrite button
  otherwise. Source plans refuse with the new code `overwrite_disabled`.
- `server.harden_process` (`AUTOAIR_SERVER__HARDEN_PROCESS`, default `true`) controls whether
  `autoair serve`, `autoair local` (and their uvicorn workers), `autoair mcp-serve --http` and
  `autoair ai-serve` make their process non-dumpable. Turn it off only to debug with py-spy,
  gdb or core dumps; the server then logs a warning at start-up. Never applied in the Airflow
  plugin.

### Changed
- `spec_version` is now 3. Version 3 adds only optional parts (`source` and the `blueprint`
  node kind); version 1 and 2 documents keep loading and are written as version 3 the next
  time they are saved or deployed. **Upgrade note:** AutoAir 0.1.0 refuses version 3 specs
  with "spec_version 3 is newer than this AutoAir supports (2); upgrade AutoAir", also in the
  loader stubs inside Airflow. Upgrade every AutoAir process that reads the same specs
  together (plugin, `autoair serve` replicas, MCP and AI services, the `autoair` package in
  Airflow's dag-processor and workers); rolling back to 0.1.0 needs the specs from before the
  upgrade.
- Overwriting a DAG file AutoAir would otherwise not replace now needs
  `preserve.allow_force_overwrite=true` (default `false`). `preserve.ejected_files=overwrite`
  takes effect only with it (otherwise it behaves like `block`, also for administrators), and
  regenerating the kept hand-written file (switch to generate) needs it as well as
  `deploy:overwrite_ejected`. Deleting a kept hand-written DAG still needs only the
  permission. Files without AutoAir's marker are never overwritten by `force`. A deploy that
  would replace the kept hand-written file without a source plan (another output mode, a
  conversion to raw code) returns HTTP 200 with `deployed: false`, `status: "ejected"` and
  `conflict.ejected_file`, `conflict.hand_written` and `conflict.force_allowed` instead of
  HTTP 409; ejected results without a source plan also carry `conflict.force_allowed`.
  **Upgrade note:** deployments that relied on `preserve.ejected_files=overwrite` or on
  regenerating kept hand-written files must set `AUTOAIR_PRESERVE__ALLOW_FORCE_OVERWRITE=true`;
  API clients that handled the 409 must read `status: "ejected"` instead.
- MCP `save_raw_dag_code`: a raw code DAG that replaces its own code no longer needs
  `deploy:overwrite_ejected`, like the REST deploy (`code:raw` is still required over HTTP).
  The file is written only while it is unchanged since AutoAir wrote it; MCP has no `force`,
  so a file changed outside AutoAir is refused. Replacing a visual DAG's kept file is still
  refused.
- **Upgrade note:** the validator service (`autoair validator-serve`) and the main server
  must run the same AutoAir version; there is no mixed-version compatibility window. Upgrade
  and roll back both together.
- Knowledge base defaults: `kb.sources` is now `official` plus `autoair`, and `kb.components`
  includes `autoair`. With `kb.refresh_on_start` the server now builds the (small) index of
  AutoAir's own documentation at start-up even when air-gapped. A `kb.sources` list you set
  replaces the default; add `{"id": "autoair", "kind": "autoair"}` to keep AutoAir's pages.
- The warm DagBag pool exchanges JSON with its workers instead of pickles, so DAG code that
  writes to the worker's pipe can no longer execute code in the AutoAir process when the
  parent reads the reply. Workers run in their own process group, and stopping one also stops
  the processes it started.
- CI tests the Airflow 3.4.0b2 pre-release (was 3.4.0b1). No final 3.4 exists yet. Airflow
  3.4 needs Python 3.11 or newer; AutoAir keeps supporting Python 3.10 with Airflow 3.0-3.3.
- A local deploy whose reparse request Airflow refuses (`403`, for example a file without a
  registered DAG on Airflow 3.4 without the `REPARSE_ALL` permission, or `404`) says so in the
  deploy message instead of "could not contact Airflow"; the deploy itself succeeds. A
  `409` (a reparse of the file is still queued) is reported as "reparse already requested".
- The deploy option `force` no longer overwrites a Python DAG file that was changed outside
  AutoAir: answer the stopped deploy with `source_resolution` instead. For loader stubs it
  needs `deploy:overwrite_ejected`.
- **Deployer** cannot deploy a DAG that keeps its hand-written Python file: the round
  trip needs `code:execute` and writing the imported file needs `code:raw`. The refusal
  names the missing permission. New built-in role **CodeDeployer** (Deployer +
  `code:execute` + `code:raw`) for those deploys; or deploy with the `generate` policy.
  Regenerating a hand-written file AutoAir keeps needs `deploy:overwrite_ejected` and
  `preserve.allow_force_overwrite=true`; deleting it needs the permission.
- Imported DAGs (Python file, Airflow, repository import, MCP) and code applied in the code
  mode keep their Python file (`source.origin: python`); deploying it as written needs
  `code:execute` for the round trip and `code:raw` when the file comes from the spec. The
  repository import dialog can generate the files instead. Promoting such a DAG between
  environments is refused (`409`).
- Calls of `remember` and of external MCP tools without `readOnlyHint: true` now wait for
  the user's approval by default (previously they ran directly). Exempt tools with
  `ai.approvals.default.exempt` or `ai.approvals.agents.<agent>.exempt`. Troubleshooting
  analyses can have the new status `awaiting_approval`.
- Validation lints all Python task bodies with one ruff run and shell scripts with one
  ShellCheck run per 50 scripts, and remembers results of unchanged bodies (a failed run is
  retried, not remembered; `.shellcheckrc` files are no longer read): validating a 1,000-task DAG takes
  about 0.3 s the first time and 0.1 s afterwards (was 2-5 s, one process per task).
  `editor.python_lint_timeout` and `editor.shellcheck_timeout` now apply per batched run.
- Editor performance on large DAGs: dragging no longer serialises the whole document for undo
  history, live checks and collaboration on every frame; node badges, run-state overlays and
  the inspector no longer re-render or rescan every node per change; live checks wait longer
  after the last change on DAGs with more than 200 nodes (up to 2 s); an auto layout that
  finishes after the graph changed is discarded instead of applied.
- Testing: visual regression baselines inside the Airflow UI (`npm run test:visual:plugin`):
  the eight standalone screens plus the connection form, clear and backfill dialogs and asset
  actions, light and dark, against a private Airflow with the plugin. The browser runs in the
  Playwright Docker image so the baselines match on every Linux machine and in CI (the
  non-blocking *Frontend · visual regression* job runs both suites).
- `autoair[all]` no longer includes `mcp`: FastMCP 4 needs Starlette 1.x, which Airflow 3.1's
  FastAPI pin excludes, so `all` now installs next to every supported Airflow. Use
  `autoair[all,mcp]` on Airflow 3.0, 3.2 and newer or standalone. The `sqlite`, `postgres` and
  `kb-pgvector` extras accept SQLAlchemy 1.4.49 or newer. New page: Installation →
  Compatibility (extras per Airflow version).

### Fixed
- Airflow 3.4: the latest log of a task waiting for a retry (`up_for_retry`) is the failed
  try again, not the upcoming try that has no log yet (run panel, AI troubleshooting and fix,
  MCP `get_task_log`). `GET /api/airflow/capabilities` reports `retry_try_is_next`.
- The SQLite and PostgreSQL state backends and the pgvector knowledge-base store work on
  SQLAlchemy 1.4, the version Airflow 3.0 installs (previously an `ImportError` on
  `sqlalchemy.Connection`). On SQLAlchemy 1.4 PostgreSQL goes through psycopg2.
- Without `autoair[mcp]` the MCP feature is reported unavailable with the reason
  (`/api/meta` `mcp_unavailable_reason`, admin *Features* tab) instead of only being unmounted.
- Python 3.10: git history timestamps ending in `Z` and `$type: datetime` parameters sent by
  the browser (`toISOString`) no longer fail to parse.
- Python DAG import on Airflow 3.2: `CronTriggerTimetable("@daily", ...)` is recognised as a
  preset again (SDK timetables keep the preset unresolved).
- Security: a hand-written DAG file adopted over MCP (`save_dag_spec` with source policy
  preserve) is recorded as kept, like a REST deploy, so regenerating or deleting it needs
  `deploy:overwrite_ejected`. The deploy targets (local folder, object storage, git) now refuse
  to replace or remove a kept file on every write path that skips the source plan: switching
  the DAG to the loader or YAML output mode, converting it to a raw code DAG, MCP saves and
  promotions. A deploy with `force` by a holder of `deploy:overwrite_ejected` still may, with
  `preserve.allow_force_overwrite=true` and only for a file with AutoAir's marker; a raw code
  DAG keeps editing its own code.
- Security: a DAG that writes part of a reply to the import-check worker's pipe and stops no
  longer hangs a DagBag pool thread (and, in the validator service, a capacity slot until a
  restart): the check's deadline covers reading the whole reply, after which the worker is
  replaced. Replies carry a per-check nonce, so code left running from an earlier check
  cannot answer for a later one.
- Security: the validator service signs its `/v1/check` answers with the HMAC key, bound to
  the request's nonce (`x-autoair-validator-response-signature`); the main server rejects
  unsigned or mismatched answers and treats the validator as unavailable
  (`validator.fail_mode` applies). Update the validator and the main server together.
- Security: `autoair serve`, `autoair local`, `autoair mcp-serve --http` and `autoair ai-serve`
  make their process non-dumpable at start-up (Linux), so DAG code in their child processes
  cannot read the server's environment through `/proc`. The Airflow plugin leaves Airflow's
  process unchanged; use `validator.url` there.
- Security: denial reasons of AI tool approvals are scrubbed of secrets and passed through the
  guardrails before they reach the model, the stored steps and the audit record.
- Security: for a hand-written file AutoAir did not write (content hidden), deploy results no
  longer return the merged `stored_spec`, and import and round-trip errors no longer quote the
  file.
- Security: the MCP `save_dag_spec` validates a preserved hand-written file with the same
  inputs as the REST deploy (operator templates, target Airflow version, user and action for
  OPA).
- Blueprint task previews report an unavailable validator service as a preview error instead
  of HTTP 500.
- The deploy audit record and commit context say when the import check was skipped
  (`validation.import_check_skipped`: `fail_open` when the validator was unavailable,
  `permission` without `code:execute`).
- The validator's Kustomize NetworkPolicy admits Airflow api-server pods only from the
  namespace `airflow` (a placeholder to adjust) instead of from every namespace.

## [0.1.0] - 2026-10-21

First public release.

### Added

#### Builder and canvas
- Visual DAG editor (React Flow) inside Airflow (**Browse → DAG Builder** and an **AutoAir**
  tab on every DAG page on Airflow ≥ 3.1; a full page at `/autoair/ui/` on 3.0) and as a
  standalone server (`autoair serve`).
- Integration logos on nodes and custom icon upload with an in-browser resizer; notes; task
  groups (group, ungroup, collapse); insert-on-edge; command palette; context menus;
  copy/paste with id and XCom remapping; alignment guides; undo/redo; ELK auto-layout in a web
  worker; run-state and version-diff overlays; keyboard shortcuts; collapsible panels.
- Full-screen code mode and an optional editable Code tab (`features.code_edit_inline`) that
  re-import edited Python into the visual spec; a read-only preview of the file generated for
  a chosen environment (`POST /api/codegen?environment=<env>`).
- Editor modes: `features.visual_editor` and `features.code_editor` give visual-only and
  code-only deployments; switching them at runtime keeps the open DAG and an unapplied code
  draft.
- Raw code DAGs (`DagSpec.raw_code`, permission `code:raw`): a hand-written DAG file stored,
  versioned, reviewed, proposed, deployed, promoted and audited like a visual DAG; validated
  by syntax, ruff, secret detection, policy (privileged and forbidden operators found by a
  static scan, dag_id rules, OPA with `context.document_type=raw`) and the DagBag import
  check, with problems located by line. **File → New raw code DAG**, **Convert to raw
  code…** and **Open in visual builder**; a **Raw code** badge and a type filter in the Open
  dialog.
- English and Turkish UI, light and dark themes, UI branding (title, logo, accent, banner).

#### Authoring
- Typed DAG spec (Pydantic v2) with migrations as the single source of truth; the TypeScript
  client is generated from the OpenAPI document.
- Every installed operator, sensor and transfer, from official and third-party providers,
  Airflow libraries, plugins, DAG folders and packages, through a static AST catalog that does
  not import the modules. The catalog can be precomputed with `autoair catalog build`.
- Operator packages describe their operators for the palette without AutoAir configuration:
  an `autoair.yaml` / `autoair.json` file in the package or an `autoair.operator_metadata`
  entry point (palette group and order, icons including sanitised SVGs, parameter widgets,
  examples, docs links). Settings `catalog.connection_param_patterns`, `catalog.groups`,
  `catalog.default_group_order`, `catalog.param_overrides` and `icons.rules_extra` take
  precedence over package metadata, which takes precedence over inference.
- Catalog diagnostics: `GET /api/catalog/diagnostics`, the admin panel's **Catalog** tab and
  `autoair catalog dump --diagnostics` (sources, operators per source, filtered operators and
  why, failed modules, applied package metadata, build time).
- Python references to callables and classes; functions written in place; embedded script
  files written to every worker at parse time.
- TaskFlow: `@task`, `@task.branch`, `short_circuit`, `virtualenv`, `external_python`,
  `sensor`, `bash`, `docker`, `kubernetes` and `@task.stub` (language SDKs).
- Dynamic task mapping (`expand`, `expand_kwargs`, `map_index_template`), task loops
  (Airflow 3.4), setup/teardown, trigger rules, callbacks and notifiers, retry policies
  (3.3), deadlines (3.1), human-in-the-loop operators with a form designer, OpenLineage
  toggles, disabled tasks.
- DAG params with a live trigger-form preview; environment overlays (dev/staging/prod).
- Scheduling: DAG time zone (DST aware), cron, presets, intervals, multi-cron, event dates,
  asset expressions, event-driven assets (AssetWatcher with SQS, Kafka, Pub/Sub, Redis,
  Service Bus, Kinesis), assets-or-time, assets-and-time (3.4) and partitioned schedules
  (3.2), with a live "next runs" preview from Airflow's timetable engine.
- Jinja-aware editors for templated fields with completion and inline preview;
  drag-to-map upstream XComs; connection pickers filtered by type with inline "New
  connection" (create and test in Airflow); IDE-style operator docs; Markdown fields with
  snippets and preview.
- Python and shell editors: jedi completion, hover and signature help that know the installed
  Airflow and providers, live ruff lint, ShellCheck diagnostics. Optional language servers
  (basedpyright, pylsp, bash-language-server) per session.
- Output as readable Python, a JSON spec with a runtime loader, an Airflow 3.4 native YAML
  DAG, or dag-factory YAML. Generated imports follow the target Airflow version.
- Import from hand-written Python DAGs (mapped tasks, `partial()` arguments and cron presets
  kept), from Airflow, and from YAML.

#### Validation and policy
- Live validation: graph, catalog, Jinja, policy and Airflow version gates, plus a warm
  DagBag import-check pool.
- Organisation policy: flat switches (required owner or description, minimum retries,
  required tags, forbidden operators, max tasks, dag_id prefix, operator allow/deny lists,
  privileged operators, Python code), configurable id and secret-detection patterns,
  declarative `policy.rules`, and an optional Open Policy Agent decision.
- Hard-coded secret detection in parameters and inline code, with a hint to use Connections
  or Variables.
- Rules-only best-practice review and leak/secret scan without AI (`POST /api/review/rules`,
  `POST /api/lint/secrets`, `builder:use`), offered in the **Run** menu when AI is off.

#### Lifecycle and deploy
- Deploy targets: a local folder (LocalDagBundle), object storage through fsspec
  (S3/GCS DagBundles) and Git: the git CLI with any remote, or the GitHub, GitLab, Bitbucket,
  Azure DevOps and Gitea APIs.
- Git pull-request mode (branch and PR per DAG) or direct push, environment promotion
  (validated for the target environment like a deploy), PR labels, reviewers and drafts, and
  GitHub App authentication.
- Deploy target dialog: discovers where Airflow reads DAGs (DAG folder,
  GitDagBundles, a git-sync checkout) and switches without a restart. Credentials always come
  from Airflow connections: shared, per user, or forced by an administrator.
- Git tokens typed per deploy: the deploy, promote, delete, conflict-retry and bulk-import
  dialogs accept the user's own token, sent only in the `X-AutoAir-Git-Token` header of that
  request and never stored, logged or audited; HTTPS or loopback only. Settings
  `storage.git.request_token` (`off` / `optional` / `required`),
  `request_token_overrides_forced`, `request_token_allow_insecure` and `mcp_request_token`.
- Deploy governance: a mandatory context block and `X-AutoAir-*` git trailers on every
  commit and PR, plus commit, PR and branch templates and a live preview.
- ETag optimistic locking with a three-way semantic merge dialog; protection of ejected
  (hand-edited) files; version history with a visual graph diff.
- Test a task or a whole DAG without the metadata database; trigger, backfill (dry run),
  clear and pause/unpause; a run overlay with logs and XComs.
- Template gallery, reusable fragments and operator templates; signed (Ed25519) template
  marketplaces (`autoair marketplace keygen|build|verify`).
- Operator template editor, available in every editor mode from the palette, the template
  gallery, the command palette and the admin panel: catalog operator picker, parameter form,
  a Default / Locked / Required switch per field, live validation, a code preview, and a
  version history with a diff (`GET /api/templates/{id}/versions[/{version}]`,
  `templates_history_limit`). Saves and deletes take `If-Match` / `If-None-Match: *` and
  answer 409 on a conflict; another team's template needs `admin:config`.

#### Collaboration, identity and access
- Real-time co-editing (Yjs): presence, remote cursors and selections, per-user undo, and
  WebSocket access through short-lived tickets that require DAG edit permission.
- IAM on top of Airflow's auth manager: built-in and custom roles; `airflow_permissions`,
  `role_mapping` and `custom_view` strategies (FAB, Keycloak, SimpleAuthManager); four-eyes
  approvals; direct-push roles; `autoair iam roles|sync-fab`; an audit trail with CSV export.
- Standalone sign-in through the Airflow UI session (SSO), with Airflow username and
  password, or with an API token; every token is verified by Airflow, and sign-out is
  propagated.
- Runtime feature flags: the admin panel's **Features** tab (`GET/PUT /api/admin/features`)
  switches `features.*` without a restart. Overrides are shared by replicas through the state
  backend (`features_override_ttl`) and audited with a reason; `features_locked` and
  `AUTOAIR_FEATURES__<NAME>` lock a flag.
- Admin panel: overview, configuration, audit browser, replicas, templates, users, AI usage
  and rules, MCP servers, proposals, catalog, knowledge base, database and caches; audit
  export and scan sizes are configurable (`admin.export_limit`, `admin.export_default`,
  `admin.scan_limit`, `admin.usage_scan_limit`).

#### AI and MCP
- AI on [pydantic-ai](https://ai.pydantic.dev) as an optional extra (`autoair[ai]` plus one
  provider extra: `ai-anthropic`, `ai-openai`, `ai-google`, `ai-ollama`, `ai-bedrock`,
  `ai-groq`, `ai-mistral`, `ai-cohere`, or `ai-all`). Providers `anthropic`, `openai`,
  `azure`, `gemini`, `google-cloud`, `ollama`, `bedrock`, `groq`, `mistral`, `cohere` and
  OpenAI-compatible servers. Without the extra, AI is reported off with the reason
  (`GET /api/meta` → `ai_unavailable_reason`, `GET /api/ai/status` → `reason`).
- AI assistance: generate a DAG from a prompt, edit with AI (reviewed as a diff), explain and
  fix problems, review, write docs, troubleshoot runs. Central or per-user models (per-user
  keys are encrypted); `ai.llm_conn_id` and per-user connections read Airflow's common.ai
  `pydanticai` connection format.
- Agents on pydantic-ai with typed tools, structured output, streaming and usage limits;
  assistant threads replay earlier tool calls and results; retrieval with optional
  reranking, guardrails, research (air-gapped or online) and an AI audit log.
- AI troubleshooting can run the Airflow actions it proposes (clear or retry a task instance,
  clear the run, mark a task or the run success or failed): `ai.troubleshoot.airflow_actions`
  = `read_only` (default), `hitl` (each action runs after the user approves it) or `apply`.
  Actions are vetted (`allowed_actions`, `max_actions`, `max_cleared_task_instances` checked
  by a dry run, upstream/downstream limits), run with the user's Airflow token and
  `run:manage`, are claimed once across replicas, and are audited.
- Documentation knowledge base (`kb` settings, `features.kb`): the Apache Airflow core, Task
  SDK, provider, Helm chart and airflowctl documentation and Astronomer's guides at the
  installed versions, read from the official sites, HTTP mirrors, git repositories, folders,
  object storage or signed portable bundles (`autoair kb build|import`) for air-gapped
  installs. Incremental, SSRF-checked, size- and time-limited ingestion by one replica at a
  time; hybrid BM25 and vector search (BM25 only without `autoair[ai]`) in a local SQLite,
  pgvector (`kb-pgvector`) or Elasticsearch (`kb-elasticsearch`) store. Used by the agents,
  writers, review and fix; routes `/api/kb/*`, MCP tools `kb_search`, `kb_sources`,
  `kb_status`, and `autoair kb sources|status|refresh|search|build|import|keygen|mcp`.
- A separate AI service (`autoair ai-serve`) that the main server proxies to, with
  HMAC-signed caller identity.
- MCP server with authoring tools for Claude Code, Claude Desktop and other agents (stdio or
  streamable HTTP inside Airflow), including raw code DAGs and operator templates; the review
  and leak-scan tools work without AI (`builder:use`). An MCP client attaches external servers
  to the agents and chat assistants (`ai.mcp_client.servers[].agents`).

#### Operations
- Deployment modes: Airflow plugin, standalone server (workers, TLS, mutual TLS, proxy
  headers, security headers, HSTS), and `autoair local` for a laptop without an Airflow
  server.
- State in files (fsspec), SQLite or PostgreSQL (`autoair db upgrade|current`). With
  PostgreSQL, replicas are stateless; collaboration fans out through the database
  (`LISTEN/NOTIFY`) or Redis.
- Configuration from environment variables, YAML/JSON/INI/Python files and `airflow.cfg`;
  `autoair config show`; a generated configuration reference; admin-only config exposure.
- Outbound TLS settings (CA bundle, client certificates, proxies, per-client overrides) for
  Airflow, git, marketplace, AI, MCP and knowledge-base calls, on the `httpx2` client.
- `/api/health`, `/api/ready`, Prometheus `/api/metrics`, JSON logs and optional
  OpenTelemetry tracing.
- A container image on the official Airflow image with a precomputed catalog, a non-root
  user and a health check (`ghcr.io/ctolon/autoair`); Helm values for the official chart;
  a Docker Compose demo with profiles for a load balancer, Redis, the AI service, Ollama,
  Gitea, TLS and tracing.
- Supports Apache Airflow ≥ 3.0, < 4 (CI: 3.0.6, 3.1.8, 3.2.2, 3.3.2, plus the 3.4
  pre-release) on Python 3.10–3.14.

[Unreleased]: https://github.com/ctolon/autoair/compare/v0.2.3...HEAD
[0.2.3]: https://github.com/ctolon/autoair/compare/v0.2.2...v0.2.3
[0.2.2]: https://github.com/ctolon/autoair/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/ctolon/autoair/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/ctolon/autoair/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/ctolon/autoair/releases/tag/v0.1.0
