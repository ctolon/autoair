---
title: Custom node kinds
description: Add node kinds with their own configuration form and code generation through the autoair.node_kinds entry point, the custom node template and the frontend registry.
sidebar_label: Custom node kinds
---

Operators from the catalog cover most needs. A **custom node kind** is for the cases where you want a purpose-built node instead: a simplified form over a complex operator, a company-specific pattern that expands to a particular operator call, or defaults and validation that a raw operator form cannot express.

A node kind is a Python class (a *handler*) registered through the `autoair.node_kinds` entry point. It declares a Pydantic configuration model, whose JSON Schema renders the inspector form, and a `build()` method that returns the task as a Python **AST expression**. Optionally, the frontend can customise how the node looks.

## The handler contract

`autoair.registry.NodeKindHandler`:

| Member | Required | Type | Purpose |
|---|---|---|---|
| `name` | yes | `ClassVar[str]` | The node kind, stored in the spec as `handler`. Must match `^[a-z0-9_.-]+$` |
| `title` | yes | `ClassVar[str]` | Palette and inspector title |
| `description` | yes | `ClassVar[str]` | Palette description |
| `category` | yes | `ClassVar[str]` | Palette category |
| `config_model` | yes | `ClassVar[type[BaseModel]]` | Pydantic model of the node's settings; its JSON Schema renders the form |
| `build(node, config, ctx)` | yes | `-> ast.expr` | The expression that creates the task, for example an operator call |
| `icon` | no | `str` | `builtin:<logo>` or `custom:<name>` |
| `color` | no | `str` | Canvas accent, `#rrggbb` |
| `example_config` | no | `dict` | Configuration prefilled when the node is inserted |
| `output_schema` | no | `dict` | JSON Schema of the task's XCom return value (documentation and typed XCom hints) |
| `doc_url` | no | `str` | Documentation link in the inspector; only `https://` URLs are passed on |
| `validate(node, config)` | no | `-> list[str]` | Extra validation messages, reported as errors |

Invalid optional hints (an `icon` or `color` that does not match its pattern, a non-`https` `doc_url`) are dropped with a warning in the log, never passed to the UI.

`GET /api/catalog/node-kinds` returns what the UI knows about each registered kind: name, title, description, category, the config JSON Schema and the hints.

### The codegen context

`build()` receives a `CodegenContext` (`autoair.codegen.context`). The helpers you will use most:

| Helper | Returns |
|---|---|
| `ctx.import_name(module, name)` | An `ast.Name` for `name`, adding `from module import name` to the file (renamed on clashes) |
| `ctx.task_kwargs(node)` | The common task keyword arguments from the inspector, each only when set: `task_id`, `task_group`, `trigger_rule`, `retries`, `retry_delay`, `execution_timeout`, `pool`, `pool_slots`, `queue`, `owner`, `doc_md`, `outlets`, `inlets`, `priority_weight`, `weight_rule`, `task_display_name`, `executor`, `max_active_tis_per_dagrun`, `depends_on_past`, `retry_exponential_backoff`, `executor_config`, `map_index_template`, `retry_policy` and the `on_success_callback`, `on_failure_callback`, `on_retry_callback`, `on_execute_callback`, `on_skipped_callback` notifiers |
| `ctx.value(value)` | A literal AST expression for a JSON value (tagged values such as XCom references included) |
| `ctx.xcom_expr(node_id, key)` | A reference to another task's output |
| `ctx.sdk(name)` | An authoring symbol imported from where the target Airflow version defines it |

## Writing a handler

`autoair new-node echo` generates this handler (shortened; the class names are derived from the node name):

```python
from __future__ import annotations

import ast
import shlex
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from autoair.codegen.context import CodegenContext
    from autoair.spec import CustomNode


class EchoHandlerConfig(BaseModel):
    """The node's settings; the JSON Schema of this model renders the inspector form."""

    model_config = ConfigDict(extra="forbid")

    message: str = Field(default="hello", min_length=1, max_length=500, description="Text the task prints.")
    times: int = Field(default=1, ge=1, le=10, description="How often to print it.")


class EchoHandler:
    name: ClassVar[str] = "echo"
    title: ClassVar[str] = "Echo"
    description: ClassVar[str] = "Print a message"
    category: ClassVar[str] = "custom"
    config_model: ClassVar[type[BaseModel]] = EchoHandlerConfig

    icon: ClassVar[str | None] = "builtin:python"
    color: ClassVar[str | None] = "#0ea5e9"
    example_config: ClassVar[dict[str, Any]] = {"message": "hello from AutoAir", "times": 1}
    output_schema: ClassVar[dict[str, Any]] = {"type": "string"}

    def validate(self, node: CustomNode, config: BaseModel) -> list[str]:
        assert isinstance(config, EchoHandlerConfig)
        return ["message must be a single line"] if "\n" in config.message else []

    def build(self, node: CustomNode, config: BaseModel, ctx: CodegenContext) -> ast.expr:
        assert isinstance(config, EchoHandlerConfig)
        operator = ctx.import_name("airflow.providers.standard.operators.bash", "BashOperator")
        command = "; ".join(["echo " + shlex.quote(config.message)] * config.times)
        return ast.Call(
            func=operator,
            args=[],
            keywords=[
                *ctx.task_kwargs(node),
                ast.keyword(arg="bash_command", value=ast.Constant(command)),
            ],
        )
```

