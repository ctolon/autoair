---
title: MCP server and client
description: Connect Claude Desktop, Claude Code, Cursor or VS Code to AutoAir's MCP server, run it in the app, the AI service or standalone, and let AutoAir's agents use external MCP servers.
sidebar_label: MCP
---

AutoAir speaks the [Model Context Protocol](https://modelcontextprotocol.io) in both directions:

- as an **MCP server**, it gives external AI agents DAG *authoring*: the operator catalog, DagSpec
  validation and code generation, saved specs, reviews, research over installed providers, and
  Airflow status and logs. Existing Airflow MCP servers focus on operations; AutoAir adds building,
  validating, reviewing and deploying DAGs.
- as an **MCP client**, its troubleshooting agent can call tools of external MCP servers you allow,
  such as a metrics, ticketing or runbook server.

The server needs the `mcp` extra (`pip install 'autoair[mcp]'`: FastMCP 4.x on the official `mcp` SDK
2.x). Without it the `/mcp` endpoint is not mounted, start-up logs
*MCP endpoint disabled … (install autoair[mcp])* and `/api/meta` reports `mcp_unavailable_reason`.
FastMCP 4 cannot be installed next to Airflow 3.1; there, run the server in its own environment
([Airflow compatibility](/docs/installation/compatibility#mcp-next-to-airflow-31)).

For coding agents that only need AutoAir's documentation, `autoair docs-mcp` serves just the
documentation tools, without Airflow or configuration: see [AutoAir docs MCP](/docs/ai/docs-mcp).

## Where the server runs

The same server can be served in several places. Over HTTP it always uses streamable HTTP and always
sits behind authentication.

| Mode | Endpoint | Authentication |
|---|---|---|
| Airflow plugin (default) | `https://<airflow>/autoair/mcp/` | Airflow's auth manager, like the AutoAir API |
| Standalone server (`autoair serve`) | `http://<host>:8765/mcp/` | The caller's Airflow bearer token, verified against Airflow |
| AI service (`autoair ai-serve`, `ai.service.serve_mcp`) | `http://<ai-host>:8790/mcp/` | Signed identity from the proxy, or a bearer token verified against Airflow |
| Main server proxying to the AI service (`ai.service.proxy_mcp=true`) | unchanged (`…/mcp/`) | Main server auth, then the signed identity |
| `autoair mcp-serve --http` | `http://<host>:8766/mcp/` | Bearer token verified against Airflow (`--no-auth` for local experiments) |
| `autoair mcp-serve`, `autoair mcp`, `autoair-mcp` | stdio | The local user; Airflow calls use `AUTOAIR_AIRFLOW_TOKEN` |

`autoair mcp --http` and `autoair-mcp --http` also exist for local, **unauthenticated** use: like
`mcp-serve --http --no-auth` they refuse a non-loopback `--host`, answer only requests whose `Host`
(and `Origin`, when sent) names a loopback address or the host of `server.public_url`, and give every
caller `iam.anonymous_roles` (default Viewer, read-only). Prefer `autoair mcp-serve --http` for
anything shared. `AUTOAIR_FEATURES__MCP=false` unmounts `/mcp` from the
server and the AI service.

```bash
autoair mcp-serve --http --host 0.0.0.0 --port 8766
# AutoAir MCP at http://0.0.0.0:8766/mcp/ (Airflow at http://airflow-api-server:8080)
```

| `autoair mcp-serve` option | Default | |
|---|---|---|
| `--http` | off | Serve streamable HTTP instead of stdio |
| `--host` | `127.0.0.1` | Bind address (HTTP) |
| `--port` | `8766` | Port (HTTP) |
| `--no-auth` | off | HTTP: do not require a bearer token |

The stdio and standalone forms read the same configuration as the server: `AUTOAIR_*` environment
variables, `.env` and configuration files (every option is in the [configuration reference](/docs/reference/configuration)). The in-app endpoint runs a
stateless, per-request session manager because Airflow mounts plugin apps as sub-applications whose
ASGI lifespans never run.

## Connecting clients

HTTP clients authenticate with an Airflow JWT in the `Authorization` header. Obtain one the way your
Airflow deployment issues API tokens (for example `POST /auth/token` on the Airflow API server); every
Airflow call the MCP server makes is forwarded with that token, so Airflow's RBAC applies.

Over stdio the server runs as a local process of the user who started it. Set `AUTOAIR_AIRFLOW_URL`
and `AUTOAIR_AIRFLOW_TOKEN` for the Airflow tools, and optionally `AUTOAIR_MCP_USER` as the identity
for user-scoped memories (default: the OS user). A token without a space is sent as
`Bearer <token>`.

### Claude Code

Remote, through the Airflow plugin:

```bash
claude mcp add --transport http autoair https://airflow.example.com/autoair/mcp/ \
  --header "Authorization: Bearer $AIRFLOW_JWT"
```

Local, over stdio:

```bash
claude mcp add autoair \
  -e AIRFLOW_HOME="$AIRFLOW_HOME" \
  -e AUTOAIR_AIRFLOW_URL=http://localhost:8080 \
  -e AUTOAIR_AIRFLOW_TOKEN="$AIRFLOW_JWT" \
  -- autoair mcp
```

### Claude Desktop

`claude_desktop_config.json` (stdio):

```json
{
  "mcpServers": {
    "autoair": {
      "command": "autoair",
      "args": ["mcp"],
      "env": {
        "AUTOAIR_AIRFLOW_URL": "http://localhost:8080",
        "AUTOAIR_AIRFLOW_TOKEN": "<airflow jwt>",
        "AUTOAIR_DAGS_FOLDER": "/opt/airflow/dags"
      }
    }
  }
}
```

Use the absolute path of the `autoair` executable (for example the one in your virtual environment)
if it is not on the PATH Claude Desktop sees.

### Cursor

`.cursor/mcp.json` in the project, or `~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "autoair": {
      "url": "https://airflow.example.com/autoair/mcp/",
      "headers": { "Authorization": "Bearer <airflow jwt>" }
    }
  }
}
```

For stdio, use `"command": "autoair", "args": ["mcp"], "env": { … }` as for Claude Desktop.

### VS Code

`.vscode/mcp.json`, prompting for the token instead of storing it:

```json
{
  "inputs": [
    { "type": "promptString", "id": "airflow-token", "description": "Airflow JWT", "password": true }
  ],
  "servers": {
    "autoair": {
      "type": "http",
      "url": "https://airflow.example.com/autoair/mcp/",
      "headers": { "Authorization": "Bearer ${input:airflow-token}" }
    }
  }
}
```

For stdio: `"type": "stdio", "command": "autoair", "args": ["mcp"], "env": { … }`.

## Tools

Every tool has typed Pydantic inputs and outputs (`outputSchema`), a description that says when to
use it, MCP annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`) and size
limits (paging with `offset` → `next_offset`, log tails, truncated code).

| Group | Tool | AutoAir permission |
|---|---|---|
| Catalog | `search_knowledge` (ranked search over operators, guides, saved DAGs, templates), `list_operators`, `get_operator_schema`, `get_dagspec_schema`, `list_node_kinds`, `list_notifiers` | `builder:use` |
| Specs | `validate_dag_spec` (static checks only; nothing runs), `apply_spec_patch` (JSON-patch edits addressed by node id), `generate_code` | `builder:use` |
| Specs | `import_check_dag_spec`: validate and import the generated DAG in a sandboxed DagBag (runs its top-level code) | `builder:use` + `code:execute` |
| Specs | `list_dags` (filter `document_type`: `spec` or `raw`), `get_dag_spec`, `get_dag_code` (the DAG file: a raw code DAG's code or a visual DAG's generated file, with `document_type`) | `spec:read` |
| Specs | `save_dag_spec` (confirm) | `spec:write`; + `deploy` when the save goes live (not a pull request or a proposal); + `code:raw` for a spec with `raw_code` or new or changed `source.code` |
| Specs | `save_raw_dag_code` (confirm): save a [raw code DAG](/docs/user-guide/code-mode#raw-code-dags); updates an existing raw code DAG (keeping its team and environments) without `deploy:overwrite_ejected`, as long as its file is unchanged since AutoAir wrote it (MCP has no overwrite, so a file changed outside AutoAir is refused); refuses to replace a visual DAG | `spec:write` + `code:raw`; + `deploy` when the save goes live |
| Specs | `delete_dag_spec` (confirm) | `spec:delete`; + `deploy` when the delete goes live |
| Specs | `import_python_dag` | `spec:write` + `code:execute` |
| [Operator templates](/docs/user-guide/operator-templates#mcp) | `list_operator_templates`, `get_operator_template` (a version and the history) | `builder:use` (+ `features.templates`) |
| [Operator templates](/docs/user-guide/operator-templates#mcp) | `save_operator_template` (confirm; validated, needs the `etag` as `if_match` to publish a new version), `delete_operator_template` (confirm) | `template:write` (+ `features.templates`) |
| Airflow | `list_connections`, `list_variables`, `get_dag_status`, `list_dag_runs`, `get_task_log` | `airflow:read` |
| Airflow | `trigger_dag` (confirm) | `airflow:read` + `run:trigger` |
| Analysis | `review_dag_spec`, `scan_dag_leaks` (rules; work without the AI extra or `features.ai`) | `builder:use`; with `use_ai: true` also `ai:use` and `features.ai` |
| Analysis | `optimize_dag_parameters` (from run history) | `ai:use` + `airflow:read` |
| Server-side LLM | `generate_dag_from_prompt`, `fix_dag_spec`, `write_code` | `ai:use` |
| Research | `search_installed_docs`, `read_source`, `list_installed_packages`, `get_package_info`, `fetch_doc` (online research only) | `ai:use` |
| Memory | `list_memories`, `remember`, `forget` | `ai:use`; `remember` with scope `global` or `team` + `admin:config` |
| [Knowledge base](/docs/ai/knowledge-base#mcp) | `kb_search`, `kb_sources`, `kb_status` (work without the AI extra) | `ai:use` (+ `features.kb`) |

The server-side LLM tools need AutoAir's own model ([Providers](/docs/ai/providers)); the analysis tools
take `use_ai: true` for an extra LLM pass and otherwise run rules only. `write_code` covers task
bodies, inline functions, Bash, SQL, operator configs, custom operators and DAG specs with the
[writers'](/docs/ai/writers) validate-and-repair loop.

**Resources**: `autoair://schema/dagspec`, `autoair://operators`, `autoair://operators/{fqn}/schema`,
`autoair://dags`, `autoair://dags/{dag_id}/spec`, `autoair://templates`, `autoair://templates/{id}`,
`autoair://docs`, `autoair://docs/{name}` (the enabled [knowledge-base](/docs/ai/research-and-knowledge#knowledge-base)
documents, including admin overrides), `autoair://memory`. DAG and template resources need
`spec:read`, the others `builder:use`.

**Prompts**: `build_dag_from_description`, `review_dag`, `fix_import_error`, `optimize_parameters`,
`scan_for_leaks`, `explain_dag`, `convert_existing_dag`.

## Security

- **Permissions.** Over HTTP the caller's IAM principal is resolved with the same strategies as the
  REST API. Each tool needs the permissions in the table above; tools the caller may not use are
  hidden from `tools/list`, and a call fails with *missing AutoAir permission '…' for …*. Unknown
  tools require `admin:config`. Over stdio no AutoAir principal applies: the process acts as the
  local user with their own files and credentials.
- **Feature flags** apply on every transport, stdio included: with `features.ai` off the AI tools are
  gone; `save_dag_spec`/`delete_dag_spec` need `features.deploy`, `trigger_dag` needs `features.runs`,
  `import_python_dag` needs `features.imports` and `features.python_code`. `save_raw_dag_code`, and
  `save_dag_spec` with a raw code DAG, also need `features.python_code` and `features.code_editor`.
- **Confirmation.** `save_dag_spec`, `save_raw_dag_code`, `delete_dag_spec` and `trigger_dag` act only with
  `confirm=true`; without it they dry-run and describe what would happen.
  `AUTOAIR_AI__MCP__ALLOW_WRITES=false` refuses them entirely (read-only server).
- **Etags.** Overwriting a DAG with `save_dag_spec` / `save_raw_dag_code` and deleting one with
  `delete_dag_spec` need `base_etag`, the `current_etag` the dry run returns; without it, or when
  the DAG changed since, nothing happens and the result carries `current_etag`
  (`storage.require_base_etag=false` drops the requirement).
- **Governance.** Confirmed writes over HTTP follow the REST deploy and delete rules. On a PR-mode
  git target, or when `iam.force_pr_without_direct_push` applies to the caller, they open a pull
  request (`status: pr_opened`). With `iam.require_approval` on, a write that would go live without
  another user's approval of that exact version is stored as a deploy or delete proposal
  (`status: pending_approval`, `proposal_etag`), as over REST; reviewers approve it in the AutoAir
  UI, and calling the tool again afterwards puts it live. Nobody's approval of a version they saved
  or proposed themselves counts. Over stdio writes land in the target's own mode.
- **Import check.** `validate_dag_spec` never runs code (its old `import_check` argument is ignored
  and the result's `note` says so); `import_check_dag_spec` imports the DAG and needs `code:execute`.
- **Airflow data.** Airflow calls use the caller's credentials. Connection and variable values and run
  `conf` are never returned.
- **Code execution.** `import_python_dag` executes the file in an isolated subprocess without
  AutoAir's secrets and requires `AUTOAIR_ALLOW_PYTHON_CODE=true`. Unexpected errors are masked.
- **Research** only reads installed sources and configured folders; `fetch_doc` exists only with
  online research and is limited to allow-listed domains with SSRF, size and rate limits. Fetched text
  is untrusted data.

### Memory

A small persistent store (`ai.memory_url`, any fsspec URL) of conventions and preferences with three
scopes: `global`, `team` (the server's `team` setting) and `user` (the JWT's `preferred_username`,
`username` or `sub` over HTTP; `AUTOAIR_MCP_USER` or the OS user over stdio). Text that looks like a
credential is rejected. Memories are also injected into the prompts of AutoAir's own AI features, so
"warehouse loads use `snowflake_prod`" applies in the editor and in every MCP client.

Global and team memories reach every user's prompts, so over HTTP only callers with `admin:config`
may add them (`remember` with `scope` `global` or `team`) or delete them (`forget`). Everyone with
`ai:use` may add personal memories (`scope: user`) and delete their own; `forget` refuses another
user's memory. Over stdio the local user manages every scope. AutoAir's own assistants ask for your
[approval](/docs/ai/tool-approval) before every `remember` call.

## MCP client: external servers for AutoAir's agents

AutoAir's troubleshooting agent can use tools of external MCP servers (fastmcp `Client` over
streamable HTTP, SSE or stdio). Each attached tool appears to the model as `mcp__<server>__<tool>`.

Servers come from two places:

| Source | Transports | Managed by |
|---|---|---|
| `ai.mcp_client.servers` in the configuration | `http`, `sse`, `stdio` (a local command) | Whoever controls the configuration |
| The admin panel's **MCP servers** tab (`/api/admin/mcp-servers`) | `http`, `sse` only, so the database can never make AutoAir run a command | Listing: `admin:panel`; create, change, delete and **Test connection**: `admin:config` |

Servers from the configuration are shown read-only in the tab. Admin-panel servers are stored in the
`ai_mcp_servers` collection with header values encrypted with the same Fernet key as user API keys
(`ai.user_llm.encryption_key`); they are never returned (`***` keeps a stored value when editing).
`ai.mcp_client.database_servers=false` ignores them.

Per server:

| Field | Default | |
|---|---|---|
| `name` | – | Short id, `^[a-z0-9][a-z0-9_-]{0,39}$`; the tool-name prefix |
| `description` | `""` | Shown to users |
| `transport` | `http` | `http` (streamable HTTP), `sse` or `stdio` |
| `url` | – | `http(s)` URL (http / sse) |
| `command`, `args`, `env` | – | stdio only; `env` values are secrets |
| `headers` | `{}` | HTTP headers such as `Authorization` (secrets) |
| `tools` | `["*"]` | Tools agents may call (fnmatch over the server's tool names) |
| `read_only_tools` | `[]` | Tools you vouch only read (fnmatch): they run without [approval](/docs/ai/tool-approval) and the admin assistant may use them. Every other tool needs approval |
| `timeout_seconds` | 30 | One tool call |
| `enabled` | `true` | Offer this server |
| `agents` | `["troubleshoot"]` | Agents that may attach it: `troubleshoot`, `developer`, `admin` |

```yaml
ai:
  mcp_client:
    servers:
      - name: grafana
        description: Dashboards and metrics
        url: https://grafana.example.com/mcp
        headers: { Authorization: "Bearer ..." }
        tools: ["query_*", "get_dashboard"]
        read_only_tools: ["query_*", "get_dashboard"]
        agents: [troubleshoot]
      - name: runbooks
        transport: stdio
        command: /opt/mcp/runbooks-server
        args: ["--readonly"]
```

```bash
AUTOAIR_AI__MCP_CLIENT__SERVERS='[{"name": "grafana", "url": "https://grafana.example.com/mcp", "headers": {"Authorization": "Bearer ..."}, "tools": ["query_*"], "agents": ["troubleshoot"]}]'
```

### Attaching servers to an analysis

Users attach servers in the troubleshooting dialog (see [Troubleshooting runs](/docs/ai/troubleshooting)):

1. `GET /api/ai/troubleshoot/options` returns, under `mcp_servers`, the enabled servers whose
   `agents` include `troubleshoot` (name, description and tool patterns). The list is empty when
   `ai.troubleshoot.allow_external_mcp` is off.
2. The dialog shows them under **External tools (MCP servers)**. Selecting a server lists its tools
   (`GET /api/ai/mcp/servers/{name}/tools`, already filtered by the server's `tools` allow-list);
   "Click a tool to leave it out."
3. When the analysis starts, AutoAir connects to each selected server for the duration of the run
   (status **Connecting tools…**) and gives the agent the allowed, selected tools, at most
   `max_tools_per_server` (40) per server. If a server has more, a note says
   `<server>: only the first N tools were attached`.

| Situation | Result |
|---|---|
| Server unreachable within `connect_timeout_seconds` (15), or listing its tools fails | The analysis continues without it; a status note reads `MCP server '<name>' unavailable: <error>`. |
| A request names a server that is disabled or not allowed for the agent | That server is reported as unavailable in the same way. |
| A request attaches servers while `ai.troubleshoot.allow_external_mcp` is off | HTTP 403 `external MCP servers are not allowed`. |

Tool results are truncated to `max_output_chars` (12000), scrubbed by the guardrails and wrapped as
untrusted data. Each call is bounded by the server's `timeout_seconds`. TLS options come from the
outbound TLS configuration (client name `mcp`).

**Server annotations are not trusted.** A server's own `readOnlyHint` is a claim of that server: a
compromised or malicious server could mark a destructive tool read-only to skip the approval. So an
external tool counts as read-only only when an administrator lists it in the server's
`read_only_tools`; every other call waits for the user's [approval](/docs/ai/tool-approval).
**Test connection** in the **MCP servers** tab shows which tools are read-only (`read_only`) and,
for information, which ones the server marks read-only (`hinted_read_only`). Upgrading from 0.2.2:
tools that ran without approval because the server marked them `readOnlyHint: true` now wait until
you add them to `read_only_tools`.

### Which agents attach external tools

| Agent (`agents` value) | Admin panel label | How servers are attached | Which tools |
|---|---|---|---|
| `troubleshoot` | **Troubleshooting** | The servers the user selects in the troubleshooting dialog (`ai.troubleshoot.allow_external_mcp`) | The server's `tools` allow-list, minus the tools the user leaves out |
| `developer` | **Developer** | Every chat turn of the developer assistant (`ai.assistants.developer.allow_external_mcp`) | The server's `tools` allow-list, then the caller's role in `ai.guardrails.tool_allowlist` and `ai.assistants.developer.tools` |
| `admin` | **Admin** | Every chat turn of the admin assistant (`ai.assistants.admin.allow_external_mcp`) | The server's `tools` allow-list, then `ai.assistants.admin.tools`, and only tools listed in the server's `read_only_tools` |

### Chat assistants

The developer and admin chat assistants ([Ask AI](/docs/ai/assistants)) connect to the servers
scoped to them at the start of each turn and disconnect after the answer. The tools are named
`mcp__<server>__<tool>`, like in troubleshooting.

- `GET /api/ai/assistant/info` lists, under `developer.mcp_servers` and `admin.mcp_servers`, the
  servers each assistant attaches for the caller (empty for the admin assistant without `ai:admin`
  or `admin:panel`, and when the profile's `allow_external_mcp` is off).
- A turn attaches all of them by default. `POST /api/ai/assistant/chat/stream` takes an optional
  `mcp` list (`[{"server": "grafana", "tools": ["query_metrics"]}]`) to choose servers and tools;
  `[]` attaches none. Naming a server that is not scoped to the assistant reports it as unavailable.
  Sending `mcp` while the profile's `allow_external_mcp` is off answers HTTP 403
  `external MCP servers are not allowed`.
- **IAM.** The developer assistant applies the caller's role allow-list
  (`ai.guardrails.tool_allowlist`) to the external tools as to its own: the default `viewer` list has
  no `mcp__*` pattern, so viewers get no external tools until you add, for example,
  `mcp__grafana__query_*`. The `editor` and `admin` roles allow every tool by default. Listing a
  server's tools for the admin assistant (`GET /api/ai/mcp/servers/{name}/tools?agent=admin`) needs
  `ai:admin` or `admin:panel`; for troubleshooting it needs `airflow:read`.
- **Read-only admin assistant.** The admin assistant's own tools are read-only, and so are the
  external tools it gets: only tools listed in the server's `read_only_tools` are attached (the
  server's `readOnlyHint` is not trusted). A status note reports the others:
  `<server>: N tool(s) not attached (not in the server's read_only_tools; the admin assistant only uses read-only tools)`.
- Unreachable servers do not fail the turn: a status note reads
  `MCP server '<name>' unavailable: <error>`.

Every server connected adds the connection time to the turn, so scope only the servers an assistant
needs to `developer` or `admin`.

## Related pages

- [`ai.mcp`](/docs/reference/configuration#aimcp), [`ai.mcp_client`](/docs/reference/configuration#aimcp_client), [`ai.service`](/docs/reference/configuration#aiservice)
- [Troubleshooting runs](/docs/ai/troubleshooting) and the chat assistants (where external MCP tools are used)
- [MCP integration for extensions](/docs/extending/mcp-integration)
- [Separate AI service](/docs/ai/ai-service)
- [AutoAir docs MCP](/docs/ai/docs-mcp): the documentation tools on their own (`autoair docs-mcp`)
