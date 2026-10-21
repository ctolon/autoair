"""Turn an operator class into a JSON Schema for its constructor parameters."""

from __future__ import annotations

import collections.abc
import contextlib
import inspect
import json
import re
import sys
import types
import typing
from dataclasses import dataclass, field
from typing import Any, Union

from pydantic import TypeAdapter

from autoair.sections import DEFAULT_CONNECTION_PARAM_PATTERNS

# Arguments every BaseOperator accepts; they are edited through dedicated, typed
# fields of the spec (retries, pool, ...) or are not meaningful in a no-code builder.
_SKIP_PARAMS = {"self", "args", "kwargs"}
# The built-in connection-param pattern; the catalog build applies the configured
# catalog.connection_param_patterns on top (autoair.catalog.service._Builder).
_CONN_RE = re.compile(DEFAULT_CONNECTION_PARAM_PATTERNS[0])
_RENDERER_HINTS = {
    "bash": "bash",
    "sql": "sql",
    "py": "python",
    "json": "json",
    "yaml": "yaml",
    "md": "markdown",
    "doc_md": "markdown",
}


@dataclass
class ParamInfo:
    name: str
    schema: dict[str, Any]
    required: bool
    adapter: TypeAdapter[Any] | None = None
    supported: bool = True


@dataclass
class IntrospectedOperator:
    params: dict[str, ParamInfo] = field(default_factory=dict)
    template_fields: list[str] = field(default_factory=list)
    docstring: str = ""
    summary: str = ""

    @property
    def unsupported_required(self) -> list[str]:
        return [n for n, p in self.params.items() if p.required and not p.supported]

    def json_schema(self, title: str) -> dict[str, Any]:
        props = {n: p.schema for n, p in self.params.items()}
        required = [n for n, p in self.params.items() if p.required and p.supported]
        schema: dict[str, Any] = {
            "type": "object",
            "title": title,
            "properties": props,
            "additionalProperties": False,
        }
        if required:
            schema["required"] = required
        return schema


def base_operator_param_names(base: type) -> set[str]:
    return set(inspect.signature(vars(base)["__init__"]).parameters) - _SKIP_PARAMS


def parse_param_docs(doc: str) -> dict[str, str]:
    """Extract ``:param name: text`` (sphinx style, multi-line) from a docstring."""
    out: dict[str, str] = {}
    current: str | None = None
    for raw in inspect.cleandoc(doc or "").splitlines():
        m = re.match(r"\s*:param\s+(?:[\w\[\], |.]+\s+)?(\w+):\s*(.*)", raw)
        if m:
            current = m.group(1)
            out[current] = m.group(2).strip()
        elif current and raw.strip() and not raw.lstrip().startswith(":"):
            out[current] = f"{out[current]} {raw.strip()}".strip()
        else:
            current = None
    return out


def summarize_doc(doc: str) -> str:
    text = inspect.cleandoc(doc or "")
    para = text.split("\n\n", 1)[0]
    return " ".join(para.split())[:300]


def _strip_argnotset(tp: Any) -> Any:
    """Remove Airflow's ``ArgNotSet`` sentinel from unions."""
    origin = typing.get_origin(tp)
    if origin in (Union, types.UnionType):
        args = [
            a
            for a in typing.get_args(tp)
            if getattr(a, "__name__", "") not in {"ArgNotSet", "NoneType_"}
        ]
        if not args:
            return Any
        if len(args) == 1:
            return args[0]
        return Union[tuple(args)]  # noqa: UP007 - runtime construction
    return tp


def _contains_callable(tp: Any) -> bool:
    if tp is typing.Callable or tp is collections.abc.Callable:
        return True
    if typing.get_origin(tp) in (typing.Callable, collections.abc.Callable):
        return True
    return any(_contains_callable(a) for a in typing.get_args(tp))


def _fallback_schema(annotation: str) -> dict[str, Any]:
    """Best-effort schema from a string annotation that could not be resolved."""
    ann = annotation.replace(" ", "")
    types_: list[str] = []
    for token, js in (
        ("str", "string"),
        ("bool", "boolean"),
        ("int", "integer"),
        ("float", "number"),
        ("dict", "object"),
        ("Mapping", "object"),
        ("list", "array"),
        ("Sequence", "array"),
        ("Iterable", "array"),
        ("Collection", "array"),
        ("None", "null"),
    ):
        if re.search(rf"(^|[^\w]){token}($|[^\w])", ann) and js not in types_:
            types_.append(js)
    if not types_ or ann in {"Any", ""}:
        return {}
    return {"type": types_[0]} if len(types_) == 1 else {"type": types_}


def _json_default(value: Any) -> tuple[bool, Any]:
    if type(value).__name__ == "ArgNotSet":
        return False, None
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return False, None
    return True, value


