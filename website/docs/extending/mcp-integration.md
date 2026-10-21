---
title: MCP integration
description: Integrate AI agents and tools with AutoAir through its Model Context Protocol server - endpoints, authentication, safety switches and when to prefer it over the REST API.
sidebar_label: MCP integration
---

AutoAir exposes DAG authoring to AI agents through a **Model Context Protocol (MCP)** server: agents can search the operator catalog, read the DagSpec schema, validate and patch specs, generate code, review DAGs, read Airflow run state and, with explicit confirmation, save DAGs and trigger runs. Use MCP when the client is an agent (Claude Code, Claude Desktop, IDE agents, your own agent framework); use the [REST API](/docs/extending/rest-api) for scripts and CI.

The full tool, resource and prompt catalogue is on [MCP server](/docs/ai/mcp); the sections below are what an integrator needs.

## Endpoints

| Where | URL / command | Authentication |
|---|---|---|
| Plugin (inside Airflow) | `https://<airflow-host>/autoair/mcp/` (streamable HTTP) | Airflow's auth manager: `Authorization: Bearer <airflow token>` |
| Standalone server | `https://<autoair-host>/mcp/` | Bearer token verified against Airflow |
| Separate AI service | `/mcp` on `autoair ai-serve` (`ai.service.serve_mcp`), or proxied through the main server with `ai.service.proxy_mcp=true` | Main server auth, then the signed identity |
| Standalone MCP process | `autoair mcp-serve --http` (HTTP at `/mcp/`), or stdio: `autoair mcp-serve`, `autoair mcp`, `autoair-mcp` | HTTP: bearer token verified against Airflow; stdio: `AUTOAIR_AIRFLOW_TOKEN` |

The MCP endpoint needs the `mcp` extra (`pip install 'autoair[mcp]'`) and `features.mcp=true` (default). Without the extra the endpoint is not mounted and the server logs `MCP endpoint disabled`.

## Connect a client

Remote, through Airflow (Claude Code):

```bash
claude mcp add --transport http autoair https://airflow.example.com/autoair/mcp/ \
  --header "Authorization: Bearer $AIRFLOW_TOKEN"
```

Local, over stdio:

```bash
claude mcp add autoair -e AIRFLOW_HOME=$AIRFLOW_HOME -e AUTOAIR_AIRFLOW_TOKEN=$AIRFLOW_TOKEN -- autoair mcp
```

Any MCP client that supports streamable HTTP and custom headers can connect the same way. Obtain the token from Airflow as described in [REST API](/docs/extending/rest-api#authentication).

## Safety model

- **Same identity, same RBAC.** Over HTTP the endpoint sits behind the same authentication as the API, and the caller's credentials are forwarded to Airflow, so Airflow's permissions apply to every Airflow tool.
- **Writes need confirmation.** `save_dag_spec`, `delete_dag_spec` and `trigger_dag` only act with `confirm=true`; without it they describe what would happen.
- **Read-only switch.** `AUTOAIR_AI__MCP__ALLOW_WRITES=false` refuses those tools entirely.
- **No secrets out.** Connection and variable values and run `conf` are never returned.
- **Code execution is gated.** `import_python_dag` runs the file in an isolated subprocess without AutoAir's secrets and requires `allow_python_code`.
- **Route permissions do not apply.** MCP tools call AutoAir's services directly and do not go through the REST routes' AutoAir permission checks; restrict who can reach `/mcp`, and use `allow_writes=false` where agents should only read. See [IAM](/docs/admin/iam).

## Typical agent workflow

1. `get_dagspec_schema` and `search_knowledge` / `list_operators` to learn the format and the available operators.
2. Build or modify a spec; `apply_spec_patch` edits by node id.
3. `validate_dag_spec` until there are no errors; `generate_code` to show the Python.
4. `review_dag_spec` and `scan_dag_leaks` for a second opinion.
5. `save_dag_spec` with `confirm=true` to deploy through the normal pipeline (validation, policy, approvals, git pull requests).

## AutoAir as an MCP client

The reverse direction also exists: AutoAir's own AI agents can use external MCP servers (metrics, ticketing, runbooks) as tools, configured in `ai.mcp_client.servers` or the admin panel. See [AI troubleshooting](/docs/ai/troubleshooting).

## Related pages

- [MCP server](/docs/ai/mcp)
- [Spec format](/docs/extending/spec-format)
- [Separate AI service](/docs/deployment/ai-service)