Register it:

```toml
[project.entry-points."autoair.node_kinds"]
echo = "autoair_node_echo.handler:EchoHandler"
```

The entry-point key is informational; the handler's `name` is the node kind. The entry point may reference a class (instantiated without arguments) or an instance.

### Rules

- **Emit AST only.** Build values with `ast.Constant`, `ctx.value(...)` and `ctx.import_name(...)`, never by formatting source text. User-entered values then cannot inject code; the template's tests include an injection test.
- **No I/O in `build()`.** It runs in the AutoAir server during code generation and validation; it must not read files, call services or execute user input.
- **Forbid unknown fields** in the config model (`extra="forbid"`), so typos in stored specs are reported.
- **The generated DAG imports what `build()` references.** Install the referenced provider (and your package, if the DAG imports from it) on the DAG processor and workers.
- **Custom nodes do not support dynamic task mapping**; validation reports `mapping` errors for them.

## In the spec

A custom node is stored as `kind: "custom"` with the handler name and its config:

```json
{
  "kind": "custom",
  "id": "n1",
  "task_id": "say_hello",
  "handler": "echo",
  "config": {"message": "it's done", "times": 2},
  "retries": 1
}
```

All common task fields apply; they are listed under [Task fields](/docs/extending/spec-format#task-fields) in the spec format.

Validation of a custom node, in order:

1. `unknown_handler`: no installed extension provides the node kind (for example the package is missing on this replica);
2. `config`: the config does not validate against `config_model` (first line of the Pydantic error);
3. `config`: each message returned by `validate()`; a hook that raises is reported as one message (`<name>: validation hook failed: ...`) instead of breaking validation.

## The project template

`autoair new-node` scaffolds a complete package: handler, `pyproject.toml` with the entry point, README and tests.

```bash
autoair new-node http_get --title "HTTP GET" --description "Call an HTTP endpoint" --category integrations
cd autoair-node-http-get
pip install -e '.[test]'      # into the environment AutoAir runs in
pytest
```

| Option | Default | Used for |
|---|---|---|
| `NAME` | (required) | Handler `name` and entry-point key: a lowercase letter followed by up to 60 lowercase letters, digits or `_` |
| `--title`, `--description`, `--category` | `My node`, `A custom AutoAir node kind`, `custom` | Palette metadata |
| `--icon` | `builtin:python` | Palette and canvas icon |
| `-o`, `--output` | `.` | Parent folder; the project folder (`autoair-node-<name>`) must not exist |

The distribution is `autoair-node-<name>` (underscores become hyphens), the import package `autoair_node_<name>`, the handler class `<Name>Handler` (for `http_get`: `HttpGetHandler`, with its config model `HttpGetHandlerConfig`). The generated `pyproject.toml` builds with hatchling, requires Python 3.10 or later, depends on `autoair` and `pydantic>=2`, and declares the `autoair.node_kinds` entry point and a `test` extra (pytest).

On success the command prints `created <path>`. An invalid name or an existing project folder prints `error: ...` and exits with code `2`; nothing is written.

The template ships inside the AutoAir package, in `src/autoair/scaffold/custom-node/` of the repository (`autoair/scaffold/custom-node/` in an installed wheel): a `cookiecutter.json` and a `{{cookiecutter.project_slug}}` tree. `autoair new-node` renders it with a sandboxed Jinja2 environment, so cookiecutter does not need to be installed; if you prefer, `cookiecutter <path to that folder>` produces the same project.

The generated tests render a spec with the node and compile it, check that values cannot inject code, run validation with good and bad configs, and check discovery through the entry point (this last test needs the package installed).

Restart AutoAir after installing: the node appears in the palette under its category.

## Frontend customisation

A backend node kind works without any frontend code: the inspector form is generated from the config JSON Schema. To change how its nodes look, register a renderer in `frontend/src/extensions/registry.ts`:

```ts
import { registerNodeKind } from "./extensions/registry";
import { LuGlobe } from "react-icons/lu";

registerNodeKind({
  handler: "http_get",                       // the handler's name
  icon: LuGlobe,                             // React component, receives { size }
  color: "#0ea5e9",
  describe: (spec) => String(spec.config.url ?? ""),   // short text under the node title
  // Inspector: MyHttpGetForm,               // replaces the generated config form
});
```

| Field | Effect |
|---|---|
| `handler` | Node kind the extension applies to |
| `icon` | Canvas icon component |
| `color` | Canvas accent |
| `describe(spec)` | Text rendered under the node title |
| `Inspector` | Component replacing the generated form; receives `spec` and `onChange(config)` |

Frontend extensions are compiled into the UI bundle, so they require building the frontend (`make build`) with the registration included. There is no runtime loading of frontend code.

## Distributing node kinds

Publish the package to your internal index and install it in the image (see [Extending AutoAir](/docs/extending/overview#where-to-install-extension-packages)). To make it discoverable for other teams, list it as an `extension` entry in a [marketplace](/docs/extending/marketplace): AutoAir then shows its `pip install` line, but never installs it by itself.

## Related pages

- [Custom operators](/docs/extending/custom-operators)
- [Spec format](/docs/extending/spec-format)
- [Security overview](/docs/security/overview)