def introspect_operator(cls: type, base: type) -> IntrospectedOperator:
    base_names = base_operator_param_names(base)
    docs: dict[str, str] = {}
    for klass in reversed(cls.__mro__):
        if klass is not object:
            docs.update(parse_param_docs(klass.__doc__ or ""))

    result = IntrospectedOperator(
        template_fields=[f for f in getattr(cls, "template_fields", ()) if isinstance(f, str)],
        docstring=inspect.cleandoc(cls.__doc__ or ""),
        summary=summarize_doc(cls.__doc__ or ""),
    )
    renderers: dict[str, str] = dict(getattr(cls, "template_fields_renderers", {}) or {})

    supplied: set[str] = set()  # kwargs a subclass passes to its parent itself
    for klass in cls.__mro__:
        if klass is base or not issubclass(klass, base):
            break
        init = klass.__dict__.get("__init__")
        if init is None:
            continue
        try:
            sig = inspect.signature(init)
        except (TypeError, ValueError):
            continue
        hints = _resolve_hints(init)
        for name, p in sig.parameters.items():
            if (
                (name in supplied and name not in result.params)
                or name in _SKIP_PARAMS
                or name in base_names
                or name in result.params
                or p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD)
            ):
                continue
            result.params[name] = _param_info(name, p, hints.get(name), docs, result, renderers)
        supplied.update(super_init_kwargs(init))
    return result


def super_init_kwargs(func: Any) -> list[str]:
    """Keyword names ``func`` passes explicitly to ``super().__init__(...)``."""
    import ast
    import textwrap

    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(inspect.unwrap(func))))
    except (OSError, TypeError, SyntaxError, IndentationError):
        return []
    names: list[str] = []
    for sub in ast.walk(tree):
        if (
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Attribute)
            and sub.func.attr == "__init__"
            and (
                (
                    isinstance(sub.func.value, ast.Call)
                    and getattr(sub.func.value.func, "id", "") == "super"
                )
                or (isinstance(sub.func.value, (ast.Name, ast.Attribute)) and bool(sub.args))
            )
        ):
            names += [kw.arg for kw in sub.keywords if kw.arg and kw.arg not in names]
    return names


def _resolve_hints(func: Any) -> dict[str, Any]:
    """Resolve annotations one by one so a single unresolvable name does not lose all."""
    with contextlib.suppress(Exception):
        return typing.get_type_hints(func)
    module = sys.modules.get(getattr(func, "__module__", ""), None)
    namespace: dict[str, Any] = {
        **vars(typing),
        **collections.abc.__dict__,
        **(vars(module) if module else {}),
    }
    hints: dict[str, Any] = {}
    for name, ann in getattr(func, "__annotations__", {}).items():
        if not isinstance(ann, str):
            hints[name] = ann
            continue
        # Unresolvable annotations fall back to a string-based schema later on.
        with contextlib.suppress(Exception):
            hints[name] = eval(ann, namespace)  # noqa: S307 - library annotations only
    return hints


def _param_info(
    name: str,
    p: inspect.Parameter,
    hint: Any,
    docs: dict[str, str],
    result: IntrospectedOperator,
    renderers: dict[str, str],
) -> ParamInfo:
    schema: dict[str, Any]
    adapter: TypeAdapter[Any] | None = None
    supported = True
    if hint is not None:
        tp = _strip_argnotset(hint)
        if _contains_callable(tp):
            supported = False
            schema = {}
        else:
            try:
                adapter = TypeAdapter(tp)
                schema = adapter.json_schema(mode="validation")
            except Exception:
                adapter = None
                schema = _fallback_schema(str(p.annotation))
    else:
        schema = (
            _fallback_schema(str(p.annotation))
            if p.annotation is not inspect.Parameter.empty
            else {}
        )

    if "$defs" in schema:  # keep schemas self-contained but small
        schema = {k: v for k, v in schema.items() if k != "$defs"} | {"$defs": schema["$defs"]}
    schema = dict(schema)
    schema.setdefault("title", name.replace("_", " ").capitalize())
    if p.annotation is not inspect.Parameter.empty:
        from autoair.catalog.static import short_annotation

        raw = (
            p.annotation
            if isinstance(p.annotation, str)
            else inspect.formatannotation(p.annotation)
        )
        if shown := short_annotation(raw):
            schema["x-autoair-annotation"] = shown
    if name in docs:
        schema["description"] = docs[name]
    has_default = p.default is not inspect.Parameter.empty
    ok, default = _json_default(p.default) if has_default else (False, None)
    if ok:
        schema["default"] = default
    # NOTSET / ArgNotSet defaults mean "not set": the param is optional.
    required = not has_default
    if name in result.template_fields:
        schema["x-autoair-templated"] = True
        renderer = renderers.get(name) or next(
            (v for k, v in _RENDERER_HINTS.items() if name.endswith(k)), None
        )
        if renderer:
            schema["x-autoair-renderer"] = renderer
    if _CONN_RE.search(name):
        schema["x-autoair-widget"] = "connection"
    elif name == "pool" or name.endswith("_pool"):
        schema["x-autoair-widget"] = "pool"
    if not supported:
        schema["x-autoair-python-ref"] = "required"  # only a Python reference can express it
    return ParamInfo(
        name=name, schema=schema, required=required, adapter=adapter, supported=supported
    )
