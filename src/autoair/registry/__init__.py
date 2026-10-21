"""Extension point for custom node kinds.

Third-party packages register handlers through the ``autoair.node_kinds`` entry point
group::

    [project.entry-points."autoair.node_kinds"]
    http_get = "my_pkg.autoair_ext:HttpGetHandler"

A handler declares a Pydantic ``config_model`` (its JSON Schema drives the form in the
UI) and returns the operator construction expression as a Python AST node. Because the
handler emits AST — never source text — user-supplied strings cannot inject code.

Optional class attributes refine the UI and validation (all have defaults):

* ``icon`` — palette/canvas icon id (``builtin:<logo>`` or ``custom:<name>``);
* ``color`` — canvas accent (``#rrggbb``);
* ``example_config`` — config prefilled when the node is inserted;
* ``output_schema`` — JSON Schema of the task's XCom return value (documentation, typed
  XCom hints);
* ``doc_url`` — documentation link shown in the inspector;
* ``validate(node, config) -> list[str]`` — extra validation messages (reported as errors).

``autoair new-node <name>`` scaffolds a package implementing all of this (docs/extending.md).
"""

from __future__ import annotations

import ast
import functools
import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, runtime_checkable

from pydantic import BaseModel

if TYPE_CHECKING:
    from autoair.codegen.context import CodegenContext
    from autoair.spec import CustomNode

log = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "autoair.node_kinds"
ICON_PATTERN = re.compile(r"^(builtin|custom|package):[a-z0-9][a-z0-9-]{0,80}$")
COLOR_PATTERN = re.compile(r"^#[0-9a-fA-F]{6}$")


@functools.lru_cache(maxsize=256)
def _config_schema(model: type[BaseModel]) -> dict[str, Any]:
    """JSON Schema of a handler's config model (classes are immutable: build it once)."""
    return model.model_json_schema()


@runtime_checkable
class NodeKindHandler(Protocol):
    """Contract every custom node kind implements."""

    name: ClassVar[str]
    title: ClassVar[str]
    description: ClassVar[str]
    category: ClassVar[str]
    config_model: ClassVar[type[BaseModel]]

    def build(self, node: CustomNode, config: BaseModel, ctx: CodegenContext) -> ast.expr:
        """Return an expression that constructs the task (e.g. an operator call).

        Use ``ctx.import_name(module, name)`` to obtain a reference to an imported
        symbol and ``ctx.task_kwargs(node)`` for the common task keyword arguments
        (``task_id``, retries, pool, ...).
        """
        ...


def _hint(handler: object, name: str, pattern: re.Pattern[str]) -> str | None:
    value = getattr(handler, name, None)
    if isinstance(value, str) and pattern.fullmatch(value):
        return value
    if value is not None:
        kind = getattr(handler, "name", "?")
        log.warning("node kind %r: ignoring invalid %s %r", kind, name, value)
    return None


def _dict_hint(handler: object, name: str) -> dict[str, Any] | None:
    value = getattr(handler, name, None)
    return dict(value) if isinstance(value, dict) else None


def describe_handler(h: NodeKindHandler) -> dict[str, Any]:
    """What the UI knows about a node kind: titles, config schema and the optional UI hints
    (invalid hints are dropped, never passed on)."""
    doc_url = getattr(h, "doc_url", None)
    return {
        "name": h.name,
        "title": h.title,
        "description": h.description,
        "category": h.category,
        "config_schema": _config_schema(h.config_model),
        "icon": _hint(h, "icon", ICON_PATTERN),
        "color": _hint(h, "color", COLOR_PATTERN),
        "example_config": _dict_hint(h, "example_config"),
        "output_schema": _dict_hint(h, "output_schema"),
        "doc_url": doc_url if isinstance(doc_url, str) and doc_url.startswith("https://") else None,
    }


def handler_problems(handler: NodeKindHandler, node: CustomNode, config: BaseModel) -> list[str]:
    """Messages of the handler's optional ``validate(node, config)`` hook; a failing hook is
    reported as one message instead of breaking validation."""
    hook = getattr(handler, "validate", None)
    if not callable(hook):
        return []
    try:
        result = hook(node, config)
    except Exception as exc:
        return [f"{handler.name}: validation hook failed: {type(exc).__name__}: {exc}"]
    if not result:
        return []
    if isinstance(result, str):
        return [result]
    return [str(m) for m in result]


@dataclass
class HandlerRegistry:
    _handlers: dict[str, NodeKindHandler] = field(default_factory=dict)

    def register(self, handler: NodeKindHandler, *, replace: bool = False) -> None:
        if not isinstance(handler, NodeKindHandler):
            raise TypeError(f"{handler!r} does not implement NodeKindHandler")
        if handler.name in self._handlers and not replace:
            raise ValueError(f"node kind handler {handler.name!r} already registered")
        self._handlers[handler.name] = handler

    def get(self, name: str) -> NodeKindHandler:
        try:
            return self._handlers[name]
        except KeyError:
            raise KeyError(f"unknown custom node handler {name!r}") from None

    def __contains__(self, name: object) -> bool:
        return name in self._handlers

    def __iter__(self) -> Iterator[NodeKindHandler]:
        return iter(self._handlers.values())

    def describe(self) -> list[dict[str, Any]]:
        return [describe_handler(h) for h in self]

    @classmethod
    def from_entry_points(cls) -> HandlerRegistry:
        registry = cls()
        for ep in entry_points(group=ENTRY_POINT_GROUP):
            try:
                obj = ep.load()
                registry.register(obj() if isinstance(obj, type) else obj)
            except Exception:  # an extension must never break the builder
                log.exception("failed to load autoair node kind %r", ep.name)
        return registry


_default: HandlerRegistry | None = None


def default_registry() -> HandlerRegistry:
    global _default
    if _default is None:
        _default = HandlerRegistry.from_entry_points()
    return _default
