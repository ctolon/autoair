"""AutoAir IAM for MCP: every tool needs the same permission as its REST counterpart.

The HTTP mounts (inside the API, the AI service, ``autoair mcp-serve --http``) resolve the
caller's :class:`~autoair.iam.model.Principal` with the same strategies as the REST API and
store it on the request; this middleware checks it before a tool runs, and hides tools the
caller may not call from ``tools/list`` (agents then don't plan with them).

Over **stdio** there is no HTTP request: the server runs as a local process of the user who
started it (with their own filesystem and credentials), so no AutoAir principal applies.

Unknown tools are denied (``admin:config``) — a new tool must be classified here; a test
enforces that every registered tool is.

Git tokens typed per deploy (``storage.git.request_token``, header ``X-AutoAir-Git-Token``):
MCP tools never take a token as an argument (it would end up in the model's context, agent
transcripts and tool-call logs). With ``storage.git.mcp_request_token`` on, MCP over HTTP
reads it from the ``X-AutoAir-Git-Token`` header of the MCP request
(:mod:`autoair.mcp.git_token`); with it off such a header is refused. When the git target
*requires* a request token, :data:`GIT_WRITE_TOOLS` without one are refused over every
transport (stdio has no header) — dry runs (``confirm=false``) still answer.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from fastmcp.exceptions import ResourceError, ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

from autoair.iam import model as P
from autoair.secret_mask import masking

PRINCIPAL_ATTR = "autoair_principal"
_LOCAL = object()

#: tool name -> AutoAir permissions it needs (conditional extras are in ``_extra``).
TOOL_PERMISSIONS: Mapping[str, tuple[str, ...]] = {
    # catalog / authoring helpers (no side effects)
    "list_operators": (P.BUILDER_USE,),
    "get_operator_schema": (P.BUILDER_USE,),
    "get_dagspec_schema": (P.BUILDER_USE,),
    "list_node_kinds": (P.BUILDER_USE,),
    "list_notifiers": (P.BUILDER_USE,),
    "list_blueprints": (P.BUILDER_USE,),
    "search_knowledge": (P.BUILDER_USE,),
    "validate_dag_spec": (P.BUILDER_USE,),
    "apply_spec_patch": (P.BUILDER_USE,),
    "generate_code": (P.BUILDER_USE,),
    # deterministic rules (no LLM unless use_ai, which adds ai:use / features.ai: _extra)
    "review_dag_spec": (P.BUILDER_USE,),
    "scan_dag_leaks": (P.BUILDER_USE,),
    # stored specs
    "list_dags": (P.SPEC_READ,),
    "get_dag_spec": (P.SPEC_READ,),
    "save_dag_spec": (P.SPEC_WRITE, P.DEPLOY),
    # what save_dag_spec would write (REST: POST /specs/{dag_id}/source-preview); preserve
    # previews import the DAG file, which the plan refuses without code:execute
    "preview_dag_source": (P.SPEC_WRITE,),
    "get_dag_code": (P.SPEC_READ,),
    "save_raw_dag_code": (P.SPEC_WRITE, P.DEPLOY, P.CODE_RAW),
    "delete_dag_spec": (P.SPEC_DELETE,),
    "import_python_dag": (P.SPEC_WRITE, P.CODE_EXECUTE),
    # operator templates (the REST API's /templates routes)
    "list_operator_templates": (P.BUILDER_USE,),
    "get_operator_template": (P.BUILDER_USE,),
    "save_operator_template": (P.TEMPLATE_WRITE,),
    "delete_operator_template": (P.TEMPLATE_WRITE,),
    # Airflow (proxied with the caller's token: Airflow's RBAC applies too)
    "list_connections": (P.AIRFLOW_READ,),
    "list_variables": (P.AIRFLOW_READ,),
    "get_dag_status": (P.AIRFLOW_READ,),
    "list_dag_runs": (P.AIRFLOW_READ,),
    "get_task_log": (P.AIRFLOW_READ,),
    "trigger_dag": (P.AIRFLOW_READ, P.RUN_TRIGGER),
    # AI assistance, research, memories
    "optimize_dag_parameters": (P.AI_USE, P.AIRFLOW_READ),
    "generate_dag_from_prompt": (P.AI_USE,),
    "fix_dag_spec": (P.AI_USE,),
    "write_code": (P.AI_USE,),
    "search_installed_docs": (P.AI_USE,),
    "read_source": (P.AI_USE,),
    "list_installed_packages": (P.AI_USE,),
    "get_package_info": (P.AI_USE,),
    "fetch_doc": (P.AI_USE,),
    "list_memories": (P.AI_USE,),
    "remember": (P.AI_USE,),
    "forget": (P.AI_USE,),
    # documentation knowledge base (autoair.kb): read-only, works without an LLM
    "kb_search": (P.AI_USE,),
    "kb_sources": (P.AI_USE,),
    "kb_status": (P.AI_USE,),
    # AutoAir's own documentation (autoair.docs): the same pages are public on the website,
    # so no AutoAir permission; over HTTP the caller must still be identified.
    "search_autoair_docs": (),
    "read_autoair_doc": (),
    "list_autoair_docs": (),
}
_UNKNOWN = (P.ADMIN_CONFIG,)

#: Tools that write to the deploy target (refused when git writes need a request token).
GIT_WRITE_TOOLS = frozenset({"save_dag_spec", "save_raw_dag_code", "delete_dag_spec"})

#: Tools whose rules run without AI; their ``use_ai=true`` LLM pass needs AI.
RULE_TOOLS = frozenset({"review_dag_spec", "scan_dag_leaks"})

#: Feature switches a raw code DAG needs (``DagSpec.raw_code``: arbitrary Python).
RAW_CODE_FEATURES = ("python_code", "code_editor")

#: tool name -> feature switches (``AUTOAIR_FEATURES__*``) it needs. Applies over every
#: transport, stdio included: a disabled feature is off for agents too.
_AI = ("ai",)
TOOL_FEATURES: Mapping[str, tuple[str, ...]] = {
    "save_dag_spec": ("deploy",),
    "preview_dag_source": ("deploy",),
    "save_raw_dag_code": ("deploy", *RAW_CODE_FEATURES),
    "delete_dag_spec": ("deploy",),
    "import_python_dag": ("imports", "python_code"),
    "list_blueprints": ("blueprints",),
    **{
        name: ("templates",)
        for name in (
            "list_operator_templates",
            "get_operator_template",
            "save_operator_template",
            "delete_operator_template",
        )
    },
    # Tools that call the Airflow server: off with features.airflow (``autoair local``).
    "trigger_dag": ("runs", "airflow"),
    **{
        name: ("airflow",)
        for name in (
            "list_connections",
            "list_variables",
            "get_dag_status",
            "list_dag_runs",
            "get_task_log",
        )
    },
    **{
        name: _AI
        for name in (
            "optimize_dag_parameters",
            "generate_dag_from_prompt",
            "fix_dag_spec",
            "write_code",
            "search_installed_docs",
            "read_source",
            "list_installed_packages",
            "get_package_info",
            "fetch_doc",
            "list_memories",
            "remember",
            "forget",
        )
    },
    **{name: ("kb",) for name in ("kb_search", "kb_sources", "kb_status")},
    **{
        name: ("autoair_docs",)
        for name in ("search_autoair_docs", "read_autoair_doc", "list_autoair_docs")
    },
}


def _principal() -> Any:
    """The HTTP caller's principal, :data:`_LOCAL` over stdio, ``None`` if unresolved."""
    try:
        from fastmcp.server.dependencies import get_http_request

        request = get_http_request()
    except RuntimeError:
        return _LOCAL
    return getattr(request.state, PRINCIPAL_ATTR, None)


