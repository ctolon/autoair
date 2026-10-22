"""AutoAir MCP server (FastMCP): lets MCP clients (Claude Desktop / Code, IDE agents)
discover operators, author / validate / review / save DAG specs and talk to Airflow.

Run over stdio with ``autoair-mcp`` (or ``autoair-mcp --http --port 8765`` for a
standalone streamable-HTTP endpoint). Inside the AutoAir web app
:func:`autoair.mcp.http.create_mcp_asgi_app` mounts it behind AutoAir's auth.

Design for LLM clients:

* Typed Pydantic inputs/outputs (output schemas), precise descriptions that say when to
  use each tool, and MCP annotations (read-only / destructive / idempotent / open-world).
* Paging (``offset`` / ``next_offset``) and size limits so results fit context windows.
* ``apply_spec_patch`` edits a spec with JSON-patch operations addressed by node id - far
  fewer tokens than re-sending a whole document.
* Destructive tools (``save_dag_spec``, ``delete_dag_spec``, ``trigger_dag``) act only with
  ``confirm=true`` (otherwise they dry-run and say what would happen) and are refused
  entirely when ``AUTOAIR_AI__MCP__ALLOW_WRITES=false``.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import threading
from collections.abc import Callable, Mapping
from http.cookies import SimpleCookie
from typing import TYPE_CHECKING, Annotated, Any, Literal, TypeVar

from fastmcp import Context, FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import get_http_headers
from mcp.types import ToolAnnotations
from pydantic import Field

from autoair import __version__
from autoair.ai.assist import fix_dag, optimize_dag, review_dag
from autoair.ai.assist import scan_dag_leaks as run_leak_scan
from autoair.ai.availability import ai_runtime_available
from autoair.ai.config import ai_settings_of
from autoair.ai.corpus import retriever_for
from autoair.ai.findings import FindingsReport, PatchError, PatchOp, apply_patch
from autoair.ai.generator import GenerationResult, generate_spec
from autoair.ai.llm import LLM, LLMError
from autoair.ai.memory import MemoryError, Scope, conventions_for, memory_for
from autoair.ai.problems import SpecProblem, check_spec, with_paths
from autoair.ai.tuning import HistoryStats, fetch_history, history_stats
from autoair.auth import session_cookie_name
from autoair.catalog import OperatorDetail
from autoair.codegen import CodegenError, render
from autoair.gateway import AirflowError, AirflowGateway, DagStatus
from autoair.iam import model as P
from autoair.mcp.git_token import mcp_writer, request_git_token
from autoair.mcp.models import (
    BlueprintList,
    CodeResult,
    ConnectionList,
    DagCode,
    DagList,
    DeleteOutcome,
    ImportedSpec,
    ImportResult,
    KnowledgeHits,
    MemoryList,
    MemoryOutcome,
    NodeKindInfo,
    NodeKindList,
    NotifierList,
    OperatorBrief,
    OperatorList,
    OperatorTemplateDetail,
    OperatorTemplateList,
    PatchResult,
    RunList,
    SaveResult,
    SourceSummary,
    TaskLog,
    TemplateSaveResult,
    TriggerOutcome,
    ValidationOutcome,
    VariableList,
)
from autoair.mcp.store import RepositoryStore, SpecStore, store_for
from autoair.secret_mask import mask, masking
from autoair.spec import DagSpec
from autoair.storage.base import AuthError
from autoair.storage.render import TargetRepository
from autoair.validation import Policy, Problem, policy_from_settings

if TYPE_CHECKING:
    from autoair.api.app import Services

AIRFLOW_TOKEN_ENV = "AUTOAIR_AIRFLOW_TOKEN"  # noqa: S105 - env var name, not a secret
USER_ENV = "AUTOAIR_MCP_USER"
T = TypeVar("T")

GatewayFactory = Callable[[str, Mapping[str, str]], AirflowGateway]

INSTRUCTIONS = """AutoAir builds Apache Airflow 3 DAGs from JSON "DagSpec" documents.
Typical flow: read the guide (resource autoair://docs/dagspec-authoring) or the schema
(get_dagspec_schema); find operators (search_knowledge, list_operators) and read their
params (get_operator_schema); draft a spec; run validate_dag_spec and fix every error
(problems carry JSON-pointer paths; apply_spec_patch edits by node id); check it with
review_dag_spec and scan_dag_leaks; optionally preview generate_code; save_dag_spec with
confirm=true only after the user agreed. Use list_connections / list_variables to reference
real Airflow connection ids instead of inventing them. Read the team conventions
(list_memories / resource autoair://memory) before authoring and store new ones the user
states with remember."""

_BUILTIN_KINDS: tuple[tuple[str, str, str], ...] = (
    ("operator", "Operator", "Any catalog operator: set `operator` (FQN) and `params`."),
    (
        "python_task",
        "Python task",
        "TaskFlow function: `function_name`, `code` (function body), `decorator`, `arguments`.",
    ),
    ("task_group", "Task group", "Groups nodes; members reference it via `parent_group`."),
    (
        "blueprint",
        "Astronomer Blueprint step",
        "A blueprint template (list_blueprints): `step_id`, `blueprint`, `version`, `class_path`, "
        "`config_class` and `config` (validated against the blueprint's config_schema).",
    ),
    ("note", "Note", "Canvas sticky note (`text`); not a task."),
)

READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
READ_ONLY_REMOTE = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=True
)
#: Tools that cannot work without AutoAir's LLM (hidden without the autoair[ai] extra).
LLM_ONLY_TOOLS = ("generate_dag_from_prompt", "fix_dag_spec", "write_code")

LLM_TOOL = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=False, open_world_hint=True
)
DESTRUCTIVE = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False
)
#: Not read-only: runs the DAG file's code in a sandbox (the round-trip import).
RUNS_CODE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
MEMORY_WRITE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
MAX_PAGE = 200
MAX_CODE_CHARS = 200_000

SpecArg = Annotated[
    dict[str, Any], Field(description="A complete DagSpec document (see get_dagspec_schema).")
]
ConfirmArg = Annotated[
    bool, Field(description="Must be true to act; false = dry run. Ask the user first.")
]
UseAiArg = Annotated[
    bool,
    Field(
        description="Also run AutoAir's own LLM (if configured). Leave false when you can "
        "reason about the deterministic findings yourself."
    ),
]


def _source_summary(plan: Any) -> SourceSummary:
    diff = plan.diff
    cut = len(diff) > MAX_CODE_CHARS
    return SourceSummary(
        policy=plan.policy,
        action=plan.action,
        target_state=plan.target_state,
        operations=plan.operations,
        warnings=plan.warnings,
        refusals=[r.message for r in plan.refusals],
        options=list(plan.options),
        diff=diff[:MAX_CODE_CHARS] if cut else diff,
        diff_truncated=cut or plan.diff_truncated,
        target_sha=plan.target_sha,
        content_hidden=plan.content_hidden,
        message=plan.message,
    )


def _git_failure(dag_id: str, exc: AuthError, raw: str | None) -> str:
    """A tool error for a git host refusing the credentials (masked; with the user's token:
    never retried with other credentials)."""
    if raw is None:
        return f"could not write {dag_id!r}: {exc}"
    return mask(
        f"could not write {dag_id!r}: the git host rejected your token "
        f"(HTTP {exc.status}; check its scopes): {exc}"
    )


def _audit_mcp_write(
    svc: Any, action: str, dag_id: str, writer: Any, *, location: str | None = None
) -> None:
    """Audit an MCP write made with the user's own git token (``credential: request-token``,
    plus the git host's name for the token's owner when it tells)."""
    audit = getattr(svc, "audit", None)
    if audit is None:
        return
    from autoair.storage.request_token import token_user

    principal = _mcp_principal()
    audit.record(
        action,
        user=getattr(principal, "user", None),
        dag_id=dag_id,
        via="mcp",
        target="git",
        location=location,
        credentials="request_token",
        credential="request-token",
        credential_user=token_user(writer),
    )


def _mcp_principal() -> Any:
    try:
        from fastmcp.server.dependencies import get_http_request

        from autoair.mcp.iam import PRINCIPAL_ATTR

        return getattr(get_http_request().state, PRINCIPAL_ATTR, None)
    except RuntimeError:
        return None


def _default_gateway(base_url: str, headers: Mapping[str, str]) -> AirflowGateway:
    return AirflowGateway(base_url, headers=dict(headers))


def _request_headers() -> Mapping[str, str]:
    return get_http_headers(include={"authorization", "cookie"})


def airflow_credentials(ctx: Context | None = None) -> dict[str, str]:
    """Authorization to forward to Airflow: the incoming HTTP request's header or AutoAir
    cookie (plugin / HTTP mode), else ``AUTOAIR_AIRFLOW_TOKEN`` (stdio)."""
    headers = _request_headers()
    auth = headers.get("authorization")
    if auth:
        return {"Authorization": auth}
    cookie = headers.get("cookie")
    if cookie:
        jar: SimpleCookie = SimpleCookie()
        jar.load(cookie)
        morsel = jar.get(session_cookie_name())
        if morsel is not None and morsel.value:
            return {"Authorization": f"Bearer {morsel.value}"}
    token = os.environ.get(AIRFLOW_TOKEN_ENV)
    if token:
        return {"Authorization": token if " " in token else f"Bearer {token}"}
    return {}


def current_user() -> str | None:
    """Caller identity for user-scoped memories: the ``sub``/``preferred_username`` of the
    (already authenticated) bearer JWT in HTTP mode, ``AUTOAIR_MCP_USER`` or the OS user."""
    auth = airflow_credentials().get("Authorization", "")
    if auth.lower().startswith("bearer ") and auth.count(".") == 2:
        try:
            payload = auth.split(" ", 1)[1].split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
            for key in ("preferred_username", "username", "sub"):
                if isinstance(claims.get(key), str) and claims[key]:
                    return str(claims[key])
        except (ValueError, IndexError):
            pass  # not a decodable JWT payload: fall back to AUTOAIR_MCP_USER or the OS user
    if os.environ.get(USER_ENV):
        return os.environ[USER_ENV]
    try:
        return getpass.getuser()
    except Exception:
        return None


def _page(total: int, offset: int, limit: int) -> dict[str, Any]:
    more = offset + limit < total
    return {"total": total, "truncated": more, "next_offset": offset + limit if more else None}


def create_mcp_server(
    services_factory: Callable[[], Services] | Services,
    *,
    store: SpecStore | None = None,
    llm: LLM | None = None,
    gateway_factory: GatewayFactory | None = None,
    name: str = "autoair",
) -> FastMCP:
    """Build the MCP server.

    ``services_factory`` may be a ``Services`` instance or a zero-arg factory (called
    lazily, once, on first use, so start-up stays fast). ``store`` overrides the spec
    storage (default: adapter over ``services.deploy_target``/``services.repository``),
    ``llm`` the LLM (default: ``services.llm``), ``gateway_factory`` the Airflow client.
    """
    lock = threading.Lock()
    cache: dict[str, Any] = {}
    make_gateway = gateway_factory or _default_gateway

    def services() -> Services:
        if "services" not in cache:
            with lock:
                if "services" not in cache:
                    cache["services"] = (
                        services_factory() if callable(services_factory) else services_factory
                    )
        svc: Services = cache["services"]
        return svc

    def spec_store() -> SpecStore:
        return store if store is not None else store_for(services())

    def custom_store() -> bool:
        """A store passed to create_mcp_server (no deploy target behind it)."""
        return store is not None

    def policy() -> Policy:
        return policy_from_settings(services().settings)

    def get_llm() -> LLM | None:
        return llm if llm is not None else getattr(services(), "llm", None)

    def require_llm() -> LLM:
        client = get_llm()
        if client is None:
            from autoair.ai.availability import ai_unavailable_reason

            raise ToolError(
                (
                    ai_unavailable_reason()
                    or "AutoAir has no LLM configured (AUTOAIR_AI__PROVIDER "
                    "/ AUTOAIR_AI__API_KEY or AUTOAIR_AI__LLM_CONN_ID)."
                )
                + " Do the reasoning yourself with the deterministic tools "
                "(validate_dag_spec, review_dag_spec, scan_dag_leaks)."
            )
        return client

    def writes_allowed() -> None:
        if not ai_settings_of(services().settings).mcp.allow_writes:
            raise ToolError(
                "this AutoAir MCP server is read-only (AUTOAIR_AI__MCP__ALLOW_WRITES=false)"
            )

    def gateway() -> AirflowGateway:
        return make_gateway(services().settings.airflow_url, airflow_credentials())

    def parse(spec: Mapping[str, Any]) -> DagSpec:
        check = check_spec(spec, catalog=None, registry=services().registry)
        if check.spec is None:
            raise ToolError(
                "spec is not a valid DagSpec:\n"
                + "\n".join(f"{p.path}: {p.message}" for p in check.problems[:20])
            )
        return check.spec

    def render_problems(spec: DagSpec) -> tuple[str | None, list[SpecProblem]]:
        try:
            return render(spec, services().registry), []
        except CodegenError as exc:
            problem = Problem(
                severity="error", code="codegen", message=str(exc), node_id=exc.node_id
            )
        except Exception as exc:  # e.g. custom handler config errors
            problem = Problem(severity="error", code="codegen", message=str(exc))
        return None, with_paths(spec, [problem])

    def llm_call(fn: Callable[[], T]) -> T:
        try:
            return fn()
        except LLMError as exc:
            raise ToolError(f"LLM error: {exc}") from exc

    mcp = FastMCP(
        name,
        instructions=INSTRUCTIONS,
        version=__version__,
        website_url="https://github.com/ctolon/autoair",
        mask_error_details=True,
        on_duplicate="error",
    )
    # Same AutoAir permissions as the REST API for every tool/resource (HTTP callers).
    from autoair.iam.deps import iam_settings
    from autoair.mcp.git_token import mcp_tokens_allowed
    from autoair.mcp.iam import IamMiddleware
    from autoair.storage.request_token import git_token_required

    mcp.add_middleware(
        IamMiddleware(
            require_approval=lambda: iam_settings(services()).require_approval,
            feature=lambda name: services().settings.feature(name),
            git_token_required=lambda: git_token_required(services()),
            git_token=request_git_token,
            git_token_allowed=lambda: mcp_tokens_allowed(services()),
        )
    )

    # ------------------------------------------------------------------ catalog

    @mcp.tool(title="List operators", annotations=READ_ONLY, tags={"catalog"})
    def list_operators(
        query: Annotated[
            str | None,
            Field(description="Words that must all appear (class, module, integration, summary)."),
        ] = None,
        provider: Annotated[
            str | None, Field(description="Provider package substring, e.g. 'postgres'.")
        ] = None,
        limit: Annotated[int, Field(ge=1, le=MAX_PAGE)] = 50,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> OperatorList:
        """Browse the operator catalog (installed Airflow providers) by exact words.

        For "which operator does X" questions prefer search_knowledge (ranked, fuzzy).
        Returns compact summaries; use get_operator_schema for parameters.
        """
        words = (query or "").lower().split()
        prov = (provider or "").lower()
        matches = []
        for s in services().catalog.summaries():
            haystack = " ".join(
                (s.fqn, s.class_name, s.integration or "", s.summary, s.provider)
            ).lower()
            if prov and prov not in s.provider.lower():
                continue
            if all(w in haystack for w in words):
                matches.append(s)
        return OperatorList(
            operators=[
                OperatorBrief(
                    fqn=s.fqn,
                    class_name=s.class_name,
                    provider=s.provider,
                    category=s.category,
                    summary=s.summary[:300],
                    builder_supported=s.builder_supported,
                )
                for s in matches[offset : offset + limit]
            ],
            **_page(len(matches), offset, limit),
        )

    @mcp.tool(title="Get operator schema", annotations=READ_ONLY, tags={"catalog"})
    def get_operator_schema(
        fqn: Annotated[str, Field(description="Fully qualified class name from the catalog.")],
    ) -> OperatorDetail:
        """Full catalog entry of an operator: JSON Schema of its constructor params
        (`params_schema`; `x-autoair-conn-type` marks connection-id params), templated
        fields, sensor/deferrable flags and docstring. Read it before using an operator."""
        detail = services().catalog.get(fqn)
        if detail is None:
            raise ToolError(f"operator {fqn!r} is not in the catalog (try search_knowledge)")
        return detail

    @mcp.tool(title="Get DagSpec JSON schema", annotations=READ_ONLY, tags={"catalog"})
    def get_dagspec_schema() -> dict[str, Any]:
        """JSON Schema (draft 2020-12) of the DagSpec document accepted by all spec tools.
        Large: read the autoair://docs/dagspec-authoring guide first for the essentials."""
        return DagSpec.model_json_schema()

    @mcp.tool(title="List node kinds", annotations=READ_ONLY, tags={"catalog"})
    def list_node_kinds() -> NodeKindList:
        """Node kinds usable in `nodes` (built-ins plus installed custom node handlers)."""
        kinds = [NodeKindInfo(kind=k, title=t, description=d) for k, t, d in _BUILTIN_KINDS]
        for h in services().registry.describe():
            kinds.append(
                NodeKindInfo(
                    kind="custom",
                    handler=str(h["name"]),
                    title=str(h.get("title", h["name"])),
                    description=str(h.get("description", "")),
                    config_schema=h.get("config_schema"),
                )
            )
        return NodeKindList(node_kinds=kinds)

    @mcp.tool(title="List blueprints", annotations=READ_ONLY, tags={"catalog"})
    def list_blueprints(
        name: Annotated[
            str | None, Field(description="Only this blueprint name (all its versions).")
        ] = None,
        limit: Annotated[int, Field(ge=1, le=MAX_PAGE)] = 50,
    ) -> BlueprintList:
        """Astronomer Blueprint templates usable as `kind: blueprint` nodes, with the JSON
        Schema of each version's config. Copy name, version, class_path and config_class into
        the node exactly as listed."""
        svc = services()
        catalog = getattr(svc.catalog, "blueprints", None)
        if catalog is None or not svc.settings.feature("blueprints"):
            return BlueprintList(available=False, blueprints=[])
        found = [d for d in catalog.data().blueprints if name is None or d.name == name]
        return BlueprintList(
            available=True,
            blueprints=[d.model_dump(mode="json", exclude={"docstring"}) for d in found[:limit]],
            total=len(found),
        )

    @mcp.tool(title="List notifiers", annotations=READ_ONLY, tags={"catalog"})
    def list_notifiers() -> NotifierList:
        """Notifier classes usable in `callbacks` (`notifier` FQN + `params`)."""
        fn = getattr(services().catalog, "notifiers", None)
        if not callable(fn):
            return NotifierList(available=False, notifiers=[])
        items = []
        for n in fn():
            dump = getattr(n, "model_dump", None)
            items.append(dump(mode="json") if callable(dump) else dict(n))
        return NotifierList(available=True, notifiers=items)

    @mcp.tool(title="Search operators, docs and DAGs", annotations=READ_ONLY, tags={"catalog"})
    def search_knowledge(
        query: Annotated[
            str,
            Field(
                min_length=1,
                max_length=500,
                description="Natural language, e.g. 'copy files from S3 to GCS'.",
            ),
        ],
        kinds: Annotated[
            list[Literal["operator", "doc", "spec", "template"]] | None,
            Field(description="Restrict to these corpora (default all)."),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=25)] = 8,
    ) -> KnowledgeHits:
        """Ranked search (BM25, plus embeddings when configured) over the operator catalog,
        AutoAir/Airflow 3 guides, saved DAG specs and templates. Use it to find the right
        operator, an example DAG to copy from, or a best practice. Hits carry a resource
        `uri` to read the full item."""
        retriever = retriever_for(services())
        return KnowledgeHits(
            hits=retriever.search(query, limit, kinds),
            retrieval="hybrid" if retriever.hybrid else "bm25",
        )

    # ------------------------------------------------------------------ specs

    @mcp.tool(title="Validate DAG spec", annotations=READ_ONLY, tags={"specs"})
    def validate_dag_spec(
        spec: SpecArg,
        import_check: Annotated[
            bool,
            Field(description="Also render and import the DAG in a sandboxed DagBag (slower)."),
        ] = False,
    ) -> ValidationOutcome:
        """Validate a DagSpec: schema, graph, operator params and organisation policy.

        Each problem has a JSON-pointer `path` into `spec`, the `node_id` and `field`.
        Fix every `error` before saving.
        """
        svc = services()
        check = check_spec(
            spec,
            catalog=svc.catalog,
            registry=svc.registry,
            policy=policy(),
            import_check=import_check,
        )
        return ValidationOutcome(
            ok=check.ok,
            problems=check.problems,
            import_errors=list(check.import_check.import_errors) if check.import_check else [],
        )

    @mcp.tool(title="Patch DAG spec", annotations=READ_ONLY, tags={"specs"})
    def apply_spec_patch(
        spec: SpecArg,
        patch: Annotated[
            list[PatchOp],
            Field(
                min_length=1,
                max_length=100,
                description="JSON Patch ops (add/replace/remove). Address nodes by id: "
                "/nodes/@<node id>/retries, /nodes/@<node id>/params/sql; append a node "
                "with path /nodes/-.",
            ),
        ],
    ) -> PatchResult:
        """Apply JSON-patch edits to a spec and validate the result (nothing is saved).
        Cheaper than re-sending a whole spec for small changes."""
        try:
            document = apply_patch(spec, patch)
        except PatchError as exc:
            return PatchResult(ok=False, spec=None, problems=[], error=str(exc))
        svc = services()
        check = check_spec(document, catalog=svc.catalog, registry=svc.registry, policy=policy())
        return PatchResult(ok=check.ok, spec=check.spec, problems=check.problems)

    @mcp.tool(title="Generate DAG code", annotations=READ_ONLY, tags={"specs"})
    def generate_code(spec: SpecArg) -> CodeResult:
        """Render the Airflow DAG Python file for a DagSpec (preview; nothing is written)."""
        parsed = parse(spec)
        code, problems = render_problems(parsed)
        if code is not None and len(code) > MAX_CODE_CHARS:
            code = code[:MAX_CODE_CHARS] + "\n# ... truncated ..."
        return CodeResult(code=code, problems=problems)

    @mcp.tool(title="List saved DAGs", annotations=READ_ONLY, tags={"specs"})
    def list_dags(
        query: Annotated[
            str | None, Field(description="Substring of dag_id / name / tags / description.")
        ] = None,
        limit: Annotated[int, Field(ge=1, le=MAX_PAGE)] = 100,
        offset: Annotated[int, Field(ge=0)] = 0,
        document_type: Annotated[
            Literal["spec", "raw"] | None,
            Field(description="Only visual (spec) or raw code (raw) DAGs."),
        ] = None,
    ) -> DagList:
        """DAG specs saved by AutoAir (id, name, tags, task count, location, document type:
        spec = visual, raw = raw code DAG)."""
        dags = spec_store().list()
        if document_type is not None:
            dags = [d for d in dags if (d.document_type or "spec") == document_type]
        if query:
            q = query.lower()
            dags = [
                d
                for d in dags
                if q
                in " ".join((d.dag_id, d.display_name or "", d.description or "", *d.tags)).lower()
            ]
        return DagList(dags=dags[offset : offset + limit], **_page(len(dags), offset, limit))

    @mcp.tool(title="Get DAG spec", annotations=READ_ONLY, tags={"specs"})
    def get_dag_spec(dag_id: str) -> DagSpec:
        """The saved DagSpec of a DAG (edit it and save it back with save_dag_spec)."""
        try:
            spec = spec_store().get(dag_id)
        except Exception as exc:
            raise ToolError(str(exc)) from exc
        if spec is None:
            raise ToolError(f"no AutoAir spec for {dag_id!r} (see list_dags)")
        return spec

    @mcp.tool(title="Save DAG spec", annotations=DESTRUCTIVE, tags={"specs", "write"})
    def save_dag_spec(
        spec: SpecArg,
        confirm: ConfirmArg = False,
        expected_target_sha: Annotated[
            str | None,
            Field(
                max_length=64,
                description="target_sha of the preview (dry run or preview_dag_source) the "
                "user agreed to: the save stops when the DAG file changed since.",
            ),
        ] = None,
    ) -> SaveResult:
        """Validate and save a DagSpec, writing the DAG file to the deploy target (Airflow's
        DAG folder, object storage or git); overwrites an AutoAir DAG with the same dag_id.

        Without `confirm=true` this is a dry run that only validates and says whether it
        would create or overwrite. Ask the user before confirming.
        """
        return save_spec(spec, confirm, expected_target_sha)

    @mcp.tool(title="Preview DAG file", annotations=RUNS_CODE, tags={"specs"})
    def preview_dag_source(
        spec: SpecArg,
        resolution: Annotated[
            Literal["preserve", "overwrite"] | None,
            Field(
                description="Only after a 'blocked' preview (a file changed outside "
                "AutoAir): preview applying the change to that file (preserve) or "
                "regenerating it (overwrite)."
            ),
        ] = None,
    ) -> SourceSummary:
        """What save_dag_spec would write as the DAG file, without writing: generated from
        the spec, or -- for DAGs that come from hand-written Python (source policy
        preserve) -- the existing file with the change patched in (comments and formatting
        kept), with a unified diff. Preserve previews import the file in a sandbox."""
        from autoair.api.source_plans import plan_for

        parsed = parse(spec)
        svc = services()
        target = getattr(svc, "target", None)
        if target is None:
            raise ToolError("this MCP server has no deploy target to compare with")
        try:
            plan = plan_for(
                svc,
                _mcp_principal(),
                parsed,
                target=target,
                environment=None,
                resolution=resolution,
            )
        except Exception as exc:
            raise ToolError(mask(f"cannot preview {parsed.dag_id!r}: {exc}")) from None
        return _source_summary(plan)

    @mcp.tool(title="Get DAG code", annotations=READ_ONLY, tags={"specs"})
    def get_dag_code(dag_id: str) -> DagCode:
        """The DAG file of a saved DAG: the hand-written code of a raw code DAG
        (document_type raw), or the file generated from a visual spec (document_type spec)."""
        try:
            spec = spec_store().get(dag_id)
        except Exception as exc:
            raise ToolError(str(exc)) from exc
        if spec is None:
            raise ToolError(f"no AutoAir spec for {dag_id!r} (see list_dags)")
        if spec.raw_code is not None:
            return DagCode(dag_id=dag_id, document_type="raw", code=spec.raw_code)
        from autoair.api.source_plans import existing_file
        from autoair.preserve.service import keeps_hand_written_file

        target = getattr(services(), "target", None)
        settings = getattr(services(), "settings", None)
        if store is None and target is not None and keeps_hand_written_file(spec, settings):
            # The DAG keeps its hand-written file: that file is the DAG, not generated code.
            try:
                current = existing_file(target, dag_id, None, None)
            except Exception as exc:
                raise ToolError(mask(f"cannot read the DAG file of {dag_id!r}: {exc}")) from None
            if current is not None:
                return DagCode(dag_id=dag_id, document_type="spec", code=current.content)
        code, problems = render_problems(spec)
        if code is None:
            raise ToolError("; ".join(p.message for p in problems) or "code generation failed")
        return DagCode(dag_id=dag_id, document_type="spec", code=code)

    @mcp.tool(title="Save raw DAG code", annotations=DESTRUCTIVE, tags={"specs", "write"})
    def save_raw_dag_code(
        dag_id: Annotated[str, Field(min_length=1, max_length=250)],
        code: Annotated[
            str,
            Field(
                max_length=MAX_CODE_CHARS,
                description="The complete DAG file (Python). Its DAG must use this dag_id.",
            ),
        ],
        confirm: ConfirmArg = False,
        display_name: Annotated[str | None, Field(max_length=500)] = None,
        tags: Annotated[list[str] | None, Field(max_length=50)] = None,
    ) -> SaveResult:
        """Validate and save a raw code DAG: a hand-written Airflow DAG file deployed as
        written (syntax, lint, secret, policy and import checks; no spec rules). Updates an
        existing raw code DAG with this dag_id (keeping its team and environments); refuses
        to replace a visual (spec-backed) DAG. Without `confirm=true` this is a dry run."""
        try:
            current = spec_store().get(dag_id)
        except Exception:
            current = None
        if current is not None and current.raw_code is None:
            raise ToolError(
                f"{dag_id!r} is a visual (spec-backed) DAG: edit its spec with save_dag_spec; "
                "converting it to raw code is done in the AutoAir UI"
            )
        data: dict[str, Any] = (
            current.model_dump(mode="json", by_alias=True, exclude_none=True)
            if current is not None
            else {"dag_id": dag_id}
        )
        data["raw_code"] = code
        if display_name is not None:
            data["display_name"] = display_name
        if tags is not None:
            data["tags"] = tags
        return save_spec(data, confirm)

    def preserved_problems(parsed: DagSpec, plan: Any) -> list[SpecProblem] | None:
        """Errors of a patched / kept hand-written file (policy rules on its hand-written
        parts, like the deploy route's validation); None when it may be written."""
        if plan is None or plan.action not in ("preserve", "unchanged") or plan.source is None:
            return None
        from autoair.validation import validate_spec

        svc = services()
        principal = _mcp_principal()
        templates = getattr(svc, "templates", None)
        settings = getattr(svc, "settings", None)
        # The same inputs the REST deploy validates with (specs.validate_for_write), so
        # policy rules and OPA decide the same over MCP.
        report = validate_spec(
            parsed,
            catalog=svc.catalog,
            registry=svc.registry,
            policy=policy(),
            target_version=settings.target_version_tuple() if settings is not None else None,
            templates=templates.lock if templates is not None else None,
            environment=None,
            context={
                "user": getattr(principal, "user", None) or current_user(),
                "action": "deploy",
            },
            source=plan.source,
        )
        return None if report.ok else with_paths(parsed, report.problems)

    def save_spec(
        spec: Mapping[str, Any], confirm: bool, expected_target_sha: str | None = None
    ) -> SaveResult:
        writes_allowed()
        svc = services()
        check = check_spec(spec, catalog=svc.catalog, registry=svc.registry, policy=policy())
        if not check.ok or check.spec is None:
            return SaveResult(
                saved=False,
                dry_run=not confirm,
                problems=check.problems,
                message="validation failed; nothing was written",
            )
        parsed = check.spec
        target = spec_store()
        try:
            stored = target.get(parsed.dag_id)
        except Exception:
            stored = None
        exists = stored is not None
        # A raw code DAG replacing its own code (the caller sent all of it): like the REST
        # deploy, the kept file may be replaced without deploy:overwrite_ejected, and only
        # while it is unchanged since AutoAir wrote it (no force over MCP).
        raw_edit = (
            parsed.raw_code is not None and stored is not None and stored.raw_code is not None
        )
        # A dry run reads the target with the shared credentials (no token header needed):
        # when that is not possible the preview is left out instead of failing.
        plan = source_plan(svc, parsed, getattr(svc, "target", None), soft=True)
        summary = _source_summary(plan) if plan is not None else None
        if plan is not None and plan.action in ("blocked", "refused"):
            return SaveResult(
                saved=False,
                dry_run=not confirm,
                problems=check.problems,
                source=summary,
                message=plan.message + "".join(f"\n- {r.message}" for r in plan.refusals[:10]),
            )
        refused = preserved_problems(parsed, plan)
        if refused is not None:
            return SaveResult(
                saved=False,
                dry_run=not confirm,
                problems=refused,
                source=summary,
                message="validation of the DAG file failed; nothing was written",
            )
        if not confirm:
            verb = "overwrite the existing" if exists else "create a new"
            how = f" ({plan.message})" if plan is not None else ""
            return SaveResult(
                saved=False,
                dry_run=True,
                problems=check.problems,
                source=summary,
                message=f"validation passed; call again with confirm=true to {verb} "
                f"DAG {parsed.dag_id!r}{how}",
            )
        source: str | None = None
        if parsed.output_mode == "python":
            source, problems = render_problems(parsed)
            if source is None:
                return SaveResult(
                    saved=False, dry_run=False, problems=problems, message="code generation failed"
                )
        raw = request_git_token()  # storage.git.mcp_request_token (autoair.mcp.git_token)
        with masking(raw):
            writer = mcp_writer(svc, raw)
            # the plan the write follows: read with the user's token when there is one
            try:
                plan = source_plan(
                    svc, parsed, writer if writer is not None else getattr(svc, "target", None)
                )
            except AuthError as exc:
                raise ToolError(_git_failure(parsed.dag_id, exc, raw)) from None
            if plan is not None and plan.action in ("blocked", "refused"):
                return SaveResult(
                    saved=False, dry_run=False, source=_source_summary(plan), message=plan.message
                )
            if (
                plan is not None
                and expected_target_sha is not None
                and plan.target_sha != expected_target_sha
            ):
                return SaveResult(
                    saved=False,
                    dry_run=False,
                    source=_source_summary(plan),
                    message="the DAG file in the target changed since the preview: preview "
                    "it again",
                )
            refused = preserved_problems(parsed, plan)
            if refused is not None:
                return SaveResult(
                    saved=False,
                    dry_run=False,
                    problems=refused,
                    source=_source_summary(plan),
                    message="validation of the DAG file failed; nothing was written",
                )
            store = RepositoryStore(TargetRepository(writer)) if writer is not None else target
            try:
                if plan is not None and plan.action in ("preserve", "unchanged"):
                    location = preserve_write(svc, writer, parsed, plan)
                elif raw_edit and not custom_store() and (writer or getattr(svc, "target", None)):
                    location = raw_write(svc, writer, parsed, source)
                else:
                    location = store.save(parsed, source)
            except AuthError as exc:
                raise ToolError(_git_failure(parsed.dag_id, exc, raw)) from None
            except Exception as exc:
                if raw is None:
                    raise ToolError(f"could not save {parsed.dag_id!r}: {exc}") from exc
                raise ToolError(mask(f"could not save {parsed.dag_id!r}: {exc}")) from None
            if writer is not None:
                _audit_mcp_write(svc, "deploy", parsed.dag_id, writer, location=str(location))
            location = mask(str(location)) if raw is not None else location
        retriever_for(svc).invalidate()
        return SaveResult(
            saved=True,
            dry_run=False,
            location=location,
            problems=check.problems,
            source=_source_summary(plan) if plan is not None else None,
            message=f"saved to {location}; Airflow picks it up on the next DAG bundle parse",
        )

    def source_plan(svc: Any, parsed: DagSpec, target: Any, *, soft: bool = False) -> Any:
        """The source plan of writing ``parsed`` to ``target`` (None: a custom store, an
        output mode the source policy does not apply to, or -- ``soft`` -- a target that
        cannot be read right now)."""
        from autoair.api.source_plans import plan_for
        from autoair.preserve.service import applicable

        if store is not None or target is None or not applicable(parsed):
            return None
        if getattr(svc, "settings", None) is None:
            return None
        try:
            return plan_for(svc, _mcp_principal(), parsed, target=target, environment=None)
        except AuthError:
            if soft:
                return None
            raise  # reported like a refused write (never echoing the token)
        except Exception as exc:
            if soft:
                return None
            raise ToolError(mask(f"cannot plan the DAG file of {parsed.dag_id!r}: {exc}")) from None

    def preserve_write(svc: Any, writer: Any, parsed: DagSpec, plan: Any) -> str:
        """Write the patched (or unchanged hand-written) file of a preserve plan."""
        target_obj = writer if writer is not None else svc.target
        name = f"{parsed.dag_id}.py"
        kwargs: dict[str, Any] = {}
        if plan.expected_sha is not None:
            kwargs["expected"] = {name: plan.expected_sha}
        if plan.hand_written or plan.overwrite:
            # Like the REST deploy: the kept file is recorded as hand-written, so a later
            # regenerate needs preserve.allow_force_overwrite and deploy:overwrite_ejected
            # (REST only) and a delete over MCP is refused.
            kwargs["hand_written"] = plan.hand_written
        result = target_obj.save(plan.stored_spec or parsed, {name: plan.source}, **kwargs)
        return str(result.path)

    def raw_write(svc: Any, writer: Any, parsed: DagSpec, source: str | None) -> str:
        """Write a raw code DAG over its own earlier code (``allow_hand_written``)."""
        from autoair.storage import render_dag_files

        target_obj = writer if writer is not None else svc.target
        files = render_dag_files(parsed, python_source=source)
        result = target_obj.save(parsed, files, allow_hand_written=True)
        return str(result.path)

    @mcp.tool(title="Delete DAG spec", annotations=DESTRUCTIVE, tags={"specs", "write"})
    def delete_dag_spec(dag_id: str, confirm: ConfirmArg = False) -> DeleteOutcome:
        """Delete a saved AutoAir DAG (spec and generated file). Requires `confirm=true`."""
        writes_allowed()
        target = spec_store()
        if not confirm:
            exists = target.get(dag_id) is not None
            return DeleteOutcome(
                deleted=False,
                message=(
                    f"dry run: call again with confirm=true to delete {dag_id!r}"
                    if exists
                    else f"no AutoAir spec for {dag_id!r}"
                ),
            )
        raw = request_git_token()  # storage.git.mcp_request_token (autoair.mcp.git_token)
        with masking(raw):
            writer = mcp_writer(services(), raw)
            store = RepositoryStore(TargetRepository(writer)) if writer is not None else target
            try:
                deleted = store.delete(dag_id)
            except AuthError as exc:
                raise ToolError(_git_failure(dag_id, exc, raw)) from None
            except Exception as exc:
                if raw is None:
                    raise ToolError(f"could not delete {dag_id!r}: {exc}") from exc
                raise ToolError(mask(f"could not delete {dag_id!r}: {exc}")) from None
            if writer is not None and deleted:
                _audit_mcp_write(services(), "delete", dag_id, writer)
        retriever_for(services()).invalidate()
        return DeleteOutcome(
            deleted=deleted, message="deleted" if deleted else f"no AutoAir spec for {dag_id!r}"
        )

    # ------------------------------------------------------------ operator templates

    def template_owner_check(owner: str | None) -> None:
        """Multi-team deployments: another team's template needs ``admin:config`` (HTTP;
        over stdio the server runs as the local user and no principal applies)."""
        team = services().settings.team
        if not team or not owner or owner.casefold() == team.casefold():
            return
        principal = _mcp_principal()
        if principal is not None and not principal.has(P.ADMIN_CONFIG):
            raise ToolError(
                f"the template is owned by team {owner!r}; this deployment belongs to team "
                f"{team!r} (changing it needs AutoAir permission {P.ADMIN_CONFIG!r})"
            )

    def audit_template(action: str, **fields: Any) -> None:
        audit = getattr(services(), "audit", None)
        if audit is not None:
            principal = _mcp_principal()
            audit.record(
                action, user=getattr(principal, "user", None) or current_user(), via="mcp", **fields
            )

    @mcp.tool(title="List operator templates", annotations=READ_ONLY, tags={"templates"})
    def list_operator_templates(
        query: Annotated[
            str | None,
            Field(
                max_length=200,
                description="Words matched against id, title, description, tags, owner team "
                "and operator class (all must match).",
            ),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=MAX_PAGE)] = 50,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> OperatorTemplateList:
        """List operator templates: reusable, versioned, pre-configured tasks a platform team
        published. Prefer one over a hand-configured operator when it fits; insert it as a
        node with `template_ref: {id, version}` and the template's values."""
        words = (query or "").lower().split()
        items = [t for t in services().templates.list() if t.kind == "operator"]
        if words:

            def hay(t: Any) -> str:
                fields = [t.id, t.title, t.description, t.owner_team, t.operator_class]
                return " ".join([*(f or "" for f in fields), *t.tags]).lower()

            items = [t for t in items if all(w in hay(t) for w in words)]
        items.sort(key=lambda t: t.title.lower())
        return OperatorTemplateList(
            templates=items[offset : offset + limit], **_page(len(items), offset, limit)
        )

    @mcp.tool(title="Get operator template", annotations=READ_ONLY, tags={"templates"})
    def get_operator_template(
        template_id: Annotated[str, Field(max_length=81, description="The template id.")],
        version: Annotated[
            str | None,
            Field(max_length=40, description="An older stored version; default the current."),
        ] = None,
    ) -> OperatorTemplateDetail:
        """Read one operator template (its task, locked and required fields, etag) and its
        version history."""
        from autoair.templates import TemplateError

        store = services().templates
        try:
            tpl = store.get_version(template_id, version) if version else store.get(template_id)
            versions = store.versions(template_id) or []
        except TemplateError as exc:
            raise ToolError(str(exc)) from None
        if tpl is None or tpl.kind != "operator":
            raise ToolError(
                f"no operator template {template_id!r}"
                + (f" in version {version!r}" if version else "")
            )
        return OperatorTemplateDetail(template=tpl, versions=versions)

    @mcp.tool(title="Save operator template", annotations=MEMORY_WRITE, tags={"templates", "write"})
    def save_operator_template(
        template: Annotated[
            dict[str, Any],
            Field(
                description="The template: `id` (slug), `title`, optional `description`, "
                "`owner_team`, `tags`, `icon`, `docs_url`, `version` (empty = next number) "
                "and `blueprint` {`node`: one operator/python_task/custom node, `locked`: "
                "field paths, `required`: field paths}. Field paths: `retries`, `pool`, "
                "`params.<name>`, `arguments.<name>`, `config.<name>`."
            ),
        ],
        if_match: Annotated[
            str | None,
            Field(
                max_length=64,
                description="The `etag` from get_operator_template: save only if nobody "
                "changed the template since. Omit to create a new template (refused when the "
                "id exists) - pass it to publish a new version of an existing one.",
            ),
        ] = None,
        confirm: ConfirmArg = False,
    ) -> TemplateSaveResult:
        """Create an operator template, or publish a new version of one (version N+1; older
        versions stay readable). The task is validated first (catalog parameters, policy);
        errors refuse the save. Requires `confirm=true`."""
        from pydantic import ValidationError

        from autoair.templates import (
            TemplateConflict,
            TemplateError,
            TemplateInput,
            blueprint_problems,
            blueprint_spec,
        )

        writes_allowed()
        try:
            data = TemplateInput.model_validate(
                {"category": "operator", **template, "kind": "operator"}
            )
        except ValidationError as exc:
            raise ToolError(f"invalid template: {exc.errors(include_url=False)}") from None
        assert data.blueprint is not None  # kind=operator without blueprint fails in save()
        svc = services()
        check = check_spec(
            blueprint_spec(data.blueprint),
            catalog=svc.catalog,
            registry=svc.registry,
            policy=policy(),
        )
        kept = {id(p) for p in blueprint_problems(check.problems, data.blueprint)}
        problems = [p for p in check.problems if id(p) in kept]
        if any(p.severity == "error" for p in problems):
            return TemplateSaveResult(
                saved=False,
                dry_run=not confirm,
                problems=problems,
                message="the template's task has errors: fix them and call again",
            )
        store = svc.templates
        try:
            existing = store.get(data.id)
        except TemplateError as exc:
            raise ToolError(str(exc)) from None
        if existing is not None and existing.builtin:
            raise ToolError(f"{data.id!r} is a built-in template id")
        if existing is not None and existing.kind != "operator":
            raise ToolError(f"{data.id!r} is a {existing.kind} template, not an operator template")
        if existing is not None:
            template_owner_check(existing.owner_team)
        template_owner_check(data.owner_team)
        if existing is not None and if_match is None:
            raise ToolError(
                f"operator template {data.id!r} exists (v{existing.version}): read it with "
                "get_operator_template and pass its etag as if_match to publish a new version"
            )
        if not confirm:
            action = f"publish version after v{existing.version} of" if existing else "create"
            return TemplateSaveResult(
                saved=False,
                dry_run=True,
                problems=problems,
                message=f"dry run: valid; call again with confirm=true to {action} {data.id!r}",
            )
        try:
            saved = store.save(
                data,
                user=getattr(_mcp_principal(), "user", None) or current_user(),
                if_match=if_match,
                create_only=existing is None,
            )
        except TemplateConflict as exc:
            raise ToolError(f"{exc}: read it again with get_operator_template") from None
        except TemplateError as exc:
            raise ToolError(str(exc)) from None
        audit_template(
            "template_save",
            template=data.id,
            kind="operator",
            version=saved.version,
            previous_version=existing.version if existing else None,
            created=existing is None,
        )
        return TemplateSaveResult(
            saved=True,
            dry_run=False,
            template=saved,
            problems=problems,
            message=f"saved operator template {data.id!r} v{saved.version}",
        )

    @mcp.tool(
        title="Delete operator template", annotations=DESTRUCTIVE, tags={"templates", "write"}
    )
    def delete_operator_template(
        template_id: Annotated[str, Field(max_length=81, description="The template id.")],
        confirm: ConfirmArg = False,
    ) -> DeleteOutcome:
        """Delete an operator template and its version history. Tasks inserted from it stay
        in their DAGs but are no longer constrained (validation warns `template_missing`).
        Requires `confirm=true`."""
        from autoair.templates import TemplateError

        writes_allowed()
        store = services().templates
        try:
            existing = store.get(template_id)
        except TemplateError as exc:
            raise ToolError(str(exc)) from None
        if existing is None or existing.builtin or existing.kind != "operator":
            return DeleteOutcome(
                deleted=False, message=f"no saved operator template {template_id!r}"
            )
        template_owner_check(existing.owner_team)
        if not confirm:
            return DeleteOutcome(
                deleted=False,
                message=f"dry run: call again with confirm=true to delete {template_id!r} "
                f"(v{existing.version}) and its version history",
            )
        deleted = store.delete(template_id)
        if deleted:
            audit_template(
                "template_delete", template=template_id, kind="operator", version=existing.version
            )
        return DeleteOutcome(deleted=deleted, message="deleted" if deleted else "already gone")

    @mcp.tool(title="Import a Python DAG file", annotations=LLM_TOOL, tags={"specs"})
    def import_python_dag(
        source: Annotated[
            str,
            Field(max_length=200_000, description="Python source of an Airflow DAG file."),
        ],
    ) -> ImportResult:
        """Convert a hand-written Airflow DAG file into DagSpec(s) by importing it in an
        isolated subprocess (needs AUTOAIR_ALLOW_PYTHON_CODE). Only pass code the user gave
        you; review the warnings, then validate_dag_spec."""
        if not services().settings.feature("python_code"):
            raise ToolError("importing Python is disabled (AUTOAIR_ALLOW_PYTHON_CODE=false)")
        from autoair.importer.python_dag import import_python_source
        from autoair.preserve.service import mark_hand_written

        result = import_python_source(source, timeout=60)
        if len(result.dags) == 1 and result.dags[0].spec is not None:
            # the file is the DAG's hand-written source: save_dag_spec preserves it
            dag = result.dags[0]
            if dag.spec is not None:
                dag.spec = mark_hand_written(dag.spec, source, dag.warnings)
        return ImportResult(
            dags=[
                ImportedSpec(dag_id=d.dag_id, spec=d.spec, warnings=d.warnings, error=d.error)
                for d in result.dags
            ],
            import_errors=result.import_errors,
        )

    # ------------------------------------------------------------------ analysis

    @mcp.tool(title="Review DAG spec", annotations=LLM_TOOL, tags={"analysis"})
    def review_dag_spec(spec: SpecArg, use_ai: UseAiArg = False) -> FindingsReport:
        """Best-practice review: retries, timeouts, idempotency (wall-clock use), Airflow 3
        idioms (removed template variables, Task SDK, deadlines vs SLA), ownership and docs.
        Findings carry node_id/field and, when unambiguous, a JSON patch; `proposed_spec`
        has all patches applied and validated."""
        svc = services()
        parsed = parse(spec)
        client = require_llm() if use_ai else None
        return llm_call(
            lambda: review_dag(
                parsed,
                catalog=svc.catalog,
                registry=svc.registry,
                llm=client,
                policy=policy(),
                conventions=conventions_for(svc, user=current_user()),
            )
        )

    @mcp.tool(title="Scan DAG for leaks", annotations=LLM_TOOL, tags={"analysis"})
    def scan_dag_leaks(spec: SpecArg, use_ai: UseAiArg = False) -> FindingsReport:
        """Security scan: hard-coded secrets (keys, tokens, passwords, credential URLs),
        credentials exposed via logs / rendered templates / XCom / command lines, and
        unbounded resources (missing timeouts, poke sensors, unbounded mapping). Secret
        values are masked in the output."""
        svc = services()
        parsed = parse(spec)
        client = require_llm() if use_ai else None
        return llm_call(
            lambda: run_leak_scan(
                parsed, catalog=svc.catalog, registry=svc.registry, llm=client, policy=policy()
            )
        )

    @mcp.tool(title="Optimise DAG parameters", annotations=LLM_TOOL, tags={"analysis", "airflow"})
    async def optimize_dag_parameters(
        spec: SpecArg,
        use_ai: UseAiArg = False,
        history_runs: Annotated[
            int, Field(ge=0, le=50, description="Recent runs to analyse (0 = static only).")
        ] = 15,
    ) -> FindingsReport:
        """Tune retries, execution/run timeouts, deferrable mode and concurrency from the
        DAG's run history in Airflow (p50/p95 durations, flaky tasks); falls back to static
        defaults when Airflow is unreachable or the DAG never ran."""
        svc = services()
        parsed = parse(spec)
        stats: HistoryStats | None = None
        note = None
        if history_runs and not svc.settings.feature("airflow"):
            note = "Run history unavailable (features.airflow is off)."
        elif history_runs:
            try:
                async with gateway() as gw:
                    stats = history_stats(
                        parsed.dag_id, await fetch_history(gw, parsed.dag_id, history_runs)
                    )
            except AirflowError as exc:
                note = f"Run history unavailable ({exc.detail})."
        client = require_llm() if use_ai else None

        def go() -> FindingsReport:
            return optimize_dag(
                parsed,
                catalog=svc.catalog,
                registry=svc.registry,
                stats=stats,
                llm=client,
                policy=policy(),
                conventions=conventions_for(svc, user=current_user()),
            )

        import anyio.to_thread

        report: FindingsReport = await anyio.to_thread.run_sync(lambda: llm_call(go))
        if note:
            report.notes.insert(0, note)
        return report

    # ------------------------------------------------------------------ Airflow

    @mcp.tool(title="List Airflow connections", annotations=READ_ONLY_REMOTE, tags={"airflow"})
    async def list_connections(
        conn_type: Annotated[
            str | None, Field(description="Filter by type, e.g. 'postgres', 'aws'.")
        ] = None,
    ) -> ConnectionList:
        """Airflow connection ids (never secrets), optionally filtered by `conn_type`.
        Reference these ids in `*_conn_id` params instead of inventing names."""
        try:
            async with gateway() as gw:
                conns = await gw.connections()
        except AirflowError as exc:
            raise ToolError(f"Airflow: {exc.detail}") from exc
        if conn_type:
            conns = [c for c in conns if (c.conn_type or "").lower() == conn_type.lower()]
        return ConnectionList(connections=conns)

    @mcp.tool(title="List Airflow variables", annotations=READ_ONLY_REMOTE, tags={"airflow"})
    async def list_variables() -> VariableList:
        """Airflow variable keys (values are never returned)."""
        try:
            async with gateway() as gw:
                return VariableList(variables=await gw.variables())
        except AirflowError as exc:
            raise ToolError(f"Airflow: {exc.detail}") from exc

    @mcp.tool(title="Get DAG status", annotations=READ_ONLY_REMOTE, tags={"airflow"})
    async def get_dag_status(dag_id: str) -> DagStatus:
        """Whether Airflow parsed the DAG, paused state and the latest runs."""
        try:
            async with gateway() as gw:
                return await gw.dag_status(dag_id)
        except AirflowError as exc:
            raise ToolError(f"Airflow: {exc.detail}") from exc

    @mcp.tool(title="List DAG runs", annotations=READ_ONLY_REMOTE, tags={"airflow"})
    async def list_dag_runs(
        dag_id: str, limit: Annotated[int, Field(ge=1, le=100)] = 20
    ) -> RunList:
        """Recent runs of a DAG (newest first): run id, state, dates. Use the run id with
        get_task_log to diagnose failures."""
        try:
            async with gateway() as gw:
                runs = await gw.dag_runs(dag_id, limit=limit)
        except AirflowError as exc:
            raise ToolError(f"Airflow: {exc.detail}") from exc
        return RunList(runs=[r.model_copy(update={"conf": None}) for r in runs])

    @mcp.tool(title="Get task log", annotations=READ_ONLY_REMOTE, tags={"airflow"})
    async def get_task_log(
        dag_id: str,
        run_id: str,
        task_id: str,
        try_number: Annotated[
            int | None, Field(ge=1, description="Default: the latest try.")
        ] = None,
        map_index: int = -1,
        tail_chars: Annotated[int, Field(ge=500, le=50_000)] = 12_000,
    ) -> TaskLog:
        """The tail of a task instance's log (errors are usually at the end)."""
        try:
            async with gateway() as gw:
                if try_number is None:
                    tis = await gw.task_instances(dag_id, run_id)
                    ti = next(
                        (t for t in tis if t.task_id == task_id and t.map_index == map_index),
                        None,
                    )
                    try_number = await gw.latest_log_try(ti) if ti else 1
                text = await gw.task_log(dag_id, run_id, task_id, try_number, map_index)
        except AirflowError as exc:
            raise ToolError(f"Airflow: {exc.detail}") from exc
        return TaskLog(
            text=text[-tail_chars:], truncated=len(text) > tail_chars, total_chars=len(text)
        )

    @mcp.tool(
        title="Trigger DAG run",
        tags={"airflow", "write"},
        annotations=ToolAnnotations(
            read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True
        ),
    )
    async def trigger_dag(
        dag_id: str,
        conf: Annotated[
            dict[str, Any] | None, Field(description="Run configuration (DAG params).")
        ] = None,
        confirm: ConfirmArg = False,
        unpause: Annotated[bool, Field(description="Unpause the DAG first.")] = True,
    ) -> TriggerOutcome:
        """Trigger a DAG run (and unpause the DAG unless `unpause=false`).

        Requires `confirm=true`; ask the user first.
        """
        writes_allowed()
        if not confirm:
            return TriggerOutcome(
                triggered=False,
                message=f"dry run: call again with confirm=true to trigger {dag_id!r}",
            )
        try:
            async with gateway() as gw:
                if unpause:
                    await gw.set_paused(dag_id, False)
                run = await gw.trigger(dag_id, conf)
        except AirflowError as exc:
            raise ToolError(f"Airflow: {exc.detail}") from exc
        return TriggerOutcome(
            triggered=True,
            dag_run_id=run.get("dag_run_id"),
            state=run.get("state"),
            message="triggered",
        )

    # ------------------------------------------------------------------ AI (server-side LLM)

    @mcp.tool(title="Generate DAG from prompt", annotations=LLM_TOOL, tags={"ai"})
    def generate_dag_from_prompt(
        prompt: Annotated[str, Field(min_length=1, max_length=20_000)],
        existing_dag_id: Annotated[
            str | None, Field(description="Modify this saved DAG instead of creating one.")
        ] = None,
        max_repairs: Annotated[int, Field(ge=0, le=5)] = 3,
    ) -> GenerationResult:
        """Draft a validated DagSpec from a natural-language description using AutoAir's
        configured LLM (nothing is saved). If no LLM is configured, author the spec yourself
        with the other tools (see the build_dag_from_description prompt)."""
        client = get_llm()
        if client is None:
            raise ToolError(
                "AutoAir has no LLM configured (set AUTOAIR_ANTHROPIC_API_KEY, AUTOAIR_AI__* or "
                "AUTOAIR_AI__LLM_CONN_ID). Build the spec yourself: get_dagspec_schema, "
                "list_operators, validate_dag_spec."
            )
        existing = None
        if existing_dag_id:
            existing = spec_store().get(existing_dag_id)
            if existing is None:
                raise ToolError(f"no AutoAir spec for {existing_dag_id!r}")
        svc = services()
        result: GenerationResult = llm_call(
            lambda: generate_spec(
                prompt,
                catalog=svc.catalog,
                registry=svc.registry,
                llm=client,
                existing=existing,
                max_repairs=max_repairs,
                policy=policy(),
            )
        )
        return result

    @mcp.tool(title="Fix DAG spec", annotations=LLM_TOOL, tags={"ai"})
    def fix_dag_spec(
        spec: SpecArg,
        import_errors: Annotated[
            list[str], Field(max_length=5, description="DAG import tracebacks.")
        ] = [],  # noqa: B006 - immutable use; FastMCP needs a JSON default
        task_log: Annotated[
            str | None, Field(max_length=200_000, description="Failed task log (tail is used).")
        ] = None,
        task_id: str | None = None,
    ) -> FindingsReport:
        """Root-cause and patch a broken DAG with AutoAir's LLM: validation problems (found
        automatically), import errors and/or a failed task's log (get_task_log). Returns
        findings with patches and a validated `proposed_spec`; nothing is saved."""
        client = require_llm()
        svc = services()
        report: FindingsReport = llm_call(
            lambda: fix_dag(
                spec,
                llm=client,
                catalog=svc.catalog,
                registry=svc.registry,
                policy=policy(),
                import_errors=import_errors,
                task_log=task_log,
                task_id=task_id,
                conventions=conventions_for(svc, user=current_user()),
            )
        )
        return report

    # ------------------------------------------------------------------ memory

    @mcp.tool(title="List memories", annotations=READ_ONLY, tags={"memory"})
    def list_memories(
        query: Annotated[str | None, Field(description="Keyword filter.")] = None,
        scope: Annotated[Scope | None, Field(description="global, team or user.")] = None,
        limit: Annotated[int, Field(ge=1, le=MAX_PAGE)] = 50,
        offset: Annotated[int, Field(ge=0)] = 0,
    ) -> MemoryList:
        """Team conventions and user preferences to follow when authoring DAGs (owners,
        naming, connections, retries...). Shows global memories plus the caller's team and
        user memories."""
        svc = services()
        items, total = memory_for(svc).list(
            scope=scope,
            query=query,
            visible_to=(svc.settings.team, current_user()),
            limit=limit,
            offset=offset,
        )
        return MemoryList(memories=items, **_page(total, offset, limit))

    @mcp.tool(title="Remember", annotations=MEMORY_WRITE, tags={"memory"})
    def remember(
        text: Annotated[
            str,
            Field(
                min_length=3,
                max_length=2000,
                description="One self-contained convention or preference, e.g. 'Warehouse "
                "loads use the snowflake_prod connection'.",
            ),
        ],
        scope: Annotated[
            Scope,
            Field(description="global (everyone), team (AUTOAIR_TEAM) or user (only you)."),
        ] = "user",
        kind: Literal["convention", "preference", "fact"] = "preference",
        tags: Annotated[list[str], Field(max_length=10)] = [],  # noqa: B006
    ) -> MemoryOutcome:
        """Store a convention/preference the user stated, so future sessions (and AutoAir's
        own AI features) follow it. Never store secrets. Duplicates are ignored."""
        svc = services()
        user = current_user()
        owner = {"global": None, "team": svc.settings.team, "user": user}[scope]
        try:
            item = memory_for(svc).add(
                text, scope=scope, owner=owner, kind=kind, tags=tags, created_by=user
            )
        except MemoryError as exc:
            return MemoryOutcome(ok=False, message=str(exc))
        return MemoryOutcome(ok=True, memory=item, message="remembered")

    @mcp.tool(
        title="Forget",
        tags={"memory"},
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=True,
            idempotent_hint=True,
            open_world_hint=False,
        ),
    )
    def forget(
        memory_id: Annotated[str, Field(description="id from list_memories.")],
    ) -> MemoryOutcome:
        """Delete a memory (e.g. an outdated convention)."""
        deleted = memory_for(services()).delete(memory_id)
        return MemoryOutcome(ok=deleted, message="deleted" if deleted else "no such memory")

    # ------------------------------------------------------------------ resources

    @mcp.resource(
        "autoair://schema/dagspec",
        name="dagspec-schema",
        title="DagSpec JSON schema",
        mime_type="application/schema+json",
    )
    def dagspec_schema_resource() -> str:
        return json.dumps(DagSpec.model_json_schema(), indent=1)

    @mcp.resource(
        "autoair://operators",
        name="operator-index",
        title="Operator catalog index",
        description="Every catalog operator: FQN, provider and a one-line summary.",
        mime_type="text/plain",
    )
    def operator_index_resource() -> str:
        return "\n".join(
            f"{s.fqn}\t{s.provider}\t{s.summary[:120]}"
            for s in services().catalog.summaries()
            if s.builder_supported
        )

    @mcp.resource(
        "autoair://operators/{fqn}/schema",
        name="operator-schema",
        title="Operator parameter schema",
        mime_type="application/json",
    )
    def operator_schema_resource(fqn: str) -> str:
        detail = services().catalog.get(fqn)
        if detail is None:
            raise ValueError(f"operator {fqn!r} is not in the catalog")
        return detail.model_dump_json(indent=1)

    @mcp.resource(
        "autoair://dags", name="dag-list", title="Saved DAGs", mime_type="application/json"
    )
    def dags_resource() -> str:
        return json.dumps([d.model_dump(mode="json") for d in spec_store().list()], indent=1)

    @mcp.resource(
        "autoair://dags/{dag_id}/spec",
        name="dag-spec",
        title="Saved DAG spec",
        mime_type="application/json",
    )
    def dag_spec_resource(dag_id: str) -> str:
        spec = spec_store().get(dag_id)
        if spec is None:
            raise ValueError(f"no AutoAir spec for {dag_id!r}")
        return spec.model_dump_json(indent=2, exclude_none=True, by_alias=True)

    @mcp.resource(
        "autoair://templates",
        name="template-list",
        title="DAG and fragment templates",
        mime_type="application/json",
    )
    def templates_resource() -> str:
        return json.dumps(
            [t.model_dump(mode="json") for t in services().templates.list()], indent=1
        )

    @mcp.resource(
        "autoair://templates/{template_id}",
        name="template",
        title="Template",
        mime_type="application/json",
    )
    def template_resource(template_id: str) -> str:
        template = services().templates.get(template_id)
        if template is None:
            raise ValueError(f"no template {template_id!r}")
        return template.model_dump_json(indent=1, exclude_none=True)

    @mcp.resource(
        "autoair://docs", name="doc-index", title="AutoAir guides", mime_type="text/plain"
    )
    def docs_index_resource() -> str:
        from autoair.ai.knowledge import knowledge_for

        docs = knowledge_for(services()).entries()
        return "\n".join(f"autoair://docs/{d.id}" for d in docs if d.enabled)

    @mcp.resource(
        "autoair://docs/{name}", name="doc", title="AutoAir guide", mime_type="text/markdown"
    )
    def doc_resource(name: str) -> str:
        from autoair.ai.knowledge import KnowledgeError, knowledge_for

        kb = knowledge_for(services())
        if not any(d.id == name and d.enabled for d in kb.entries()):
            raise ValueError(f"no guide {name!r} (see autoair://docs)")
        try:
            return kb.content(name)
        except KnowledgeError as exc:
            raise ValueError(str(exc)) from exc

    @mcp.resource(
        "autoair://memory",
        name="memory",
        title="Team conventions and preferences",
        mime_type="text/plain",
    )
    def memory_resource() -> str:
        lines = conventions_for(services(), user=current_user())
        return "\n".join(lines) or "(no conventions stored yet)"

    # ------------------------------------------------------------------ research & writers

    @mcp.tool(title="Search installed docs", annotations=READ_ONLY, tags={"research"})
    async def search_installed_docs(
        query: Annotated[str, Field(min_length=1, max_length=500)],
        scopes: Annotated[
            list[Literal["installed", "knowledge", "web", "docs"]] | None,
            Field(
                description="installed: docstrings, provider metadata and example DAGs of "
                "the installed Airflow providers; knowledge: AutoAir/Airflow guides; web: "
                "pages fetched with fetch_doc; docs: the indexed official documentation "
                "(knowledge base; kb_search has more filters)."
            ),
        ] = None,
        package: Annotated[
            str | None,
            Field(description="Only this distribution (e.g. apache-airflow-providers-amazon)."),
        ] = None,
        limit: Annotated[int, Field(ge=1, le=15)] = 6,
    ) -> dict[str, Any]:
        """Offline (air-gapped) documentation search that matches the versions INSTALLED on
        the AutoAir server. Use before writing code against a provider API."""
        tool = _research_tool("search_docs")
        hits = await tool.handler(
            tool.input_model(query=query, scopes=scopes, package=package, limit=limit)
        )
        return {"hits": hits}

    @mcp.tool(title="Read installed source", annotations=READ_ONLY, tags={"research"})
    def read_source(
        target: Annotated[
            str,
            Field(
                description="Module (airflow.providers.http.operators.http), module.Class, or "
                "a file under site-packages / the custom operator folders."
            ),
        ],
        symbol: Annotated[str | None, Field(description="Class or Class.method.")] = None,
    ) -> dict[str, Any]:
        """Source of an installed module, class or function (read-only; access outside
        site-packages and the configured folders is refused)."""
        from autoair.ai.research.local import ResearchError

        tool = _research_tool("read_source")
        try:
            result: dict[str, Any] = tool.handler(tool.input_model(target=target, symbol=symbol))
        except ResearchError as exc:
            raise ToolError(str(exc)) from exc
        return result

    @mcp.tool(title="List installed packages", annotations=READ_ONLY, tags={"research"})
    def list_installed_packages(
        pattern: Annotated[str | None, Field(description="Name filter, e.g. 'amazon'.")] = None,
        providers_only: bool = False,
        limit: Annotated[int, Field(ge=1, le=500)] = 100,
    ) -> dict[str, Any]:
        """Python distributions installed on the AutoAir server (Airflow providers marked)."""
        tool = _research_tool("list_installed_packages")
        return {
            "packages": tool.handler(
                tool.input_model(pattern=pattern, providers_only=providers_only, limit=limit)
            )
        }

    @mcp.tool(title="Get package info", annotations=READ_ONLY_REMOTE, tags={"research"})
    async def get_package_info(
        name: Annotated[str, Field(description="Distribution name.")],
        online: Annotated[
            bool, Field(description="Also query PyPI (only when online research is enabled).")
        ] = False,
    ) -> dict[str, Any]:
        """Installed version, summary and provider metadata (operators, hooks, sensors,
        connection types) of a distribution; optionally PyPI's latest release."""
        tool = _research_tool("get_package_info")
        result: dict[str, Any] = await tool.handler(tool.input_model(name=name, online=online))
        return result

    @mcp.tool(title="Fetch documentation page", annotations=READ_ONLY_REMOTE, tags={"research"})
    async def fetch_doc(
        url: Annotated[
            str,
            Field(
                description="https URL on an allowed site or a path under "
                "https://airflow.apache.org/docs/."
            ),
        ],
        max_chars: Annotated[int, Field(ge=500, le=60_000)] = 12_000,
    ) -> dict[str, Any]:
        """Fetch an allow-listed documentation page (online research mode only)."""
        from autoair.ai.net import UrlNotAllowed
        from autoair.ai.research.online import ResearchDisabled

        tool = _research_tool("fetch_doc", online_only=True)
        try:
            result: dict[str, Any] = await tool.handler(
                tool.input_model(url=url, max_chars=max_chars)
            )
        except (UrlNotAllowed, ResearchDisabled) as exc:
            raise ToolError(str(exc)) from exc
        return result

    @mcp.tool(title="Write code with AutoAir", annotations=LLM_TOOL, tags={"ai"})
    async def write_code(
        kind: Annotated[
            Literal[
                "task_body",
                "inline_function",
                "bash",
                "sql",
                "operator_config",
                "custom_operator",
                "dag_spec",
            ],
            Field(description="What to write."),
        ],
        description: Annotated[str, Field(min_length=1, max_length=20_000)],
        dialect: Annotated[str | None, Field(description="SQL dialect.")] = None,
        function_name: str | None = None,
        arguments: list[str] | None = None,
    ) -> dict[str, Any]:
        """AutoAir's specialised writers (uses AutoAir's own LLM): the code is built for the
        installed environment and checked (ruff / ShellCheck / catalog schema / leak scan)
        with automatic repair rounds. ``ok`` tells whether every check passed."""
        from autoair.ai.guardrails import GuardrailViolation, guard_input, pipeline_for
        from autoair.ai.writers import WriteRequest, run_writer

        client = require_llm()
        guards = pipeline_for(services())  # built once per server, external guardrails included
        try:
            description = await guard_input(guards, description)
        except GuardrailViolation as exc:
            raise ToolError(f"blocked by guardrails: {exc}") from exc
        request = WriteRequest(
            kind=kind,
            description=description,
            dialect=dialect,
            function_name=function_name,
            arguments=arguments or [],
        )
        try:
            result = await run_writer(
                request,
                llm=client.guarded(guards),
                services=services(),
                guardrails=guards,
            )
        except LLMError as exc:
            raise ToolError(f"LLM error: {exc}") from exc
        data: dict[str, Any] = result.model_dump(mode="json", exclude_none=True)
        return data

    # ------------------------------------------------------------------ knowledge base

    from autoair.kb.mcp import register_kb_tools

    register_kb_tools(mcp, services)

    # ------------------------------------------------------------------ AutoAir documentation

    from autoair.docs.mcp import register_docs_tools

    register_docs_tools(mcp)

    def _research_tool(name: str, *, online_only: bool = False) -> Any:
        from autoair.ai.research.tools import research_tools

        # The caller's model (query rewriting / LLM reranking): never the central one by
        # accident in per-user mode.
        tools = {t.name: t for t in research_tools(services(), llm=get_llm())}
        if name not in tools:
            if online_only:
                raise ToolError(
                    "online research is disabled on this server (air-gapped); use "
                    "search_installed_docs"
                )
            raise ToolError(f"research tool {name} unavailable")
        return tools[name]

    # ------------------------------------------------------------------ prompts

    @mcp.prompt(title="Build a DAG from a description", tags={"author"})
    def build_dag_from_description(description: str) -> str:
        """Step-by-step instructions to author, validate, review and save a DAG."""
        return (
            "Build an Apache Airflow 3 DAG with AutoAir for this request:\n\n"
            f"{description}\n\n"
            "Steps:\n"
            "1. Read autoair://memory (team conventions) and autoair://docs/dagspec-authoring.\n"
            "2. Find operators with search_knowledge and read their parameters with "
            "get_operator_schema. Prefer provider operators over python_task nodes.\n"
            "3. Use list_connections / list_variables to reference existing connection ids.\n"
            "4. Draft the complete DagSpec (unique node ids, task_id per task, edges for "
            "dependencies, schedule object, retries and execution timeouts, no positions).\n"
            "5. Call validate_dag_spec and fix every error (apply_spec_patch for small edits) "
            "until ok is true.\n"
            "6. Run review_dag_spec and scan_dag_leaks; apply the relevant patches.\n"
            "7. Summarise the DAG for the user (generate_code if they want to see Python).\n"
            "8. Only after the user agrees, call save_dag_spec with confirm=true."
        )

    @mcp.prompt(name="review_dag", title="Review a DAG", tags={"review"})
    def review_dag_prompt(dag_id: str) -> str:
        """Production-readiness review of a saved DAG."""
        return (
            f"Review the AutoAir DAG {dag_id!r} for production readiness.\n"
            f"1. get_dag_spec(dag_id={dag_id!r}).\n"
            "2. review_dag_spec and scan_dag_leaks on it; read autoair://memory for team "
            "conventions.\n"
            "3. Add your own analysis of what the rules cannot see: business logic, "
            "dependency structure, trigger rules, XCom data volume, idempotency of SQL/code.\n"
            "4. Report findings grouped by severity with node ids and concrete fixes; offer to "
            "apply them with apply_spec_patch and save only after confirmation."
        )

    @mcp.prompt(title="Fix a DAG import error", tags={"fix"})
    def fix_import_error(dag_id: str, error: str = "") -> str:
        """Diagnose and fix a DAG that fails to import or validate."""
        return (
            f"The AutoAir DAG {dag_id!r} fails"
            + (f" with:\n```\n{error}\n```\n" if error else ".\n")
            + "1. get_dag_spec and validate_dag_spec(import_check=true).\n"
            "2. Find the root cause (one mistake often causes several errors); read the "
            "operator schema of the failing node with get_operator_schema.\n"
            "3. Fix it with apply_spec_patch until validation and the import check pass "
            "(fix_dag_spec can propose patches when AutoAir has an LLM).\n"
            "4. Explain the cause and the fix, then save with confirm=true once approved."
        )

    @mcp.prompt(title="Optimise DAG parameters", tags={"tuning"})
    def optimize_parameters(dag_id: str) -> str:
        """Data-driven tuning of retries, timeouts and concurrency."""
        return (
            f"Tune the parameters of the AutoAir DAG {dag_id!r}.\n"
            "1. get_dag_spec, then optimize_dag_parameters on it (uses Airflow run history).\n"
            "2. Check the statistics behind each suggestion (list_dag_runs, get_task_log for "
            "failures); keep only changes the data supports.\n"
            "3. Present a table: task, setting, current -> proposed, evidence.\n"
            "4. Apply approved changes with apply_spec_patch and save with confirm=true."
        )

    @mcp.prompt(title="Scan a DAG for leaks", tags={"security"})
    def scan_for_leaks(dag_id: str) -> str:
        """Secrets / credential exposure / unbounded resources audit."""
        return (
            f"Audit the AutoAir DAG {dag_id!r} for secret leaks and unbounded resources.\n"
            "1. get_dag_spec, then scan_dag_leaks.\n"
            "2. For every hard-coded secret: tell the user to rotate it and move it to an "
            "Airflow connection or secrets backend; propose the spec change.\n"
            "3. Check code and commands for logging or returning credentials and for missing "
            "timeouts.\n"
            "4. Never print a full secret value."
        )

    @mcp.prompt(title="Explain a DAG", tags={"explain"})
    def explain_dag(dag_id: str) -> str:
        """Explain what a saved AutoAir DAG does."""
        try:
            spec = spec_store().get(dag_id)
        except Exception:
            spec = None
        body = (
            spec.model_dump_json(indent=2, exclude_none=True, by_alias=True)
            if spec is not None
            else f"(spec not found - call get_dag_spec with dag_id={dag_id!r})"
        )
        return (
            f"Explain the Airflow DAG {dag_id!r} to a data engineer: purpose, schedule, "
            "the tasks in execution order, data passed between tasks (XCom), required "
            "connections and anything risky (retries, timeouts, privileged code). Use "
            "get_dag_status to report its current state.\n\nDagSpec:\n" + body
        )

    @mcp.prompt(title="Convert an existing DAG", tags={"author"})
    def convert_existing_dag(python_source: str) -> str:
        """Convert a hand-written Airflow DAG file into an AutoAir DagSpec."""
        return (
            "Convert this Airflow DAG file into an AutoAir DagSpec. Try import_python_dag "
            "first; if it is unavailable, map each operator to its catalog FQN "
            "(search_knowledge / get_operator_schema), TaskFlow functions to python_task nodes "
            "(code = function body), task groups to task_group nodes, `>>` dependencies to "
            "edges and the DAG arguments to the spec fields. Validate with validate_dag_spec "
            "until ok, then show the result; save only after the user confirms.\n\n"
            "```python\n" + python_source + "\n```"
        )

    if not ai_runtime_available():
        # Without autoair[ai] the tools that need AutoAir's own LLM never show up (also over
        # stdio); features.ai is off as well, which hides the other AI-tagged tools.
        mcp.disable(names=set(LLM_ONLY_TOOLS))
    return mcp


