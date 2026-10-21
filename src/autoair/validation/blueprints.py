"""Checks of Astronomer Blueprint steps (``kind: blueprint`` nodes)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from autoair.catalog.schema_check import validate_value
from autoair.spec import BlueprintNode, DagSpec, TaskGroupNode

if TYPE_CHECKING:
    from autoair.blueprints.catalog import BlueprintCatalog
    from autoair.validation import Problem

RUNTIME_HINT = (
    "the Airflow environment needs airflow-blueprint and the module {module!r} "
    "(the blueprint templates) to parse this DAG"
)


def _has_tag(value: Any) -> bool:
    if isinstance(value, dict):
        return "$type" in value or any(_has_tag(v) for v in value.values())
    if isinstance(value, list):
        return any(_has_tag(v) for v in value)
    return False


def _schema_value(config: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    """``Blueprint.get_schema()`` writes ``X | None`` as plain ``X`` (optionality is left to
    ``required``), so an explicit null of an optional field is valid for Pydantic but not
    for the schema: check it as unset."""
    required = set(schema.get("required") or [])
    return {k: v for k, v in config.items() if v is not None or k in required}


def blueprint_problems(spec: DagSpec, catalog: BlueprintCatalog | None) -> list[Problem]:
    from autoair.validation import _err

    nodes = spec.blueprint_nodes()
    if not nodes:
        return []
    problems: list[Problem] = []
    if catalog is not None and not catalog.enabled():
        return [
            _err(
                "blueprint_disabled",
                "blueprint steps are switched off (features.blueprints)",
                node.id,
                "kind",
            )
            for node in nodes
        ]
    groups = {g.id: g for g in spec.group_nodes()}
    if spec.output_mode == "yaml":
        problems.append(
            _err(
                "blueprint_output_mode",
                "blueprint steps cannot be expressed in YAML Dags: use the python or loader "
                "output mode",
                nodes[0].id,
                "output_mode",
            )
        )
    for node in nodes:
        parent = groups.get(node.parent_group or "")
        if isinstance(parent, TaskGroupNode) and parent.loop is not None:
            problems.append(
                _err(
                    "blueprint_loop",
                    "blueprint steps cannot be placed in a loop group",
                    node.id,
                    "parent_group",
                )
            )
        tagged = [k for k, v in node.config.items() if _has_tag(v)]
        for key in tagged:
            problems.append(
                _err(
                    "blueprint_config",
                    "blueprint config values must be plain JSON (no XCom references, "
                    "timedeltas or other tagged values)",
                    node.id,
                    f"config.{key}",
                )
            )
        problems += _catalog_problems(node, catalog, skip_schema=bool(tagged))
    first: dict[str, str] = {}
    for node in nodes:
        first.setdefault(node.class_path.rpartition(".")[0], node.id)
    for module, node_id in first.items():
        problems.append(
            _err("blueprint_runtime", RUNTIME_HINT.format(module=module), node_id, severity="info")
        )
    return problems


def _catalog_problems(
    node: BlueprintNode, catalog: BlueprintCatalog | None, *, skip_schema: bool
) -> list[Problem]:
    from autoair.validation import _err

    module = node.class_path.rpartition(".")[0]
    if catalog is None:
        return []
    detail = catalog.get(node.blueprint, node.version)
    if detail is None:
        known = sorted({d.version for d in catalog.data().blueprints if d.name == node.blueprint})
        more = f" (available versions: {', '.join(map(str, known))})" if known else ""
        return [
            _err(
                "blueprint_unknown",
                f"blueprint {node.blueprint!r} v{node.version} is not in the blueprint "
                f"catalog{more}; install its package or configure blueprints.template_dirs",
                node.id,
                "blueprint",
            )
        ]
    problems: list[Problem] = []
    # The catalog is the import allowlist: generated code imports class_path.
    if detail.class_path != node.class_path:
        problems.append(
            _err(
                "blueprint_class",
                f"{node.blueprint!r} v{node.version} is {detail.class_path}, not {node.class_path}",
                node.id,
                "class_path",
            )
        )
    if node.config_class is not None and node.config_class != detail.config_class:
        problems.append(
            _err(
                "blueprint_class",
                f"the config model of {node.blueprint!r} v{node.version} is not "
                f"{node.config_class!r} in {module}",
                node.id,
                "config_class",
            )
        )
    if detail.config_schema is None:
        reason = detail.schema_error or "schema unavailable"
        problems.append(
            _err(
                "blueprint_schema",
                f"config not checked: {reason}",
                node.id,
                "config",
                severity="warning",
            )
        )
    elif not skip_schema:
        for msg in validate_value(
            detail.config_schema, _schema_value(node.config, detail.config_schema)
        ):
            problems.append(_err("blueprint_config", msg, node.id, "config"))
    if not detail.latest:
        problems.append(
            _err(
                "blueprint_outdated",
                f"a newer version of {node.blueprint!r} is available",
                node.id,
                "version",
                severity="info",
            )
        )
    return problems
