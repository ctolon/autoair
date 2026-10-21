---
title: AI writers and agent runtimes
description: The specialised code writers with their validate-and-repair loop, and the pydantic-ai agent runtime with its budgets.
sidebar_label: Writers and agent
---

The **AI writer** produces code for the environment AutoAir actually runs in, checks it with the
same tools a reviewer would use, and feeds every problem back to the model until the code is clean or
the repair budget is spent. The result always says whether it passed, so unchecked code is never
presented as valid.

Open it from **AI → AI writer…**, call `POST /api/ai/write` (or `/api/ai/write/stream` for progress
events), or use the MCP tool `write_code`. It needs `ai:use` and a configured model.

## Writer kinds

| Kind (`kind`) | UI label | Output | Checks |
|---|---|---|---|
| `task_body` | Python task body (`@task`) | Function body for the selected `python_task` | syntax, ruff, import availability, leak scan |
| `inline_function` | Python function (for example a callback or a branch condition) | A named function (`function_name`, `arguments`) | syntax, ruff, import availability, leak scan |
| `bash` | Bash script | Script | ShellCheck (when installed), leak scan |
| `sql` | SQL | Statement(s) in the chosen `dialect` | sqlglot when installed, else structural checks; no `UPDATE`/`DELETE` without `WHERE`; leak scan |
| `operator_config` | Operator task (from the catalog) | Operator class and parameters | catalog parameter schema, AutoAir validation, leak scan |
| `custom_operator` | Custom operator class | Python module | syntax, ruff, imports, operator structure (subclasses `BaseOperator`, `execute(self, context)`, Task SDK import), leak scan |
| `dag_spec` | Whole DAG | DagSpec | AutoAir validation, leak scan |

ruff runs with pyflakes, syntax and likely-bug rules plus `exec`/`eval` detection; its warnings of
those classes block like errors. **Import availability** means every import must resolve in this
Python environment, so generated code does not depend on a package the workers lack.

## How a write runs

1. **Environment profile.** The prompt describes the installed environment: Airflow and Python
   version, installed providers, importable top-level packages (at most
   `ai.writers.max_packages_in_prompt`, 120) and the linters available. `GET /api/ai/writers` returns
   this profile and the writer kinds.
2. **Context.** Team conventions from the AI memory, project [rules](/docs/ai/research-and-knowledge#project-rules),
   and, unless `research: false`, reference material retrieved from the installed providers' docs and
   the knowledge base (wrapped as untrusted data). For `operator_config`, the candidate operators of
   the installed catalog with their parameter schemas (at most
   `ai.writers.max_operators_in_prompt`, 25).
3. **Structured answer.** The model answers in a Pydantic-validated schema: code, explanation,
   requirements, connections used.
4. **Validate and repair.** The checks for the kind run (each ruff/ShellCheck run limited to
   `ai.writers.lint_timeout_seconds`, 15 s). Remaining problems go back to the model for up to
   `ai.writers.max_repairs` rounds (default 2).
5. **Result.** `ok`, the code, the remaining `issues` (severity, check, line, message), leak
   `findings`, the `checks` that ran, the number of `rounds`, the model and token usage.

The streaming endpoint emits `status` events with stages `environment`, `research`, `write`,
`validate` and `repair`, then a `result` of kind `writer`.

In the dialog, choose **What to write**, describe it (Ctrl/⌘+Enter runs), and review **Generated
code** with the check table. **Apply to …** writes a task body into the selected node; other kinds
are copied (**Copy**). A result marked *has problems* lists what is still wrong.

The guardrail `ai.guardrails.check_generated_code` (default `true`) enforces this output policy.
Install `autoair[format]` for ruff and `autoair[shell]` for ShellCheck; without them those checks are
skipped and not listed in `checks`.

Request fields of `POST /api/ai/write`: `kind`, `description`, `spec` (the open DAG), `node_id`,
`existing` (current code to change), `function_name`, `arguments`, `dialect`, `operator`
(preferred operator), `research`, `model`.

Each writer has its own system prompt: `WRITER_TASK_BODY_SYSTEM`, `WRITER_INLINE_FUNCTION_SYSTEM`,
`WRITER_BASH_SYSTEM`, `WRITER_SQL_SYSTEM`, `WRITER_OPERATOR_SYSTEM`,
`WRITER_CUSTOM_OPERATOR_SYSTEM`; see [Prompts](/docs/ai/research-and-knowledge#prompts).

## The agent

Open-ended questions (**Ask AutoAir**, `POST /api/ai/ask` and `/api/ai/ask/stream`), the
[chat assistants](/docs/ai/assistants) and [troubleshooting](/docs/ai/troubleshooting) run a
tool-using agent on a [pydantic-ai](https://ai.pydantic.dev) `Agent` (`GET /api/ai/status` reports
`runtime: pydantic-ai`):

- tool inputs are validated with Pydantic; when the model asks for several tools in one turn they run
  concurrently; repeated identical calls are served from a per-run cache; tool output is wrapped as
  untrusted data and truncated to `ai.agent.max_tool_output_chars`; a failing tool is reported to the
  model and never fails the run;
- the answer is a typed final answer (a proposed DagSpec in it is validated) or plain text;
- on the last allowed step the tools are withdrawn, so the model must answer.

Multi-turn state lives in the assistants' threads, which store each turn's pydantic-ai messages.

### Budgets

| Setting | Default | Limit |
|---|---|---|
| `ai.agent.max_steps` | 8 | Model turns per run |
| `ai.agent.token_budget` | 250000 | Input + output tokens per run |
| `ai.agent.cost_budget_usd` | unset | USD per run (needs `ai.pricing`) |
| `ai.agent.timeout_seconds` | 300 | Wall clock per agent or writer run |
| `ai.agent.final_turn_grace_seconds` | 30 | Reserved for the forced final turn (at most a quarter of the timeout) |
| `ai.agent.tool_timeout_seconds` | 90 | One tool call |
| `ai.agent.max_tokens_per_call` | 8000 | `max_tokens` of each model call |

When a limit is hit the run stops with `stopped` set to `max_steps`, `budget` or `timeout` and returns
what it completed; the UI shows *stopped at the step limit*, *stopped at the token budget* or
*stopped: time limit*.

## Related pages

- [`ai.writers`](/docs/reference/configuration#aiwriters), [`ai.agent`](/docs/reference/configuration#aiagent)
- [Guardrails](/docs/ai/guardrails)
- [AI architecture](/docs/ai/overview)
