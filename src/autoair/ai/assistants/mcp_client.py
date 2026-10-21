"""External MCP servers as tool providers of the agents (fastmcp ``Client``).

Servers come from the configuration (``ai.mcp_client.servers``: streamable HTTP, SSE or a
local stdio command) and - unless ``ai.mcp_client.database_servers`` is off - from the
admin panel (stored with the state; HTTP/SSE only, so the database can never make the
server run a command). Each server has a tool allow-list, secret headers (``SecretStr`` in
the configuration, Fernet-encrypted at rest when stored), a timeout and the agents that
may attach it. Users pick which allowed servers/tools to attach before an analysis.

Tool results are untrusted: they are truncated, scrubbed by the guardrails like every
tool output and wrapped as data for the model.
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import json
import re
import threading
from collections.abc import AsyncIterator, Sequence
from typing import Any, Literal

from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from autoair.ai.config import McpAgent, McpServerConfig, _default_agents, ai_settings_of
from autoair.ai.tools import AgentTool
from autoair.persistence.documents import DocumentStore

MASK = "***"
_NAME_RE = re.compile(r"[^A-Za-z0-9_-]")


class McpError(RuntimeError):
    pass


class McpServerView(BaseModel):
    """A server as the API shows it (secret values never leave the server)."""

    name: str
    description: str = ""
    transport: Literal["http", "sse", "stdio"]
    url: str | None = None
    command: str | None = None
    tools: list[str] = Field(default_factory=list)
    headers: list[str] = Field(default_factory=list, description="Names of the set headers.")
    timeout_seconds: float = 30
    enabled: bool = True
    agents: list[McpAgent] = Field(default_factory=list)
    source: Literal["settings", "database"]
    updated_by: str | None = None
    updated_at: str | None = None


class McpServerInput(BaseModel):
    """Create/replace a server in the admin panel (HTTP/SSE only)."""

    model_config = ConfigDict(extra="forbid")

    description: str = Field(default="", max_length=500)
    transport: Literal["http", "sse"] = "http"
    url: str = Field(pattern=r"^https?://", max_length=2_000)
    tools: list[str] = Field(default_factory=lambda: ["*"], max_length=200)
    headers: dict[str, str | None] = Field(
        default_factory=dict,
        description="Header values; null or '***' keeps the stored value, '' removes it.",
    )
    timeout_seconds: float = Field(default=30, gt=0, le=600)
    enabled: bool = True
    agents: list[McpAgent] = Field(default_factory=_default_agents)


class McpToolInfo(BaseModel):
    name: str
    description: str = ""
    allowed: bool = True


class McpSelection(BaseModel):
    server: str = Field(max_length=40)
    tools: list[str] | None = Field(
        default=None, description="Tools to attach (default: every allowed tool)."
    )


# --------------------------------------------------------------------------- store


class McpServerStore:
    """Admin-defined servers: one document per server, header values Fernet-encrypted."""

    def __init__(self, documents: DocumentStore, key: bytes) -> None:
        self.documents = documents
        self._fernet = Fernet(key)

    def _decode(self, raw: bytes) -> dict[str, Any] | None:
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def raw(self, name: str) -> dict[str, Any] | None:
        doc = self.documents.get(name)
        return self._decode(doc.data) if doc is not None else None

    def all_raw(self) -> list[dict[str, Any]]:
        out = []
        keys = self.documents.keys()
        for key in keys:
            data = self.raw(key)
            if data is not None:
                out.append(data)
        return sorted(out, key=lambda d: str(d.get("name")))

    def config(self, data: dict[str, Any]) -> McpServerConfig | None:
        headers: dict[str, SecretStr] = {}
        for name, token in (data.get("headers") or {}).items():
            try:
                headers[name] = SecretStr(self._fernet.decrypt(token.encode()).decode())
            except (InvalidToken, AttributeError, ValueError):
                continue
        try:
            return McpServerConfig(
                name=data["name"],
                description=data.get("description", ""),
                transport=data.get("transport", "http"),
                url=data.get("url"),
                tools=data.get("tools", ["*"]),
                headers=headers,
                timeout_seconds=data.get("timeout_seconds", 30),
                enabled=data.get("enabled", True),
                agents=data.get("agents", ["troubleshoot"]),
            )
        except (ValidationError, KeyError):
            return None

    def put(self, name: str, body: McpServerInput, user: str | None) -> dict[str, Any]:
        from autoair.ai.assistants.threads import now_iso

        McpServerConfig(name=name, transport=body.transport, url=body.url)  # validates
        current = self.raw(name) or {}
        headers: dict[str, str] = dict(current.get("headers") or {})
        for key, value in body.headers.items():
            if not re.fullmatch(r"[A-Za-z0-9-]{1,100}", key):
                raise ValueError(f"invalid header name {key!r}")
            if value is None or value == MASK:
                continue
            if value == "":
                headers.pop(key, None)
            else:
                headers[key] = self._fernet.encrypt(value.encode()).decode()
        data = {
            "name": name,
            "description": body.description,
            "transport": body.transport,
            "url": body.url,
            "tools": body.tools,
            "headers": headers,
            "timeout_seconds": body.timeout_seconds,
            "enabled": body.enabled,
            "agents": body.agents,
            "updated_by": user,
            "updated_at": now_iso(),
        }
        self.documents.put(name, json.dumps(data).encode())
        return data

    def delete(self, name: str) -> bool:
        return self.documents.delete(name)


_STORES: dict[str, McpServerStore] = {}
_LOCK = threading.Lock()


def server_store_for(services: Any) -> McpServerStore:
    from autoair.ai.config import ai_settings_of
    from autoair.ai.user_llm import encryption_key_for
    from autoair.persistence import document_store, is_sql

    settings = services.settings
    url = ai_settings_of(settings).mcp_client.store_url or str(
        settings.state_dir / "ai-mcp-servers"
    )
    cache = f"{'sql' if is_sql(settings) else 'file'}:{url}"
    with _LOCK:
        store = _STORES.get(cache)
        if store is None:
            store = _STORES[cache] = McpServerStore(
                document_store(settings, "ai_mcp_servers", file_url=url),
                encryption_key_for(services),
            )
        return store


def _view(
    cfg: McpServerConfig, source: Literal["settings", "database"], **kw: Any
) -> McpServerView:
    return McpServerView(
        name=cfg.name,
        description=cfg.description,
        transport=cfg.transport,
        url=cfg.url,
        command=cfg.command,
        tools=list(cfg.tools),
        headers=sorted(cfg.headers),
        timeout_seconds=cfg.timeout_seconds,
        enabled=cfg.enabled,
        agents=list(cfg.agents),
        source=source,
        **kw,
    )


def server_views(services: Any) -> list[McpServerView]:
    """Every server (configuration first; a stored one never shadows a configured one)."""
    mc = ai_settings_of(services.settings).mcp_client
    views = [_view(s, "settings") for s in mc.servers]
    names = {v.name for v in views}
    store = server_store_for(services)
    for data in store.all_raw():
        cfg = store.config(data)
        if cfg is None or cfg.name in names:
            continue
        views.append(
            _view(
                cfg,
                "database",
                updated_by=data.get("updated_by"),
                updated_at=data.get("updated_at"),
            )
        )
    return views


def servers_for(services: Any, agent: McpAgent) -> list[McpServerConfig]:
    """Enabled servers ``agent`` may attach."""
    mc = ai_settings_of(services.settings).mcp_client
    out = [s for s in mc.servers if s.enabled and agent in s.agents]
    names = {s.name for s in mc.servers}
    if mc.database_servers:
        store = server_store_for(services)
        for data in store.all_raw():
            cfg = store.config(data)
            if cfg is None or cfg.name in names or cfg.transport == "stdio":
                continue
            if cfg.enabled and agent in cfg.agents:
                out.append(cfg)
    return out


def server_by_name(services: Any, name: str, agent: McpAgent | None = None) -> McpServerConfig:
    mc = ai_settings_of(services.settings).mcp_client
    for s in mc.servers:
        if s.name == name and (agent is None or (s.enabled and agent in s.agents)):
            return s
    if mc.database_servers or agent is None:
        store = server_store_for(services)
        data = store.raw(name) if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,39}", name) else None
        cfg = store.config(data) if data else None
        if cfg is not None and (agent is None or (cfg.enabled and agent in cfg.agents)):
            return cfg
    raise McpError(f"MCP server {name!r} is not available")


# --------------------------------------------------------------------------- client


def build_transport(server: McpServerConfig) -> Any:
    """The fastmcp transport of ``server`` (tests replace this function)."""
    from fastmcp.client.transports import SSETransport, StdioTransport, StreamableHttpTransport

    from autoair.net import tls

    headers = {k: v.get_secret_value() for k, v in server.headers.items()}
    verify = tls.ssl_context("mcp")
    if server.transport == "stdio":
        env = {k: v.get_secret_value() for k, v in server.env.items()}
        return StdioTransport(server.command or "", list(server.args), env=env or None)
    if server.transport == "sse":
        return SSETransport(server.url or "", headers=headers, verify=verify)
    return StreamableHttpTransport(server.url or "", headers=headers, verify=verify)


def tool_allowed(server: McpServerConfig, tool: str) -> bool:
    return any(fnmatch.fnmatchcase(tool, p) for p in server.tools)


def agent_tool_name(server: str, tool: str) -> str:
    return f"mcp__{_NAME_RE.sub('_', server)}__{_NAME_RE.sub('_', tool)}"[:64]


def _client(services: Any, server: McpServerConfig) -> Any:
    from fastmcp import Client

    timeout = ai_settings_of(services.settings).mcp_client.connect_timeout_seconds
    return Client(build_transport(server), timeout=server.timeout_seconds, init_timeout=timeout)


async def list_server_tools(services: Any, server: McpServerConfig) -> list[McpToolInfo]:
    """Connect and list the server's tools (with whether the allow-list permits each)."""
    timeout = ai_settings_of(services.settings).mcp_client.connect_timeout_seconds

    async def fetch() -> list[Any]:
        async with _client(services, server) as client:
            return list(await client.list_tools())

    try:
        tools = await asyncio.wait_for(fetch(), timeout + 5)
    except Exception as exc:
        raise McpError(f"cannot reach MCP server {server.name!r}: {exc}") from exc
    return [
        McpToolInfo(
            name=t.name,
            description=(t.description or "")[:500],
            allowed=tool_allowed(server, t.name),
        )
        for t in tools
    ]