def _raw_spec(tool: str, args: Mapping[str, Any]) -> bool:
    """``save_dag_spec`` called with a raw code DAG (``raw_code`` set)."""
    spec = args.get("spec")
    return (
        tool == "save_dag_spec" and isinstance(spec, Mapping) and spec.get("raw_code") is not None
    )


def _extra(tool: str, arguments: Mapping[str, Any] | None) -> tuple[str, ...]:
    args = arguments or {}
    if tool == "validate_dag_spec" and args.get("import_check"):
        return (P.CODE_EXECUTE,)  # imports (executes) the generated DAG
    if _raw_spec(tool, args):
        return (P.CODE_RAW,)  # arbitrary Python, like save_raw_dag_code
    if tool in RULE_TOOLS and args.get("use_ai"):
        return (P.AI_USE,)
    return ()


def required_features(tool: str, arguments: Mapping[str, Any] | None = None) -> tuple[str, ...]:
    args = arguments or {}
    extra = RAW_CODE_FEATURES if _raw_spec(tool, args) else ()
    if tool in RULE_TOOLS and args.get("use_ai"):
        extra = (*extra, *_AI)
    return tuple(TOOL_FEATURES.get(tool, ())) + extra


def required_permissions(tool: str, arguments: Mapping[str, Any] | None = None) -> tuple[str, ...]:
    return TOOL_PERMISSIONS.get(tool, _UNKNOWN) + _extra(tool, arguments)


