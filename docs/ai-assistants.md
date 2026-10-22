# AI assistants ("Ask AI")

Reference for contributors and operators. Task-oriented guide on the documentation site: [AI assistants](https://ctolon.github.io/autoair/docs/ai/assistants).

AutoAir has two chat assistants. Both are tool-using agents with persisted conversations.
Open them from **AI → Ask AI…**, from the command palette, or with **Ctrl/⌘+Shift+A**. The
chat opens as a right-side drawer, which you can expand to full screen.

| | Developer assistant | Admin assistant |
|---|---|---|
| Who | `ai:use` | `ai:admin` **or** `admin:panel` |
| System prompt | `DEVELOPER_ASSISTANT_SYSTEM` | `ADMIN_ASSISTANT_SYSTEM` |
| Tools | catalog search / operator schema, the open DAG (`get_current_spec`), validate / lint / render code / deploy dry run, saved specs, the user's runs, task instances and logs (through Airflow with the **user's** token), research (installed provider docs and source; online when `ai.research.online`), code writers (`write_code`), memory (`list_conventions`, `remember`) | settings reference, effective configuration (secrets masked; needs `admin:config` and `AUTOAIR_EXPOSE_CONFIG`), feature flags, IAM overview, `who_has_permission`, `explain_user_access`, replicas and database, AI usage and cost, audit search, catalog/provider inventory, `find_dags_using_operator`, deploy targets, the proposals queue, health, memory |
| Changes | `propose_spec_change`: findings with JSON patches, or a full DagSpec, validated server-side and shown as a reviewable diff | `propose_config_snippet`: a YAML/env snippet shown as text; nothing is written |
| Memory scope | conventions/preferences without the `assistant:admin` tag | memories tagged `assistant:admin` |
| Settings ([reference](configuration-reference.md#aiassistantsdeveloper)) | `ai.assistants.developer.enabled`, `tools`, `max_steps`, `memory`, `allow_external_mcp` | `ai.assistants.admin.enabled`, `tools`, `max_steps`, `memory`, `allow_external_mcp` |

Each assistant has a tool allow-list, `ai.assistants.<mode>.tools` (fnmatch). The developer
assistant is also limited by `ai.guardrails.tool_allowlist` for the caller's role. The admin
assistant's tools are read-only by construction, except `remember`. Calls of tools that change
something wait for the user's approval ([Tool-call approval](#tool-call-approval)).

## A turn

`POST /api/ai/assistant/chat/stream` (Server-Sent Events) runs one turn:

1. Loads or creates the thread and stores the user message.
2. **Context compaction.** When the history sent to the model would exceed
   `ai.assistants.compaction_trigger_chars`, older messages are folded into a rolling summary
   with one `COMPACT_SYSTEM` call. The last `compaction_keep_messages` messages always stay
   verbatim. The model sees the summary followed by the recent turns: each answer stores its
   pydantic-ai messages (tool calls and results included; never returned by the API), so a
   follow-up sees what the tools returned before. Tool results of earlier turns are
   shortened by a history processor.
3. Runs the bounded pydantic-ai agent (`ai.assistants.<mode>.max_steps`, plus the `ai.agent.token_budget`,
   `ai.agent.cost_budget_usd` and `ai.agent.timeout_seconds` budgets). Guardrails apply to every model call: inputs are checked (size,
   external guardrails), outgoing text is redacted, tool output is treated as untrusted
   data, and streamed and final answers are scrubbed of secrets.
4. Streams `status` (`thread` carries the thread id, `compact`, `model`), `token`, `tool_call`,
   `tool_result`, `usage` and `guardrail` events. It ends with a `result` of kind
   `assistant_turn`, holding the stored user and assistant messages, then `done`.
5. Stores the answer with its tool calls, numbered **citations**, **follow-ups**,
   **proposals**, token usage/cost and guardrail notes. The turn is written to the AI audit
   log as feature `assistant:<mode>`, with the tools that ran, the sources used, the thread
   id and the proposal ids.

**Citations.** Every source a tool returns gets a number: search hits, source files, fetched
pages, task logs, audit records, settings and specs. The tool result carries it as `cite`,
the answer cites it as `[n]`, and the UI lists the cited sources under the answer, linking
web pages.

**Follow-ups.** The prompt asks the model to end its answer with a `<follow_ups>` block. The
server strips the block and stores up to `ai.assistants.max_follow_ups` questions, which the
UI shows as buttons.

**Regenerate.** Send `regenerate: true` with the `thread_id`: the last answer is replaced.
**Stop**: closing the stream cancels the turn, and the stored answer is marked `cancelled`.

## Threads

Threads are stored per user and mode in the persistence layer: collection `ai_threads`, as
files under `<state>/ai-threads` or rows of the SQL database. `ai.assistants.store_url`
moves the file location. Retention: `max_threads_per_user` per mode, and
`max_messages_per_thread`.

| Endpoint | |
|---|---|
| `GET /api/ai/assistant/info` | Which assistants the caller may use |
| `GET /api/ai/assistant/threads?mode=` | The caller's threads, newest first |
| `GET /api/ai/assistant/threads/{id}` | One thread with its messages |
| `PATCH /api/ai/assistant/threads/{id}` | Rename |
| `DELETE /api/ai/assistant/threads/{id}` | Delete |
| `POST /api/ai/assistant/threads/{id}/proposals/{pid}/decision` | `approve` / `reject` / `applied` (approving or applying a spec change needs `spec:write`); audited as `assistant:proposal_<decision>` |

Requests for another user's thread return 404.

## Proposals: review, then apply

A spec proposal stores the base spec (the editor's DAG at the time of the turn), the
findings with their patches, the validated proposed spec and its problems. The UI shows:

* the structured spec diff (`/api/vcs/diff`);
* validation problems;
* on demand, a side-by-side diff of the generated Python file.

**Apply to the editor** loads the proposed spec as unsaved work and records the approval.
**Deploy…** then opens the normal deploy dialog, which applies governance, four-eyes approval,
pull requests and the `deploy` permission. Once the builder reports the DAG saved under a new
version or PR, the proposal is recorded as `applied` together with the deploy outcome.
Nothing changes without these explicit steps.

Code blocks in answers have **Copy** and **Insert** buttons. Insert puts Python into the
selected `python_task` and Bash/SQL into the selected operator's `bash_command` / `sql`.

## Tool-call approval

Guide on the documentation site: [Tool-call approval](https://ctolon.github.io/autoair/docs/ai/tool-approval).

Tools that change something outside the run are deferred tools: the agent can call them, but
the call runs only after the user who started the run approves it.

**Classification.** Every agent tool has a class, `AgentTool.kind` (`ai/tools.py`): `write`
for `remember` (stores a memory) and for external MCP tools an admin has not listed in the
server's `read_only_tools` (`mcp_client.py::trusted_read_only`; the server's own
`readOnlyHint` is shown to admins but never trusted); `execute` for tools that run Python on
the server (`import_python_dag`, `deploy_dry_run`; `ai/toolbox.py`); `read` for everything
else. Neither the model nor the external server decides. `execute` tools are dropped for
callers without `code:execute` (`ai/tools.py::executable_tools`, applied after the role
allow-list in `chat.assistant_tools` and `routers/ai.py::_agent_tools`). The policy
(`ai.approvals`, `ai/approvals.py::needs_approval`) selects the tools whose calls wait: by
class (`classes`, default `["write"]`; `write` also gates `execute`, so rules written before
that class gate it too), by name pattern (`tools`, always wait), minus exemptions (`exempt`).
`ALWAYS_APPROVE` (`remember`) waits whatever the rule says: a prompt-injected memory would
reach every later prompt of its scope. The agent's `remember` stores `team` memories only for
callers with `admin:config` (`dev_tools.MEMORY_ADMIN_PERMISSION`). `ai.approvals.agents.<developer|admin|troubleshoot>` replaces
`ai.approvals.default` for one agent. With `ai.approvals.enabled=false` the tools that would
wait are not offered to the model at all. One-shot agent requests that cannot pause
(`POST /api/ai/ask`) never offer them.

**Pausing.** `ai/agent.py` wraps the selected tools so a call raises pydantic-ai's
`ApprovalRequired` unless `ctx.tool_call_approved`, and adds `DeferredToolRequests` to the
output types. The run then ends with `stopped="approval"` and the pending calls
(`PendingToolCall`: id, tool, class, description, arguments with secret-named fields masked,
strings scrubbed by `scrub_secrets` and the guardrail pipeline, registered request secrets
masked, cut to `ai.approvals.max_argument_chars`, with `omitted_chars` and
`arguments_sha256`, the SHA-256 of the canonical JSON of the exact arguments).
`GET /api/ai/approvals/{id}/calls/{call_id}/arguments` returns a pending call's complete
arguments from the stored messages, masked the same way but not cut, with the digest. Read-only calls of the same model turn run
normally. The caller stores a `ToolApproval` document (collection `ai_tool_approvals`, key
`<user hash>.<id>`): the view, the run's pydantic-ai messages, the request and context needed
to rebuild the tool set (spec, MCP selections, troubleshooting scope and evidence pack, the
citations so far), the model requests and steps already used, and `expires_at`
(`ai.approvals.ttl_seconds`). A chat answer carries the view in `ChatMessage.approvals`; an
analysis in `TroubleshootAnalysis.approvals`, with `status="awaiting_approval"`. The stream's
`status` event `approval` announces the pause, and the turn's audit record names
`approval_pending` and `approval_tools`.

**Deciding.** `POST /api/ai/approvals/{id}/stream` with one decision per pending call
(`{"decisions": [{"id": "<tool call id>", "approved": true, "arguments_sha256": "…"}]}`, or
`"approved": false` with an optional `"reason"`):

1. The approval is looked up under the caller's user key: another user gets 404, so only the
   user who started the run can decide it. The agent's own gate applies again (the admin
   assistant needs `ai:admin` or `admin:panel`; troubleshooting needs `airflow:read` and
   `features.airflow`). No new permission exists for approving.
2. The decisions are validated (every pending call exactly once, reasons at most
   `ai.approvals.max_reason_chars`; 422). Every approved call must carry the
   `arguments_sha256` it was shown, and the tool call in the stored messages (what the
   resumed run executes) must still hash to it (`approvals.check_bound_arguments`); otherwise
   409, audited as `<feature>:tool_approval_mismatch`, and nothing runs. A call paused by a
   version without digests can only be denied. The approval is then **claimed** with a
   compare-and-set: `pending` -> `resuming` with the claimer and time. An expired approval is
   stored as `expired` (410, audited as `<feature>:tool_approval_expired`); anything not
   pending is 409. Two replicas receiving the same decision resume the run once.
3. Each decision is written to the AI audit log and the audit trail as
   `assistant:tool_approved` / `assistant:tool_denied` (or `troubleshoot:…`) with the tool,
   call id, reason and approval id.
4. The run resumes: the tool set is rebuilt with the caller's current credentials and
   settings (a tool that is no longer allowed is not available), the stored messages are the
   history, and `DeferredToolResults` carry `ToolApproved` or `ToolDenied` (the denial text,
   with the reason, goes to the model; reasons pass the input guardrails). Budgets continue:
   the remaining model turns are the agent's `max_steps` minus those already used. The
   stream ends like the original one: `assistant_turn` (the same chat message, completed) or
   `troubleshoot` (the same analysis id). A resumed run may pause again with a new approval.
5. The approval is recorded as `resolved` (its messages and context are dropped), or `failed`
   when the run raised or the client went away. A refusal before the stream started (no
   model, guardrail) releases the claim, since nothing ran. A `resuming` claim older than
   `ai.approvals.claim_timeout_seconds` is shown as `failed` and never taken over, so an
   approved tool runs at most once.

A new message or a regenerate in the thread cancels a pending approval (409 while one is
being resumed); a paused answer is sent to the model as text afterwards, because its stored
messages end with unanswered tool calls. `GET /api/ai/approvals` lists the caller's open
approvals and `GET /api/ai/approvals/{id}` returns one; thread and analysis reads refresh the
approval views they carry. Beyond `ai.approvals.max_pending_per_user` the oldest pending
approval is cancelled; closed ones are deleted after `ai.approvals.retention_hours`.

**Relation to troubleshooting actions.** The Airflow actions of `ai.troubleshoot.airflow_actions`
are proposals in the finished analysis, executed by the API after the analysis; tool approval
pauses the agent before a tool call. They share the claim-then-record pattern and claim clock
(`airflow_actions.claim_expired`). In `hitl` mode every write tool call of the analysis waits
for approval even when `exempt` would let it run; in `read_only` and `apply` mode the
`ai.approvals` rule applies as configured.

**AI service.** The routes are part of the AI router, so with `ai.service_url` they are proxied
to the separate AI service like every `/api/ai/*` route; the service stores the approvals in
its own state and identifies the approver from the signed identity or the verified token.

## Prompts and prompt variables

Every system prompt can be replaced or extended:

1. `ai.prompts.override_dir`: `<NAME>.md` replaces prompt `NAME`. This takes precedence
   over the other two.
2. `ai.prompts.replace`: `{NAME: text}`.
3. `ai.prompts.append`: `{NAME: text}` is added after the prompt.

`GET /api/ai/prompts` lists the names and where each prompt comes from (`file`, `settings`,
`builtin`). The assistants' prompts are `DEVELOPER_ASSISTANT_SYSTEM`,
`ADMIN_ASSISTANT_SYSTEM`, `TROUBLESHOOT_SYSTEM`, `UPGRADE_SYSTEM`,
`DEPLOY_MESSAGE_SYSTEM` and `COMPACT_SYSTEM`.

<a id="prompt-variables"></a>**Variables** are written `${name}` and replaced in every prompt.
Unknown names are left untouched, and Jinja such as `{{ ds }}` is never affected.

| Variable | Value |
|---|---|
| `${organization}` | `ai.prompts.organization` (default "your organisation") |
| `${user}` | The caller |
| `${team}` | The `team` setting (`AUTOAIR_TEAM`) |
| `${assistant}` | `developer`, `admin` or `troubleshoot` |
| `${airflow_version}` / `${autoair_version}` | Installed versions |
| `${today}` | ISO date |
| `${online_research}` | "enabled", or "disabled (air-gapped)" |
| any key of `ai.prompts.variables` | Your own values |

## Project rules

Admins can write rules in Markdown, global or per team (scope `team:<name>`), in the admin
panel under **AI rules**, or through `PUT /api/ai/rules/{scope}`, which needs `ai:admin`.
`GET /api/ai/rules` and `GET /api/ai/rules/effective` read them. The rules are appended to
every authoring and writing prompt:

* generation;
* review, optimise and fix;
* task code and the writers;
* explain-DAG;
* the agent and the developer assistant;
* troubleshooting and upgrade.

They are stored in the `ai_rules` collection, limited to `ai.rules.max_chars`, cached for
`ai.rules.cache_seconds` and audited (`ai_rules_put` / `ai_rules_delete`).

## Commit and PR messages

In the deploy dialog, the PR description field has a **Generate** button that calls
`POST /api/ai/deploy-message`. It diffs the deployed spec against the spec being deployed and
sends only the structured diff to the model, which returns a commit subject of at most 72
characters and a Markdown PR body (Summary / Changes / Risk). Without a model, a
deterministic message is built from the same diff.

## Airflow upgrade assistant

**AI → Airflow upgrade assistant…**, or `POST /api/ai/upgrade` with `spec`, `dag_ids` or
`all: true`. It combines:

* AutoAir's Airflow 3 rules: removed template variables, deprecated operators, SLA → deadlines;
* ruff's `AIR3` rules (`ai.upgrade.ruff_rules`) on the generated code, task bodies included;
* for a single DAG, one LLM pass (`UPGRADE_SYSTEM`).

The result is findings with patches, a validated proposed spec and the code diff. **Apply**
loads the upgrade into the editor; you deploy it as usual.
