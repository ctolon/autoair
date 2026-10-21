"""AutoAir node kind ``{{ cookiecutter.node_name }}``.

The handler turns a node's validated ``config`` into the Python expression that creates the
task in the generated DAG file. Replace the example (a ``BashOperator`` echoing a message)
with your own operator; keep emitting AST so user values cannot inject code.
"""

from __future__ import annotations

import ast
import shlex
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from autoair.codegen.context import CodegenContext
    from autoair.spec import CustomNode


class {{ cookiecutter.class_name }}Config(BaseModel):
    """The node's settings. The JSON Schema of this model renders the inspector form
    (titles, descriptions, defaults, enums, min/max...)."""

    model_config = ConfigDict(extra="forbid")

    message: str = Field(
        default="hello", min_length=1, max_length=500, description="Text the task prints."
    )
    times: int = Field(default=1, ge=1, le=10, description="How often to print it.")


class {{ cookiecutter.class_name }}:
    """Implements ``autoair.registry.NodeKindHandler``."""

    # --- required
    name: ClassVar[str] = "{{ cookiecutter.node_name }}"
    title: ClassVar[str] = "{{ cookiecutter.title }}"
    description: ClassVar[str] = "{{ cookiecutter.description }}"
    category: ClassVar[str] = "{{ cookiecutter.category }}"
    config_model: ClassVar[type[BaseModel]] = {{ cookiecutter.class_name }}Config

    # --- optional UI hints
    icon: ClassVar[str | None] = "{{ cookiecutter.icon }}"
    color: ClassVar[str | None] = "#0ea5e9"
    example_config: ClassVar[dict[str, Any]] = {"message": "hello from AutoAir", "times": 1}
    output_schema: ClassVar[dict[str, Any]] = {
        "type": "string",
        "description": "The last line printed (BashOperator pushes it as its XCom).",
    }
    doc_url: ClassVar[str | None] = None

    def validate(self, node: CustomNode, config: BaseModel) -> list[str]:
        """Extra checks beyond the config model; each message is reported as an error."""
        assert isinstance(config, {{ cookiecutter.class_name }}Config)
        if "\n" in config.message:
            return ["message must be a single line"]
        return []

    def build(self, node: CustomNode, config: BaseModel, ctx: CodegenContext) -> ast.expr:
        """The task construction expression (``BashOperator(task_id=..., ...)``)."""
        assert isinstance(config, {{ cookiecutter.class_name }}Config)
        operator = ctx.import_name("airflow.providers.standard.operators.bash", "BashOperator")
        command = "; ".join(["echo " + shlex.quote(config.message)] * config.times)
        return ast.Call(
            func=operator,
            args=[],
            keywords=[
                *ctx.task_kwargs(node),  # task_id, retries, pool, owner... from the inspector
                ast.keyword(arg="bash_command", value=ast.Constant(command)),
            ],
        )
