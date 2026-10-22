---
title: Tool-call approval
description: How AutoAir's agents pause before a tool that changes something, how you approve or deny each call, and the ai.approvals settings.
sidebar_label: Tool-call approval
---

AutoAir's agents read freely, but a tool call that changes something waits for your decision.
When the developer or admin assistant, or a troubleshooting analysis, calls such a tool, the
run pauses and shows an approval card. Nothing runs until you decide; the run then continues
where it stopped.

## Which tool calls wait

Every agent tool is **read-only**, **changes something** or **runs code on the server**:

| Class | Tools |
|---|---|
| changes something (`write`) | `remember` (stores a memory); external MCP tools an administrator has not listed in the server's `read_only_tools` |
| runs code on the server (`execute`) | `import_python_dag` (imports a Python DAG file) and `deploy_dry_run` (imports the generated file in a sandboxed DagBag) |
| read-only (`read`) | Every other built-in tool (catalog, validation, specs, runs and logs, research, settings, audit); external MCP tools listed in `read_only_tools` |

The classification comes from AutoAir and the administrator, never from the model or the
external MCP server: a server's own `readOnlyHint` is not trusted, because a compromised server
could mark a destructive tool read-only ([MCP client](/docs/ai/mcp#mcp-client-external-servers-for-autoairs-agents)).
By default calls that change something or run code wait. `execute` tools are also offered only
to users with `code:execute`, whatever the tool allow-list says. Every `remember` call waits,
even when an administrator exempted it. An administrator can make more tools wait or let
specific ones run directly ([settings](#settings)). Proposals (`propose_spec_change`,
`propose_config_snippet`) never need approval: they change nothing until you apply them.

## Deciding

The paused answer shows the card **Approve a tool call** (or **Approve N tool calls**) with,
for each call:

- the tool name and whether it **changes something**;
- its description;
- its arguments. Fields named like secrets (`password`, `token`, `api_key`, ...) are shown as
  `***`, secret-looking values are masked and long values are cut to
  `ai.approvals.max_argument_chars`. A cut call says how much is hidden (**N more characters
  not shown — review the full arguments before approving.**); **Show full arguments** loads
  everything the call would run with, secrets still masked.

An approval is bound to the exact arguments of the call: the card sends back their SHA-256
digest, and the run resumes only if the stored call still has exactly those arguments.
Otherwise the decision is refused (409, audited as `assistant:tool_approval_mismatch`) and
nothing runs.

Choose **Approve** or **Deny** for each call, optionally with a reason for a denial (**Reason
(optional, sent to the assistant)**), then **Send decisions**. **Approve all** and **Deny all**
appear when the card has several calls. The run resumes in the same answer: approved calls run,
denied calls do not and the assistant is told you denied them, with your reason. The card stays
under the answer with each decision and **Decided by**.

| Card status | Meaning |
|---|---|
| waiting for you | Pending; expires after `ai.approvals.ttl_seconds` (default one hour) |
| resuming | Your decisions were accepted; the run is continuing |
| decided | The run continued with your decisions |
| expired | Not decided in time; nothing ran. Ask again |
| cancelled | You sent a new message (or regenerated) instead; nothing ran |
| failed | The resumed run failed (the reason is shown) |

Sending a new message while an approval is pending cancels it; the composer says so
(**Sending a new message cancels the pending tool approval.**).

In **Analyze with AI**, a paused analysis has the status **waiting for tool approval** and shows
the same card above the report. After your decision it continues under the same analysis.

### Who can decide

Only the user who started the run. Other users cannot see the approval (the API answers 404).
You still need the permission of the agent itself when you decide: `ai:use`, plus `ai:admin` or
`admin:panel` for the admin assistant, and `airflow:read` for troubleshooting.

A decision is accepted once. If the same decision reaches two replicas, or you click twice, the
run resumes once and the second request is refused (409), so an approved tool runs at most once.
Every approval, denial, expiry and mismatch is recorded in the AI audit log
(`assistant:tool_approved`, `assistant:tool_denied`, `assistant:tool_approval_expired`,
`assistant:tool_approval_mismatch`, and the same with `troubleshoot:`).

## Troubleshooting and Airflow actions

Tool approval pauses the agent before a tool call. Airflow actions (clear, retry, mark state) are
different: the analysis proposes them in its report and AutoAir executes them afterwards
according to `ai.troubleshoot.airflow_actions` ([Airflow actions](/docs/ai/troubleshooting#airflow-actions)).
The modes also apply to tools: in `hitl` mode every call of a tool that changes something waits
for approval, even when the administrator exempted it; in `read_only` and `apply` mode the
`ai.approvals` rules apply as configured.

## Settings

All under `ai.approvals` ([reference](/docs/reference/configuration#aiapprovals)):

| Setting | Default | Effect |
|---|---|---|
| `enabled` | `true` | `false`: tools that would need approval are not offered to the agent at all |
| `default.classes` | `["write"]` | Tool classes whose calls wait (`write`, `execute`, `read`); `write` also covers `execute` tools |
| `default.tools` | `[]` | Tool names (fnmatch) that always wait, e.g. `mcp__jira__*` |
| `default.exempt` | `[]` | Tool names (fnmatch) that never wait, e.g. `mcp__metrics__*`; ignored for `remember` |
| `agents` | `{}` | A rule per agent (`developer`, `admin`, `troubleshoot`) replacing `default` |
| `ttl_seconds` | `3600` | Time to decide before the approval expires |
| `max_pending_per_user` | `20` | A new pending approval beyond this cancels your oldest one |
| `retention_hours` | `168` | How long decided, expired and cancelled approvals are kept |
| `claim_timeout_seconds` | `900` | A resumed run that has not finished after this is shown as failed |
| `max_argument_chars` | `4000` | Characters of arguments shown on the card |
| `max_reason_chars` | `1000` | Longest denial reason (0 = no reasons) |
| `store_url` | `<state>/ai-approvals` | Where pending approvals live (SQL rows with a database backend) |

```yaml
ai:
  approvals:
    agents:
      developer:
        exempt: ["deploy_dry_run"]      # dry runs without asking (users with code:execute)
      troubleshoot:
        classes: ["write"]
        tools: ["mcp__metrics__query"]  # also ask before this read-only tool
```

Pending approvals are stored with the rest of AutoAir's state, so several replicas and the
separate [AI service](/docs/ai/ai-service) work without sticky sessions.

## Related pages

- [Chat assistants](/docs/ai/assistants)
- [AI troubleshooting](/docs/ai/troubleshooting)
- [MCP client](/docs/ai/mcp#mcp-client-external-servers-for-autoairs-agents)
- Internals: [Tool-call approval in docs/ai-assistants.md](https://github.com/ctolon/autoair/blob/main/docs/ai-assistants.md#tool-call-approval)
