"""Runtime loader: build an Airflow DAG straight from a ``*.autoair.json`` spec.

The loader compiles the exact same AST the code generator emits, so a DAG built in
``loader`` mode is identical to its ``python`` mode counterpart by construction.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from autoair.codegen import build_module
from autoair.registry import HandlerRegistry
from autoair.spec import DagSpec

SPEC_SUFFIX = ".autoair.json"

STUB_TEMPLATE = '''"""AutoAir loader stub for the airflow DAG '{dag_id}' (output mode: loader).

The DAG is built at parse time from {dag_id}{suffix} next to this file.
Edit the DAG in the AutoAir builder; this file is managed automatically.
"""

from autoair.loader import load_dag

dag = load_dag(__file__)
'''


def spec_path_for(dag_file: str | Path) -> Path:
    path = Path(dag_file)
    return path.with_name(path.name.removesuffix(".py") + SPEC_SUFFIX)


def build_dag(
    spec: DagSpec, *, filename: str = "<autoair>", registry: HandlerRegistry | None = None
) -> Any:
    """Return the ``airflow.sdk.DAG`` described by ``spec``."""
    module = build_module(spec, registry)
    code = compile(module, filename, "exec")
    namespace: dict[str, Any] = {"__name__": f"autoair_dag_{spec.dag_id}", "__file__": filename}
    exec(code, namespace)  # noqa: S102 - AST built from a validated spec
    return namespace["dag"]


def load_dag(
    dag_file: str | Path,
    registry: HandlerRegistry | None = None,
    environment: str | None = None,
) -> Any:
    """Load the spec that sits next to ``dag_file`` (the stub) and build its DAG.

    The environment overlay comes from ``environment`` or the ``AUTOAIR_ENVIRONMENT``
    variable of the dag-processor / worker (unknown environments are ignored).
    """
    path = spec_path_for(dag_file)
    spec = DagSpec.model_validate_json(path.read_text(encoding="utf-8"))
    env = environment or os.environ.get("AUTOAIR_ENVIRONMENT")
    if env and env in spec.environments:
        spec = spec.with_environment(env)
    return build_dag(spec, filename=str(path), registry=registry)


def render_stub(dag_id: str) -> str:
    return STUB_TEMPLATE.format(dag_id=dag_id, suffix=SPEC_SUFFIX)
