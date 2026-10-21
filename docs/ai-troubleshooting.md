# AI troubleshooting of DAG runs

Reference for contributors and operators. Task-oriented guide on the documentation site: [Troubleshooting with AI](https://ctolon.github.io/autoair/docs/ai/troubleshooting).

**Analyze with AI** is a button in the bottom panel's **Runs** tab (for the selected run) and
**Task** tab (for the selected task instance and try). It starts a scoped, read-only,
agentic analysis of the run and ends with a structured report: root-cause hypotheses with
evidence, remediation steps, and proposed changes. **Nothing is changed unless you approve
it**, and an approved spec change still goes live only through the normal deploy pipeline.

Requirements: `ai:use` + `airflow:read` (Airflow's own RBAC applies, because every Airflow call
uses the **caller's** token), `ai.troubleshoot.enabled`. Approving a change needs
`spec:write`; deploying it needs `deploy` (plus governance, approvals and PR mode, as
configured).

## What the agent sees

**Evidence pack** (collected deterministically before the model runs):

* run metadata (state, type, dates, `bundle_version`, *keys* of `conf`);
* every task instance (state, try, duration, operator);
* for the failed task instances (or the one you picked; at most `max_tasks`):
  * the upstream states;
  * the DagSpec node id;
  * the XCom keys;
  * the log tail of **each try** (`max_tries`, `max_log_chars`), with line numbers;
* the DAG version matching the run's bundle version;
* the DagSpec (the open editor's, else the saved one) and the generated code;
* import errors;
* the Airflow version and the installed providers.

Logs pass two scrubbers before the model sees them: secret patterns and unquoted
`password=…` / `token: …` / `Authorization: Bearer …` assignments. They are then wrapped as
untrusted data, so prompt-injection text inside logs is not followed.

**Tools**, limited by the scope:

| Scope (`ai.troubleshoot.resources.*` and the user's choice) | Tools |
|---|---|
| always | `get_task_log` (any task/try: tail, line window or grep, with line numbers), `get_task_instances`, `get_current_spec`, `get_generated_code`, catalog/validation tools |
| `other_runs` | `list_other_runs`, `get_task_history` (state/tries/duration of a task across runs) |
| `connections` | `get_connection_metadata`: conn_type, host, schema, port and the **names** of the extra fields; never logins, passwords or extra values |
| `variables` | `list_variable_keys` (never values) |
| `pools` | `get_pools` (slots, running/queued/scheduled/deferred/open) |
| `xcom_keys` / `xcom_values` | `get_xcom` (values only when allowed, truncated and scrubbed; off by default) |
| `import_errors` | `get_import_errors` |
| `airflow_config` | `get_airflow_config` (masked by Airflow, scrubbed again; off by default) |
| `filesystem` | `fs_list`, `fs_read` in the sandbox below |
| `research` / `online` | `search_docs`, `read_source`, package info, and `fetch_doc` (online only when `ai.research.online`) |
| MCP servers | `mcp__<server>__<tool>` for each attached, allow-listed tool |

The UI first asks for the scope. Options the server does not allow are shown disabled; a
request can only narrow the server's settings, never widen them. **Re-run with another
scope** starts a new analysis linked through `rerun_of`.

## Filesystem sandbox

`ai.troubleshoot.filesystem.roots`, for example Airflow's `base_log_folder`, plus the DAGs
folder when `include_dags_folder` is set. The sandbox rules:

* paths are resolved, so symlinks and `..` cannot leave a root, and listings skip links that
  point outside;
* the final open uses `O_NOFOLLOW | O_NONBLOCK`, and only regular files are read;
* every path component is checked against `deny_globs` (default `.env`, `*.env`, `*.pem`, `*.key`,
  `*.p12`, `*.pfx`, `*.kdbx`, `id_rsa*`, `id_ed25519*`, `*secret*`, `*credential*`, `*.cfg`, `*.ini`,
  `*.db`, `*.sqlite*`);
  file names must match `allow_globs`;
* `max_file_bytes` limits the file size; `max_read_chars` and `max_list_entries` limit what
  is returned;
* contents are scrubbed like logs.

## External MCP servers

The agent can use tools of external MCP servers (fastmcp `Client`; streamable HTTP, SSE or
stdio):

* `ai.mcp_client.servers` in the configuration may use any transport, including a local
  stdio command;
* the **MCP servers** tab of the admin panel (`/api/admin/mcp-servers`; listing needs
  `admin:panel`, changes and *Test* need `admin:config`) adds HTTP/SSE servers only, so the
  database can never make AutoAir run a command. They are stored in the `ai_mcp_servers`
  collection, with header values Fernet-encrypted (the same key as user API keys) and never
  returned. `ai.mcp_client.database_servers=false` ignores them.

Per server: a tool allow-list (fnmatch), secret headers, a timeout, an enabled flag and the
agents that may use it (`troubleshoot`, `developer`, `admin`). Before an analysis, users pick
which allowed servers and tools to attach (`GET /api/ai/mcp/servers/{name}/tools`). Tool
results are truncated (`max_output_chars`) and treated as untrusted. A server that cannot be
reached is reported and the analysis continues without it. TLS options come from
`outbound` (client name `mcp`).

A tool the server does not mark read-only (`readOnlyHint`) is a write tool: its calls pause
the analysis until the user approves them (`ai.approvals`; always in `hitl` mode). The
analysis is stored with `status="awaiting_approval"` and resumes under the same id; see
[Tool-call approval](ai-assistants.md#tool-call-approval).

## The report

The agent's `final_answer` (`TROUBLESHOOT_SYSTEM`) contains:

* `summary`: the root cause;
* `hypotheses`, sorted by calibrated `confidence`, each with a `category` and `evidence`
  items: `source` (e.g. `log:load#2`), `line` and the verbatim `quote`;
* `remediation`: ordered steps;
* changes of three kinds:
  * **spec/code**: `findings` with JSON patches. The server applies them to the spec and
    validates the result into `proposed_spec`, and renders `code_before` / `code_after` for
    the code diff;
  * **configuration**: `config_changes` such as retries, timeouts, pools or connection
    extras, as YAML/JSON/env/text snippets;
  * **Airflow-side**: `airflow_actions`: clear or retry a task instance, clear the run,
    mark a task or the run success/failed, or a manual step (fix a connection, adjust a
    pool). Whether AutoAir may run them depends on the mode (see
    [Airflow actions](#airflow-actions)).

The UI shows the hypotheses with confidence bars and evidence, the remediation, the spec diff
and code diff (with checkboxes to choose which patches to apply), the snippets, the
instructions, the tool calls, the numbered sources and the tokens/cost.

## Airflow actions

Each proposed action is structured: `action`, `task_id` (and `map_index`) for task actions,
optional `run_id` / `dag_id`, `include_downstream` / `include_upstream`, `only_failed` and
the `instructions`. The schema is strict: an action with unknown fields or values out of
range fails validation (the agent is asked to correct its answer), so it never reaches the
analysis.

`ai.troubleshoot.airflow_actions` sets the mode:

| Mode | What happens |
|---|---|
| `read_only` (default) | Actions are shown as instructions only. |
| `hitl` | Each runnable action gets **Execute** and **Dismiss** buttons. It runs only when the user approves that one action. |
| `apply` | Once the analysis completes, the runnable actions run one after another. Their results stream as `status` events (stage `action`) and appear in the report's action log. |

A request can only pick a stricter mode (`airflow_actions` in the stream request, the radio
buttons in the dialog). The effective mode is stored with the analysis
(`actions_mode`). It is `read_only` for a caller without `run:manage` or while
`features.runs` is off.

Logs are untrusted data and can contain text written to steer the model. AutoAir therefore
vets every proposal (`autoair.ai.assistants.airflow_actions`) and never lets one do more
than the user could do in the UI. An action is `blocked`, with a reason shown in the
report, when:

* its type is not in `ai.troubleshoot.allowed_actions` (default: `clear`, `retry`,
  `clear_run`; marking states needs an explicit opt-in);
* it targets another DAG, or another run while `actions_other_runs` is off (the default);
* a task action has no task, or a clear includes upstream / downstream tasks that
  `allow_include_upstream` (default off) / `allow_include_downstream` (default on) forbid;
* more than `max_actions` (default 5) runnable actions were proposed;
* the caller lacks `run:manage`, or `features.runs` is off.

Execution re-checks all of this against the current settings and the caller. It then goes
through the Airflow gateway with the **user's own Airflow token** (never a service
account), so Airflow's RBAC applies too. It reads the current state first so that running
an action twice is harmless:

* `clear` / `retry` (`clearTaskInstances`) and `clear_run` (`dagRuns/{id}/clear`, only
  failed tasks unless the action says otherwise) skip tasks and runs that are already
  running or queued, and `retry` skips a task that has not failed. A dry run counts the
  task instances first, and a clear of more than `max_cleared_task_instances` (default
  20) is refused;
* `mark_success` / `mark_failed` (task instance) and `mark_run_success` /
  `mark_run_failed` (run) skip targets already in that state.

Each result (`succeeded`, `skipped` or `failed`, with a message, the user, the time and
whether apply mode ran it, plus a stable `code` and `params` the UI translates) is stored on
the action. Executing an action that already succeeded or was skipped returns the analysis
unchanged.

In `hitl` mode an approval first **claims** the action: `status=running` and the claimer are
written to the analysis with compare-and-set on its stored version, and only then is
Airflow called. When two replicas (or two clicks) execute the same action at once, the
second request gets `409` with `{"code": "action_running"}` and Airflow is called once. A
claim older than `ai.troubleshoot.action_claim_timeout_seconds` (default 300) may be taken
over, for a replica that died mid-call.

The developer and admin chat assistants propose spec patches and configuration snippets
only. They never propose Airflow actions, so these modes do not apply to them.

## Approve, reject, apply

| Endpoint | |
|---|---|
| `GET /api/ai/troubleshoot/options` | What the server allows, defaults, filesystem roots, MCP servers |
| `POST /api/ai/troubleshoot/stream` | Run an analysis (SSE: `status` collect/analyze/mcp, tool events, tokens, usage, then the `troubleshoot` result) |
| `GET /api/ai/troubleshoot/analyses[?dag_id=]`, `GET /api/ai/troubleshoot/analyses/{id}` | The caller's stored analyses |
| `POST /api/ai/troubleshoot/analyses/{id}/decision` | `approve` (with `finding_ids`) returns the patched, validated spec and its code (needs `spec:write`); `reject` changes nothing |
| `POST /api/ai/troubleshoot/analyses/{id}/applied` | Records that the approved change was deployed (version / PR) |
| `POST /api/ai/troubleshoot/analyses/{id}/actions/{action_id}` | `execute` (hitl and apply analyses; needs `run:manage` and `features.runs`) or `reject` one Airflow action; returns the analysis with the result |

**Approve: open in editor** loads the patched spec as unsaved work. **Approve and deploy…**
also opens the deploy dialog. When the builder reports the DAG saved under a new version or
pull request, the analysis is marked `applied` together with the outcome.

Analyses are stored in the `ai_troubleshoot` collection (`ai.troubleshoot.store_url`). Every
step is audited:

* the analysis: AI audit feature `troubleshoot`, with user, model, tokens/cost, tools used,
  sources, run ids, scope and attached MCP servers;
* each approval, rejection and applied change: features `troubleshoot:approve`, `:reject` and
  `:applied`, also in the builder audit trail as `troubleshoot_approve`, `troubleshoot_reject` and
  `troubleshoot_applied`;
* Airflow actions: `troubleshoot:actions_proposed` (every action with its status and block
  reason, and the mode), `troubleshoot:action_approved` (hitl), `troubleshoot:action_executed`
  (result, message, `automatic` in apply mode) and `troubleshoot:action_rejected`. They are
  recorded in the builder audit trail too, with `_` instead of `:`.

## Settings

`ai.troubleshoot.*`: `enabled`, `resources.*` (above), `filesystem.*` (above), `max_tasks`,
`max_tries`, `max_log_chars`, `max_steps`, `timeout_seconds`, `allow_external_mcp`,
`store_url`, and for Airflow actions `airflow_actions`, `allowed_actions`, `max_actions`,
`max_cleared_task_instances`, `allow_include_downstream`, `allow_include_upstream`,
`actions_other_runs`. `ai.mcp_client.*`: `servers`, `database_servers`, `max_tools_per_server`,
`max_output_chars`, `connect_timeout_seconds`, `store_url`. The system prompt is `TROUBLESHOOT_SYSTEM`; it
can be replaced or extended like every prompt (see
[ai-assistants.md](ai-assistants.md#prompt-variables)). Every field is listed in
[configuration-reference.md](configuration-reference.md).
