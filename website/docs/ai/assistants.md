---
title: Chat assistants
description: The developer and admin chat assistants, what they can see and do, how a turn runs, threads, proposals, and their ai.assistants settings.
sidebar_label: Chat assistants
---

AutoAir has two chat assistants. Both are tool-using agents with persisted conversations, numbered
citations and suggested follow-ups. Open them from **AI → Ask AI…**, from the command palette
(**Ask AI (chat)**, **Ask the admin assistant**), or with **Ctrl/⌘+Shift+A**. The chat opens as a
right-side panel that you can expand to **Full screen**; the **Assistant** switch selects
**Developer** or **Admin**.

| | Developer assistant | Admin assistant |
|---|---|---|
| For | Building and debugging DAGs | Understanding how this AutoAir is set up and used |
| Permission | `ai:use` | `ai:admin` or `admin:panel` (otherwise 403 *the admin assistant needs ai:admin or admin:panel*) |
| Tools | Catalog search and operator schemas, the open DAG (`get_current_spec`), validation, lint, code rendering, deploy dry run, saved specs, your runs, task instances, logs and run statistics (through Airflow with **your** token), research (installed provider docs and source; online when `ai.research.online`), code writers (`write_code`), memory (`list_conventions`, `remember`) | Settings reference, effective configuration (secrets masked; needs `admin:config` and `AUTOAIR_EXPOSE_CONFIG`), feature flags, IAM overview, `who_has_permission`, `explain_user_access`, replicas and database, AI usage and cost, audit search, catalog and provider inventory, `find_dags_using_operator`, deploy targets, the approvals queue, health |
| Changes | `propose_spec_change`: findings with JSON patches, or a full DagSpec, validated server-side and shown as a reviewable diff | `propose_config_snippet`: a YAML or env snippet shown as text. Nothing is ever written |
| Memory scope | Conventions and preferences without the `assistant:admin` tag | Memories tagged `assistant:admin` |
| System prompt | `DEVELOPER_ASSISTANT_SYSTEM` | `ADMIN_ASSISTANT_SYSTEM` |
| Settings ([`ai.assistants.developer`](/docs/reference/configuration#aiassistantsdeveloper), [`ai.assistants.admin`](/docs/reference/configuration#aiassistantsadmin)) | `ai.assistants.developer.enabled`, `tools`, `max_steps`, `memory`, `allow_external_mcp` | `ai.assistants.admin.enabled`, `tools`, `max_steps`, `memory`, `allow_external_mcp` |

Example questions from the empty chat: *Explain what this DAG does and what could break in
production*, *Add sensible retries and timeouts to every task*, *Why did the last run of this DAG
fail?* (developer); *Why can't a Viewer deploy, and what would I change to allow it?*, *Which DAGs use
BashOperator?*, *How much did AI cost this month, per user?* (admin).

Each assistant has a tool allow-list, `ai.assistants.<mode>.tools` (fnmatch patterns, default all).
The developer assistant is further limited by `ai.guardrails.tool_allowlist` for the caller's AI role,
so a viewer cannot run tools that execute DAG code. The admin assistant's tools are read-only by
construction. Both assistants also get the tools of the external MCP servers scoped to them
(`ai.mcp_client.servers[].agents`), through the same allow-lists; the admin assistant only gets the
ones the server marks read-only. See [External MCP servers](/docs/ai/mcp#chat-assistants).

## A turn

`POST /api/ai/assistant/chat/stream` (Server-Sent Events) runs one turn. Request fields:
`thread_id` (continue a thread), `mode` (`developer` or `admin`), `message`, `spec` (the DAG open in
the editor), `model`, `regenerate`, `research` (developer: give it the research tools), `mcp`
(external MCP servers and tools to attach; default all servers scoped to the assistant, `[]` none).

1. The thread is loaded or created, and the user message is stored immediately.
2. **Context compaction.** When the history sent to the model would exceed
   `ai.assistants.compaction_trigger_chars` (40000), older messages are folded into a rolling summary
   by one `COMPACT_SYSTEM` call (`summary_max_tokens`, 1500). The last `compaction_keep_messages` (6)
   messages always stay verbatim. The model sees the summary followed by the recent messages.
3. The bounded tool loop runs: `ai.assistants.<mode>.max_steps` model turns (default 10) plus the
   the `ai.agent.token_budget`, `ai.agent.cost_budget_usd` and `ai.agent.timeout_seconds` budgets. [Guardrails](/docs/ai/guardrails) apply to every call:
   input checks, redaction of outgoing text, tool output treated as untrusted data, secrets scrubbed
   from streamed and final answers.
4. The stream sends `status` events (`thread` carries the thread id, then `compact`, `model`),
   `token`, `tool_call`, `tool_result`, `usage` and `guardrail` events, and ends with a `result` of
   kind `assistant_turn` (the stored user and assistant messages) followed by `done`.
5. The answer is stored with its tool calls, citations, follow-ups, proposals, token usage and cost,
   and guardrail notes. The turn is written to the AI audit log as feature `assistant:<mode>` with the
   tools that ran, the sources used, the thread id and the proposal ids.

**Citations.** Every source a tool returns gets a number: search hits, source files, fetched pages,
task logs, audit records, settings and specs. The answer cites it as `[n]`, and the panel lists the
cited **Sources** under the answer, linking web pages.

**Follow-ups.** The model ends its answer with suggested questions; the server strips them from the
text and stores up to `ai.assistants.max_follow_ups` (3), which the panel shows as buttons.

**Regenerate** (`regenerate: true` with the `thread_id`) replaces the last answer. **Stop** closes
the stream, which cancels the turn; the stored answer is marked cancelled (*Stopped before an answer
was given*). An answer cut short by a budget says *The answer is incomplete (…)*.

**Code blocks** have **Copy code** and **Insert into …**: Python goes into the selected
`python_task`, Bash and SQL into the selected operator's `bash_command` / `sql`.

## Threads

Threads are stored per user and assistant in the persistence layer: the `ai_threads` collection, as
files under `<state>/ai-threads` (`ai.assistants.store_url` moves them) or as rows of the SQL
database when `database.backend` is `sqlite` or `postgresql`. Retention: `max_threads_per_user` (200)
per assistant, oldest deleted first, and `max_messages_per_thread` (400), oldest dropped.

In the panel, **New chat** starts a thread and **Conversations** lists earlier ones with
**Rename** and **Delete**.

| Endpoint | |
|---|---|
| `GET /api/ai/assistant/info` | Which assistants the caller may use |
| `GET /api/ai/assistant/threads?mode=` | The caller's threads, newest first |
| `GET /api/ai/assistant/threads/{id}` | One thread with its messages |
| `PATCH /api/ai/assistant/threads/{id}` | Rename |
| `DELETE /api/ai/assistant/threads/{id}` | Delete |
| `POST /api/ai/assistant/threads/{id}/proposals/{pid}/decision` | `approve`, `reject` or `applied` (with an optional `comment` and deploy `detail`); approving or applying a spec change needs `spec:write`. Audited as `assistant:proposal_<decision>` |

Other users' threads answer 404.

## Proposals: review, then apply

A spec proposal stores the base spec (the editor's DAG at the time of the turn), the findings with
their patches, the validated proposed spec and its problems. The proposal card shows the structured
spec diff, validation problems and, on demand, the side-by-side diff of the generated Python file
(see [Proposals and the spec-diff review](/docs/ai/assist#proposals-and-the-spec-diff-review)).

- **Apply to the editor** loads the proposed spec as unsaved work and records the approval.
- **Deploy…** then opens the normal deploy dialog, which applies governance, four-eyes approval, pull
  requests and the `deploy` permission. When the builder reports the DAG saved under a new version or
  PR, the proposal is recorded as `applied` with the deploy outcome.
- **Reject** records the rejection.

Admin-assistant snippets carry *A suggestion only: nothing was changed* with **Mark as applied** and
**Dismiss**. Nothing changes without these clicks.

## Settings

| Setting | Default | |
|---|---|---|
| `ai.assistants.developer.enabled` / `admin.enabled` | `true` | Offer the assistant |
| `ai.assistants.developer.tools` / `admin.tools` | `["*"]` | Tool allow-list (fnmatch) |
| `ai.assistants.developer.max_steps` / `admin.max_steps` | 10 | Model turns per chat turn |
| `ai.assistants.developer.memory` / `admin.memory` | `true` | Use and offer to store memories in the assistant's scope |
| `ai.assistants.developer.allow_external_mcp` / `admin.allow_external_mcp` | `true` | Attach the external MCP servers scoped to the assistant |
| `ai.assistants.compaction_trigger_chars` | 40000 | Compact a thread above this many characters |
| `ai.assistants.compaction_keep_messages` | 6 | Recent messages always sent verbatim |
| `ai.assistants.summary_max_tokens` | 1500 | `max_tokens` of the compaction call |
| `ai.assistants.max_follow_ups` | 3 | Suggested follow-up questions per answer |
| `ai.assistants.max_threads_per_user` | 200 | Threads kept per user and assistant |
| `ai.assistants.max_messages_per_thread` | 400 | Messages kept per thread |
| `ai.assistants.store_url` | `<state>/ai-threads` | Thread location (SQL rows with a database backend) |

```bash
# Read-only developer assistant without code writers, and no admin assistant
AUTOAIR_AI__ASSISTANTS__DEVELOPER__TOOLS='["search_*", "get_*", "validate_spec", "lint_spec", "render_code", "list_*", "read_source", "propose_spec_change"]'
AUTOAIR_AI__ASSISTANTS__ADMIN__ENABLED=false
```

Prompts can be replaced or extended, with `${variables}` such as `${organization}` and `${assistant}`;
see [Prompts](/docs/ai/research-and-knowledge#prompts). Project rules are appended to the developer
assistant's prompt.

## Related pages

- [`ai.assistants`](/docs/reference/configuration#aiassistants)
- [Assist features](/docs/ai/assist)
- [Troubleshooting runs](/docs/ai/troubleshooting)
- [Tool-call approval](/docs/ai/tool-approval): `remember` and MCP tools that change something wait for your decision
- [Guardrails and AI audit](/docs/ai/guardrails)