class IamMiddleware(Middleware):
    """Checks the caller's AutoAir permissions for tools and resources."""

    def __init__(
        self,
        *,
        require_approval: Callable[[], bool] = lambda: False,
        feature: Callable[[str], bool] = lambda _name: True,
        git_token_required: Callable[[], bool] = lambda: False,
        git_token: Callable[[], str | None] = lambda: None,
        git_token_allowed: Callable[[], bool] = lambda: False,
    ) -> None:
        self.feature = feature
        # storage.git.request_token=required: every git write needs a token typed by a
        # user in the UI; MCP never carries one, so its writes are refused.
        self.git_token_required = git_token_required
        # The X-AutoAir-Git-Token header of the MCP request and storage.git.mcp_request_token.
        self.git_token = git_token
        self.git_token_allowed = git_token_allowed
        # With four-eyes approval, going live needs a reviewed version: MCP saves are refused
        # (agents propose through the UI/PR flow instead). Read lazily (settings may change).
        self.require_approval = require_approval

    async def on_call_tool(
        self, context: MiddlewareContext[Any], call_next: CallNext[Any, Any]
    ) -> Any:
        name = str(context.message.name)
        off = [f for f in required_features(name, context.message.arguments) if not self.feature(f)]
        if off:
            raise ToolError(f"{name} is disabled (feature {off[0]!r} is off)")
        arguments = context.message.arguments or {}
        token = self.git_token() if name in GIT_WRITE_TOOLS else None
        if token is not None and not self.git_token_allowed():
            raise ToolError(
                "X-AutoAir-Git-Token is not accepted by MCP tools here "
                "(storage.git.mcp_request_token is false)"
            )
        if (
            name in GIT_WRITE_TOOLS
            and arguments.get("confirm")
            and token is None
            and self.git_token_required()
        ):
            how = (
                "send it in the X-AutoAir-Git-Token header of the MCP request"
                if self.git_token_allowed()
                else "MCP tools take none here (storage.git.mcp_request_token); deploy from "
                "the AutoAir UI"
            )
            raise ToolError(
                f"{name} cannot write: this git deploy target needs the user's own git token "
                f"for every write (storage.git.request_token=required); {how}"
            )
        principal = _principal()
        if principal is not _LOCAL:
            wanted = required_permissions(name, context.message.arguments)
            if principal is None:
                raise ToolError("AutoAir could not identify the caller")
            missing = [p for p in wanted if not principal.has(p)]
            if missing:
                raise ToolError(f"missing AutoAir permission {missing[0]!r} for {name}")
            if name in ("save_dag_spec", "save_raw_dag_code") and self.require_approval():
                raise ToolError(
                    "deploys need an approved version (iam.require_approval): "
                    "propose the change in the AutoAir UI for review"
                )
        with masking(token):  # logs of the tool call never show the token
            return await call_next(context)

    async def on_list_tools(
        self, context: MiddlewareContext[Any], call_next: CallNext[Any, Any]
    ) -> Any:
        tools = [
            t
            for t in await call_next(context)
            if all(self.feature(f) for f in TOOL_FEATURES.get(t.name, ()))
        ]
        principal = _principal()
        if principal is _LOCAL:
            return tools
        if principal is None:
            return []
        return [t for t in tools if all(principal.has(p) for p in required_permissions(t.name))]

    async def on_read_resource(
        self, context: MiddlewareContext[Any], call_next: CallNext[Any, Any]
    ) -> Any:
        principal = _principal()
        if principal is not _LOCAL:
            uri = str(context.message.uri)
            wanted = (P.SPEC_READ,) if "dag" in uri or "template" in uri else (P.BUILDER_USE,)
            if principal is None or not all(principal.has(p) for p in wanted):
                raise ResourceError(f"missing AutoAir permission for {uri}")
        return await call_next(context)


__all__ = [
    "GIT_WRITE_TOOLS",
    "RAW_CODE_FEATURES",
    "TOOL_FEATURES",
    "TOOL_PERMISSIONS",
    "IamMiddleware",
    "required_features",
    "required_permissions",
]
