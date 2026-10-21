"""Tests of the ``{{ cookiecutter.node_name }}`` node kind (run with ``pytest``)."""

from __future__ import annotations

import ast

from autoair.codegen import render
from autoair.registry import HandlerRegistry, default_registry
from autoair.spec import DagSpec
from autoair.validation import validate_spec

from {{ cookiecutter.package_name }} import {{ cookiecutter.class_name }}


def _spec(config: dict[str, object]) -> DagSpec:
    return DagSpec.model_validate(
        {
            "dag_id": "example_{{ cookiecutter.node_name }}",
            "nodes": [
                {
                    "kind": "custom",
                    "id": "n1",
                    "task_id": "step",
                    "handler": "{{ cookiecutter.node_name }}",
                    "config": config,
                }
            ],
        }
    )


def _registry() -> HandlerRegistry:
    registry = HandlerRegistry()
    registry.register({{ cookiecutter.class_name }}())
    return registry


def test_codegen_compiles() -> None:
    source = render(_spec({"message": "it's done", "times": 2}), _registry(), fmt=False)
    ast.parse(source)
    assert "BashOperator(task_id='step'" in source


def test_values_cannot_inject_code() -> None:
    evil = "x')\nimport os; os.system('id')  #"
    source = render(_spec({"message": evil}), _registry(), fmt=False)
    tree = ast.parse(source)
    assert not any(
        isinstance(n, ast.Import) and any(a.name == "os" for a in n.names) for n in tree.body
    )


def test_validation() -> None:
    registry = _registry()
    ok = validate_spec(_spec({"message": "hi"}), registry=registry)
    assert not [p for p in ok.problems if p.severity == "error"], ok.problems
    bad = validate_spec(_spec({"message": "a\nb"}), registry=registry)
    assert any(p.code == "config" for p in bad.problems)


def test_discovered_through_entry_point() -> None:
    """Needs the package installed (pip install -e .)."""
    assert "{{ cookiecutter.node_name }}" in default_registry()
