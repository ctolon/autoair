---
title: AutoAir docs MCP
description: Give coding agents such as Claude Code, Cursor, OpenCode and Codex CLI search and read access to the AutoAir documentation over MCP, offline and matched to the installed version.
sidebar_label: Docs MCP
---

AutoAir ships its own documentation (this site) inside the Python package and serves it over the
[Model Context Protocol](https://modelcontextprotocol.io), the way LangChain's docs MCP server serves
LangChain's documentation. A coding agent can then look up how to install, configure, deploy,
secure or extend AutoAir while it works, and cite the page it used.

- The pages are those of the **installed AutoAir version**, packaged when the wheel is built. Nothing
  is fetched from the network, so it works offline and air-gapped.
- It needs no Airflow, no AI extra, no knowledge-base index and no configuration.
- `autoair docs-mcp` runs a server with only these tools. They are also part of AutoAir's main
  [MCP server](/docs/ai/mcp), so agents already connected to AutoAir have them as well.

## Tools

| Tool | What it returns |
|---|---|
| `search_autoair_docs` | Passages that match `query` (keyword search, BM25), each with the page title, heading, page path and the URL of the heading (`https://ctolon.github.io/autoair/docs/...#anchor`). `limit` 1-20 (default 5); `section` keeps one top-level section such as `ai`, `admin` or `installation`. At most two passages per page, 1,500 characters each. |
| `read_autoair_doc` | One page as Markdown. `page` is a page path (`ai/mcp`), a site route (`/docs/ai/mcp`) or a URL of the site, optionally with `#anchor` to start at that heading. `max_chars` 1,000-100,000 (default 20,000); a longer page returns `next_offset`, which you pass as `offset` to read on. Only packaged pages can be read. |
| `list_autoair_docs` | Every page with its title, one-line description, path and URL, optionally for one `section`. |

The configuration reference is included, so an agent can look up any `AUTOAIR_*` setting.

## Requirements

- AutoAir 0.2.0 or later, with the `mcp` extra (FastMCP): `autoair[mcp]`. Without the extra,
  `autoair docs-mcp` exits with a message that names it.
- For the commands below, [uv](https://docs.astral.sh/uv/) on `PATH`: `uvx` installs AutoAir in a
  cached environment of its own and runs it. Installing with `pip install 'autoair[mcp]'` and
  running `autoair docs-mcp` works the same way.

`autoair[mcp]` cannot be installed next to Airflow 3.1 (see
[Compatibility](/docs/installation/compatibility)). The docs server does not need Airflow, so run it
from its own environment with `uvx`, as shown here.

## Run the server

```bash
uvx --from 'autoair[mcp]' autoair docs-mcp                                   # stdio (default)
uvx --from 'autoair[mcp]' autoair docs-mcp --http --host 0.0.0.0 --port 8767  # streamable HTTP
```

| Option | Default | Meaning |
|---|---|---|
| `--http` | off | Serve streamable HTTP instead of stdio. |
| `--host` | `127.0.0.1` | HTTP bind address. |
| `--port` | `8767` | HTTP port. |
| `--path` | `/mcp` | HTTP endpoint path: clients use `http://HOST:PORT/mcp`. |

`autoair docs-mcp --help` prints the options and short setup lines.

## Use with your coding agent

Each agent below has two variants: **local** starts the server over stdio on the developer's machine;
**shared** connects to one HTTP instance that a team runs (see [Shared HTTP instance](#shared-http-instance)).
Replace `http://docs-host:8767/mcp` with your instance's URL.

### Claude Code

```bash
# local (stdio); add --scope user for every project, --scope project to share it in .mcp.json
claude mcp add autoair-docs -- uvx --from 'autoair[mcp]' autoair docs-mcp

# shared (HTTP)
claude mcp add --transport http autoair-docs http://docs-host:8767/mcp
```

The `--` separates Claude Code's options from the server command. `--scope project` writes
`.mcp.json` in the project root:

```json
{
  "mcpServers": {
    "autoair-docs": {
      "type": "stdio",
      "command": "uvx",
      "args": ["--from", "autoair[mcp]", "autoair", "docs-mcp"]
    }
  }
}
```

Verify: `claude mcp list` shows `autoair-docs ... ✔ Connected` (project-scoped servers are approved
the first time `claude` starts); inside Claude Code, `/mcp` shows the server and its tools.

### Cursor

Add the server to `.cursor/mcp.json` in the project, or to `~/.cursor/mcp.json` for every project:

```json
{
  "mcpServers": {
    "autoair-docs": {
      "type": "stdio",
      "command": "uvx",
      "args": ["--from", "autoair[mcp]", "autoair", "docs-mcp"]
    }
  }
}
```

Shared instance:

```json
{
  "mcpServers": {
    "autoair-docs": {
      "url": "http://docs-host:8767/mcp"
    }
  }
}
```

Verify: the server is listed and enabled in Cursor's MCP settings; the **MCP Logs** output channel
(Output panel) shows its start-up or the error.

### OpenCode

Add the server under `mcp` in `opencode.json` in the project root, or in
`~/.config/opencode/opencode.json`:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "autoair-docs": {
      "type": "local",
      "command": ["uvx", "--from", "autoair[mcp]", "autoair", "docs-mcp"],
      "enabled": true
    }
  }
}
```

Shared instance:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "autoair-docs": {
      "type": "remote",
      "url": "http://docs-host:8767/mcp",
      "enabled": true
    }
  }
}
```

Verify: `opencode mcp list` shows `autoair-docs` as connected.

### OpenAI Codex CLI

```bash
# local (stdio)
codex mcp add autoair-docs -- uvx --from 'autoair[mcp]' autoair docs-mcp

# shared (streamable HTTP)
codex mcp add autoair-docs --url http://docs-host:8767/mcp
```

Both write `~/.codex/config.toml` (a trusted project can use `.codex/config.toml` instead):

```toml
[mcp_servers.autoair-docs]
command = "uvx"
args = ["--from", "autoair[mcp]", "autoair", "docs-mcp"]

# or, for a shared instance:
# [mcp_servers.autoair-docs]
# url = "http://docs-host:8767/mcp"
```

Verify: `codex mcp list` lists `autoair-docs` as enabled; in the Codex TUI, `/mcp` shows the active
servers and their tools.

### Other MCP clients

Most clients accept the `mcpServers` JSON format:

```json
{
  "mcpServers": {
    "autoair-docs": {
      "command": "uvx",
      "args": ["--from", "autoair[mcp]", "autoair", "docs-mcp"]
    }
  }
}
```

For a client that connects to remote servers, give it the streamable HTTP URL
`http://docs-host:8767/mcp`.

## Shared HTTP instance

```bash
uvx --from 'autoair[mcp]' autoair docs-mcp --http --host 0.0.0.0 --port 8767
```

`autoair docs-mcp --http` has **no authentication, by design**. It serves only AutoAir's own
public documentation, packaged in the wheel: it has no Airflow connection, reads no settings,
state or secrets, and makes no network requests, so there is nothing a caller could reach
through it beyond the pages on this site. The same three tools on AutoAir's main MCP server need
no AutoAir permission for the same reason.

The server answers anyone who can reach it, so it binds to `127.0.0.1` by default:

- Keep `--host` on loopback (`127.0.0.1`, the default) for a single machine.
- For a shared instance, bind it to an internal address and put your own reverse proxy in front of
  it for authentication, TLS and rate limits, in particular when it is reachable beyond a trusted
  network. It is stateless (`stateless_http`), so several replicas can run behind a load
balancer without sticky sessions.

To pin the documentation to the AutoAir version your servers run, pin the package:
`uvx --from 'autoair[mcp]==0.2.0' autoair docs-mcp`.

## In AutoAir's MCP server

The three tools are registered on every AutoAir MCP endpoint: `autoair mcp` (stdio),
`autoair mcp-serve --http` and `/mcp` of the plugin, the standalone server and the AI service.
There they need no AutoAir permission, since the documentation is public, but the caller is still
authenticated like for every other tool. The feature flag `features.autoair_docs`
(`AUTOAIR_FEATURES__AUTOAIR_DOCS`, on by default) switches them off; the flag does not apply to
`autoair docs-mcp`, which has no settings.

The [knowledge base](/docs/ai/knowledge-base) indexes the same pages through its default `autoair`
source, so `kb_search` and AutoAir's own assistants can cite AutoAir pages next to the Airflow
documentation.

## Troubleshooting

| Problem | Fix |
|---|---|
| The client reports that `uvx` is not found | Desktop apps and IDEs often start without your shell's `PATH`. Use the full path of `uvx` (`which uvx`, for example `~/.local/bin/uvx`) as `command`, or install AutoAir with `pip install 'autoair[mcp]'` and use the full path of the `autoair` executable with the argument `docs-mcp`. |
| The first start times out | The first `uvx` run downloads and installs AutoAir and its dependencies, which can take a minute. Run `uvx --from 'autoair[mcp]' autoair docs-mcp --help` once in a terminal to fill uv's cache, then restart the client. OpenCode waits 5 seconds by default; raise the server's `timeout` (milliseconds) in `opencode.json` if needed. |
| `autoair docs-mcp needs the MCP extra` | AutoAir was installed without `mcp`. Use `--from 'autoair[mcp]'` with `uvx`, or `pip install 'autoair[mcp]'`. |
| `No such command 'docs-mcp'` | The installed AutoAir is older than 0.2.0. Upgrade, or clear uv's cached version with `uvx --refresh --from 'autoair[mcp]' autoair docs-mcp --help`. |
| Offline or air-gapped machine | `uvx` needs a package index. Install the wheel from your internal index or wheel files (`pip install 'autoair[mcp]'`) and point the client at `autoair` with the argument `docs-mcp`. The documentation itself is inside the wheel and needs no network. |

## Related pages

- [MCP server and client](/docs/ai/mcp): AutoAir's full MCP server for authoring and deploying DAGs.
- [Documentation knowledge base](/docs/ai/knowledge-base): Airflow and AutoAir documentation for AutoAir's own agents.
- The whole site as Markdown for language models: [llms.txt](pathname:///llms.txt) and [llms-full.txt](pathname:///llms-full.txt).