# --------------------------------------------------------------------------- CLI


def _build_default_services() -> Services:
    from autoair.api.app import build_services
    from autoair.auth import AuthBackend
    from autoair.settings import Settings

    services = build_services(Settings(), AuthBackend(name="none"))
    from autoair.ai.config import activate_ai
    from autoair.ai.prompts import configure

    configure(activate_ai(services.settings).prompts.override_dir)
    if getattr(services, "llm", None) is None:
        from autoair.ai.llm import build_llm

        client = build_llm(services.settings)
        if client is not None:
            services.llm = client
    return services


def main(argv: list[str] | None = None) -> None:
    """``autoair-mcp``: serve AutoAir over MCP (stdio by default)."""
    parser = argparse.ArgumentParser(
        prog="autoair-mcp",
        description="AutoAir MCP server. Configure with AUTOAIR_* environment variables "
        f"(AUTOAIR_DAGS_FOLDER, AUTOAIR_AIRFLOW_URL, {AIRFLOW_TOKEN_ENV}, "
        "AUTOAIR_AI__PROVIDER, AUTOAIR_ANTHROPIC_API_KEY, ...). For an authenticated HTTP "
        "endpoint use `autoair mcp-serve --http`.",
    )
    parser.add_argument("--http", action="store_true", help="serve streamable HTTP instead")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--path", default="/mcp", help="HTTP endpoint path (default /mcp)")
    args = parser.parse_args(argv)

    server = create_mcp_server(_build_default_services)
    if args.http:
        server.run(
            "http",
            show_banner=False,
            host=args.host,
            port=args.port,
            path=args.path,
            stateless_http=True,
            json_response=True,
        )
    else:
        server.run("stdio", show_banner=False)


if __name__ == "__main__":  # pragma: no cover
    main()