class _AnyArgs(BaseModel):
    model_config = ConfigDict(extra="allow")


def _result_text(result: Any, limit: int) -> str:
    parts: list[str] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text is not None:
            parts.append(str(text))
        else:
            parts.append(f"[{getattr(block, 'type', 'content')}]")
    text = "\n".join(parts)
    if not text and getattr(result, "structured_content", None) is not None:
        text = json.dumps(result.structured_content, default=str)
    if len(text) > limit:
        text = text[:limit] + f"... [truncated {len(text) - limit} characters]"
    return text


def read_only_tool(tool: Any) -> bool:
    """Whether the server marks ``tool`` read-only (MCP ``annotations.readOnlyHint``)."""
    annotations = getattr(tool, "annotations", None)
    if isinstance(annotations, dict):
        hint = annotations.get("readOnlyHint", annotations.get("read_only_hint"))
    else:  # MCP SDK v2 renamed readOnlyHint to read_only_hint
        hint = getattr(annotations, "read_only_hint", None)
        if hint is None and not hasattr(annotations, "read_only_hint"):
            hint = getattr(annotations, "readOnlyHint", None)
    return hint is True


def default_selections(services: Any, agent: McpAgent) -> list[McpSelection]:
    """Every enabled server ``agent`` may attach, with all its allowed tools."""
    return [McpSelection(server=s.name) for s in servers_for(services, agent)]


