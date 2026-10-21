"""AutoAir capabilities exposed to the agent as typed, read-only tools."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Literal, TypeVar

from pydantic import BaseModel, Field

from autoair.ai.corpus import retriever_for
from autoair.ai.problems import check_spec
from autoair.ai.rules import review_rules, scan_leaks
from autoair.ai.tools import AgentTool
from autoair.ai.tuning import fetch_history, history_stats
from autoair.ai.writers import python_code_enabled
from autoair.gateway import AirflowGateway
from autoair.validation import policy_from_settings

T = TypeVar("T")
GatewayFactory = Callable[[], AirflowGateway]


class SearchInput(BaseModel):
    query: str = Field(
        description="What you are looking for, e.g. 'load csv from s3 to snowflake'."
    )
    kinds: list[Literal["operator", "doc", "spec", "template"]] | None = Field(
        default=None, description="Restrict to these corpora."
    )
    limit: int = Field(default=6, ge=1, le=15)


class OperatorInput(BaseModel):
    fqn: str = Field(description="Fully qualified operator class name.")


class SpecInput(BaseModel):
    spec: dict[str, Any] = Field(description="A complete DagSpec document.")


class DagIdInput(BaseModel):
    dag_id: str


class RunsInput(BaseModel):
    dag_id: str
    limit: int = Field(default=10, ge=1, le=50)


class RunInput(BaseModel):
    dag_id: str
    run_id: str


class LogInput(BaseModel):
    dag_id: str
    run_id: str
    task_id: str
    try_number: int = Field(default=1, ge=1)
    map_index: int = -1
    tail_chars: int = Field(default=8_000, ge=500, le=30_000)


class ImportInput(BaseModel):
    source: str = Field(description="Python source of an Airflow DAG file.", max_length=200_000)


def _compact_operator(detail: Any) -> dict[str, Any]:
    props = detail.params_schema.get("properties") or {}
    params = {}
    for name, schema in props.items():
        entry = {k: schema[k] for k in ("type", "enum", "default") if k in schema}
        desc = str(schema.get("description") or "")
        if desc:
            entry["description"] = desc[:160]
        if "x-autoair-conn-type" in schema:
            entry["conn_type"] = schema["x-autoair-conn-type"]
        params[name] = entry
    return {
        "fqn": detail.fqn,
        "summary": detail.summary,
        "required": detail.params_schema.get("required", []),
        "params": params,
        "template_fields": detail.template_fields,
        "is_sensor": detail.is_sensor,
        "deferrable": detail.deferrable_param,
        "deprecated": detail.deprecated,
        "docstring": (detail.docstring or "")[:1200],
    }


def build_toolbox(
    services: Any,
    *,
    gateway_factory: GatewayFactory | None = None,
) -> list[AgentTool]:
    """Read-only tools over ``services`` (Airflow tools only with a gateway factory)."""
    settings = services.settings
    policy = policy_from_settings(settings)

    async def run(coro_fn: Callable[[AirflowGateway], Awaitable[T]]) -> T:
        assert gateway_factory is not None
        async with gateway_factory() as gw:
            return await coro_fn(gw)

    async def search(args: SearchInput) -> Any:
        hits = await retriever_for(services).asearch(args.query, args.limit, args.kinds)
        return [h.model_dump(exclude={"meta"}) for h in hits]

    def get_operator(args: OperatorInput) -> Any:
        detail = services.catalog.get(args.fqn)
        if detail is None:
            raise ValueError(f"operator {args.fqn!r} is not in the catalog (use search_catalog)")
        return _compact_operator(detail)

    def validate(args: SpecInput) -> Any:
        check = check_spec(
            args.spec, catalog=services.catalog, registry=services.registry, policy=policy
        )
        return {
            "ok": check.ok,
            "problems": [p.model_dump(exclude_none=True) for p in check.problems[:40]],
        }

    def lint(args: SpecInput) -> Any:
        check = check_spec(args.spec, catalog=None, registry=services.registry)
        if check.spec is None:
            return {"error": "not a valid DagSpec; call validate_spec first"}
        findings = [
            *review_rules(check.spec, services.catalog),
            *scan_leaks(check.spec, services.catalog),
        ]
        return [
            {k: v for k, v in f.model_dump(exclude={"patch", "id", "source"}).items() if v}
            for f in findings[:40]
        ]

    def render_code(args: SpecInput) -> Any:
        from autoair.codegen import render

        check = check_spec(args.spec, catalog=None, registry=services.registry)
        if check.spec is None:
            return {"error": "not a valid DagSpec; call validate_spec first"}
        return render(check.spec, services.registry)

    def dry_run(args: SpecInput) -> Any:
        check = check_spec(
            args.spec,
            catalog=services.catalog,
            registry=services.registry,
            policy=policy,
            import_check=True,
        )
        return {
            "would_deploy": check.ok,
            "problems": [p.model_dump(exclude_none=True) for p in check.problems[:40]],
            "import_errors": list(check.import_check.import_errors) if check.import_check else [],
            "note": "dry run only - nothing was written",
        }

    def saved_spec(args: DagIdInput) -> Any:
        from autoair.mcp.store import store_for

        spec = store_for(services).get(args.dag_id)
        if spec is None:
            raise ValueError(f"no AutoAir spec for {args.dag_id!r}")
        return spec.model_dump(mode="json", exclude_none=True, by_alias=True)

    def import_dag(args: ImportInput) -> Any:
        from autoair.importer.python_dag import import_python_source

        result = import_python_source(args.source, timeout=60)
        return {
            "import_errors": result.import_errors,
            "dags": [
                {
                    "dag_id": d.dag_id,
                    "warnings": d.warnings,
                    "error": d.error,
                    "spec": d.spec.model_dump(mode="json", exclude_none=True, by_alias=True)
                    if d.spec
                    else None,
                }
                for d in result.dags
            ],
        }

    tools = [
        AgentTool(
            "search_catalog",
            "Hybrid search over the operator catalog, AutoAir/Airflow docs, saved DAGs and "
            "templates. Use it first to find operators by what they do.",
            SearchInput,
            search,
        ),
        AgentTool(
            "get_operator",
            "Parameters (types, required, defaults, connection types), templated fields "
            "and docs of one operator. Read it before using an operator in a spec.",
            OperatorInput,
            get_operator,
        ),
        AgentTool(
            "validate_spec",
            "Validate a DagSpec (schema, graph, operator params, policy). Problems carry "
            "JSON pointer paths.",
            SpecInput,
            validate,
        ),
        AgentTool(
            "lint_spec",
            "Best-practice, Airflow 3, secret-leak and timeout checks of a valid DagSpec.",
            SpecInput,
            lint,
        ),
        AgentTool(
            "render_code",
            "The Python DAG file AutoAir generates for a valid DagSpec.",
            SpecInput,
            render_code,
        ),
        AgentTool(
            "deploy_dry_run",
            "Everything a deploy checks (validation + importing the generated file in a "
            "sandboxed DagBag) without writing anything. Slower; use before proposing.",
            SpecInput,
            dry_run,
        ),
        AgentTool(
            "get_saved_spec",
            "The saved DagSpec of an AutoAir DAG.",
            DagIdInput,
            saved_spec,
        ),
    ]
    if python_code_enabled(settings, default=False):
        tools.append(
            AgentTool(
                "import_python_dag",
                "Convert an Airflow DAG Python file into DagSpec(s) (runs the file in an "
                "isolated subprocess). Only for source the user provided.",
                ImportInput,
                import_dag,
            )
        )
    if gateway_factory is not None:

        async def runs(args: RunsInput) -> Any:
            items = await run(lambda gw: gw.dag_runs(args.dag_id, limit=args.limit))
            return [r.model_dump(exclude_none=True, exclude={"conf"}) for r in items]

        async def task_instances(args: RunInput) -> Any:
            items = await run(lambda gw: gw.task_instances(args.dag_id, args.run_id))
            return [t.model_dump(exclude_none=True) for t in items]

        async def task_log(args: LogInput) -> Any:
            text = await run(
                lambda gw: gw.task_log(
                    args.dag_id, args.run_id, args.task_id, args.try_number, args.map_index
                )
            )
            return text[-args.tail_chars :]

        async def run_stats(args: DagIdInput) -> Any:
            history = await run(lambda gw: fetch_history(gw, args.dag_id))
            return history_stats(args.dag_id, history)

        tools += [
            AgentTool(
                "list_dag_runs",
                "Recent runs of a DAG in Airflow (state, dates).",
                RunsInput,
                runs,
            ),
            AgentTool(
                "list_task_instances",
                "Task instances of one DAG run (state, try_number, duration).",
                RunInput,
                task_instances,
            ),
            AgentTool(
                "get_task_log",
                "Tail of a task instance's log (find run_id/try_number with the run tools).",
                LogInput,
                task_log,
            ),
            AgentTool(
                "run_statistics",
                "Duration percentiles, failures and retries per task over recent runs.",
                DagIdInput,
                run_stats,
            ),
        ]
    return tools


def gateway_factory_for(
    base_url: str,
    headers: Mapping[str, str],
    cookies: Mapping[str, str] | None = None,
    timeout: float = 15,
) -> GatewayFactory:
    def make() -> AirflowGateway:
        return AirflowGateway(
            base_url, headers=dict(headers), cookies=dict(cookies or {}), timeout=timeout
        )

    return make