@contextlib.asynccontextmanager
async def attach_mcp_tools(
    services: Any,
    selections: Sequence[McpSelection],
    agent: McpAgent,
    *,
    read_only: bool = False,
) -> AsyncIterator[tuple[list[AgentTool], list[str]]]:
    """Connect to the selected servers for the duration of a run.

    Only servers scoped to ``agent`` and tools of their allow-list are attached; with
    ``read_only`` (the admin assistant) only tools the server marks read-only.
    Yields (agent tools, notes); a server that cannot be reached is reported in the notes
    instead of failing the run."""
    mc = ai_settings_of(services.settings).mcp_client
    tools: list[AgentTool] = []
    notes: list[str] = []
    async with contextlib.AsyncExitStack() as stack:
        for selection in selections:
            try:
                server = server_by_name(services, selection.server, agent)
                # Entered in this task (anyio scopes must be exited where they were
                # entered): the client's init_timeout / request timeout bound the wait.
                client = _client(services, server)
                await stack.enter_async_context(client)
                listed = await client.list_tools()
            except Exception as exc:
                notes.append(f"MCP server {selection.server!r} unavailable: {exc}")
                continue
            wanted = set(selection.tools) if selection.tools is not None else None
            count = 0
            writable = 0
            for t in listed:
                if not tool_allowed(server, t.name):
                    continue
                if wanted is not None and t.name not in wanted:
                    continue
                if read_only and not read_only_tool(t):
                    writable += 1
                    continue
                if count >= mc.max_tools_per_server:
                    notes.append(f"{server.name}: only the first {count} tools were attached")
                    break
                count += 1
                tools.append(_wrap(client, server, t, mc.max_output_chars))
            if writable:
                notes.append(
                    f"{server.name}: {writable} tool(s) not attached (not marked read-only; "
                    f"the {agent} assistant only uses read-only tools)"
                )
        yield tools, notes


def _wrap(client: Any, server: McpServerConfig, tool: Any, limit: int) -> AgentTool:
    remote = str(tool.name)

    async def handler(args: _AnyArgs) -> str:
        result = await client.call_tool(
            remote,
            args.model_dump(),
            timeout=server.timeout_seconds,
            raise_on_error=False,
        )
        text = _result_text(result, limit)
        if getattr(result, "is_error", False):
            raise McpError(text or "the MCP tool failed")
        return text

    schema = dict(getattr(tool, "input_schema", None) or {"type": "object", "properties": {}})
    description = f"[MCP {server.name}] {(tool.description or remote)[:900]}"
    return AgentTool(
        agent_tool_name(server.name, remote), description, _AnyArgs, handler, schema=schema
    )


__all__ = [
    "McpError",
    "McpSelection",
    "McpServerInput",
    "McpServerStore",
    "McpServerView",
    "McpToolInfo",
    "agent_tool_name",
    "attach_mcp_tools",
    "build_transport",
    "default_selections",
    "list_server_tools",
    "read_only_tool",
    "server_by_name",
    "server_store_for",
    "server_views",
    "servers_for",
]
