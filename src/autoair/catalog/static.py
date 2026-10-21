"""Import-free (AST based) analysis of operator / notifier modules.

Modules are located on ``sys.path`` without importing them, parsed with :mod:`ast`
and reduced to small, picklable :class:`ModuleInfo` records. A :class:`StaticAnalyzer`
then resolves base classes across modules (following ``import`` / ``from ... import``
aliases, ``TYPE_CHECKING`` blocks and lazy-import tables), linearises the class
hierarchy and produces the same JSON Schema shape the import-based introspection
produces — without ever executing provider code.
"""

from __future__ import annotations

import ast
import functools
import importlib.util
import inspect
import re
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from autoair.catalog.introspection import (
    _CONN_RE,
    _RENDERER_HINTS,
    _SKIP_PARAMS,
    parse_param_docs,
    summarize_doc,
)

# --------------------------------------------------------------------------- locating


def find_module_file(name: str) -> Path | None:
    """Locate the source file of module ``name`` without importing it (or its parents)."""
    return _find_module_file(name, tuple(sys.path))


@lru_cache(maxsize=8192)
def _find_module_file(name: str, path: tuple[str, ...]) -> Path | None:
    parts = name.split(".")
    for entry in path:
        base = Path(entry or ".").joinpath(*parts)
        candidate = base.with_name(base.name + ".py")
        if candidate.is_file():
            return candidate
        init = base / "__init__.py"
        if init.is_file():
            return init
    # Custom finders (e.g. editable installs). Only safe without importing parents.
    parent = name.rpartition(".")[0]
    if parent and parent not in sys.modules:
        return None
    try:
        spec = importlib.util.find_spec(name)
    except (ImportError, ValueError, AttributeError):
        return None
    if spec is None or not spec.origin or not spec.origin.endswith(".py"):
        return None
    return Path(spec.origin)


def clear_locator_cache() -> None:
    _find_module_file.cache_clear()


# --------------------------------------------------------------------------- records


@dataclass
class ParamDef:
    name: str
    annotation: str | None
    default: str | None  # unparsed default expression; ``None`` means "no default"
    kind: Literal["positional", "keyword", "var_positional", "var_keyword"]


@dataclass(eq=False)
class ClassInfo:
    name: str
    module: str
    bases: list[str]
    doc: str | None
    init: list[ParamDef] | None
    attrs: dict[str, str] = field(default_factory=dict)  # class-level NAME = <expr source>
    hook_refs: list[str] = field(default_factory=list)  # names ending in "Hook" used in body
    abstract: bool = False
    deprecated: bool = False
    # keyword names passed explicitly to ``super().__init__(...)`` (supplied by this class)
    super_kwargs: list[str] = field(default_factory=list)
    # ``def m(self): return <Name|Attribute>`` (e.g. ``trigger_class``)
    method_returns: dict[str, str] = field(default_factory=dict)


@dataclass
class ModuleInfo:
    name: str
    path: str
    is_package: bool
    imports: dict[str, str] = field(default_factory=dict)
    classes: dict[str, ClassInfo] = field(default_factory=dict)
    assigns: dict[str, str] = field(default_factory=dict)
    error: str | None = None


_CLASS_ATTRS = {
    "template_fields",
    "template_fields_renderers",
    "ui_color",
    "conn_type",
    "default_conn_name",
    "conn_name_attr",
    "hook_name",
    "scheme",
}


def parse_module(name: str, path: Path | None = None) -> ModuleInfo:
    """Parse one module into a :class:`ModuleInfo` (never raises)."""
    path = path or find_module_file(name)
    if path is None:
        return ModuleInfo(name=name, path="", is_package=False, error="module source not found")
    is_package = path.name == "__init__.py"
    info = ModuleInfo(name=name, path=str(path), is_package=is_package)
    try:
        tree = ast.parse(path.read_bytes(), filename=str(path))
    except (OSError, SyntaxError, ValueError) as exc:
        info.error = f"{type(exc).__name__}: {exc}"
        return info
    package = name if is_package else name.rpartition(".")[0]
    for node in _toplevel(tree.body):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    info.imports.setdefault(alias.asname, alias.name)
                else:
                    head = alias.name.split(".", 1)[0]
                    info.imports.setdefault(head, head)
        elif isinstance(node, ast.ImportFrom):
            base = _resolve_relative(package, node.module, node.level)
            if base is None:
                continue
            for alias in node.names:
                if alias.name == "*":
                    continue
                info.imports.setdefault(alias.asname or alias.name, f"{base}.{alias.name}")
        elif isinstance(node, ast.ClassDef):
            info.classes.setdefault(node.name, _class_info(node, name))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if node.value is None:
                continue
            for t in targets:
                if isinstance(t, ast.Name):
                    info.assigns.setdefault(t.id, ast.unparse(node.value))
    return info


def _toplevel(body: list[ast.stmt]) -> Iterator[ast.stmt]:
    """Module-level statements, descending into ``if`` / ``try`` blocks (version shims)."""
    for node in body:
        if isinstance(node, ast.If):
            yield from _toplevel(node.body)
            yield from _toplevel(node.orelse)
        elif isinstance(node, ast.Try):
            yield from _toplevel(node.body)
            for h in node.handlers:
                yield from _toplevel(h.body)
            yield from _toplevel(node.orelse)
            yield from _toplevel(node.finalbody)
        else:
            yield node


def _resolve_relative(package: str, module: str | None, level: int) -> str | None:
    if level == 0:
        return module
    parts = package.split(".") if package else []
    if level - 1 > len(parts):
        return None
    base = parts[: len(parts) - (level - 1)]
    if module:
        base.append(module)
    return ".".join(base) or None


def _decorator_name(node: ast.expr) -> str:
    if isinstance(node, ast.Call):
        node = node.func
    try:
        return ast.unparse(node)
    except Exception:  # pragma: no cover - defensive
        return ""


def _class_info(node: ast.ClassDef, module: str) -> ClassInfo:
    bases = [ast.unparse(b) for b in node.bases]
    info = ClassInfo(
        name=node.name,
        module=module,
        bases=bases,
        doc=ast.get_docstring(node, clean=False),
        init=None,
        deprecated=any("deprecat" in _decorator_name(d).lower() for d in node.decorator_list),
    )
    for stmt in _toplevel(node.body):
        if isinstance(stmt, (ast.Assign, ast.AnnAssign)) and stmt.value is not None:
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            for t in targets:
                if isinstance(t, ast.Name):
                    value = ast.unparse(stmt.value)
                    if t.id in _CLASS_ATTRS or len(value) <= 200:
                        info.attrs.setdefault(t.id, value)
        elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if any(_decorator_name(d).endswith("abstractmethod") for d in stmt.decorator_list):
                info.abstract = True
            if stmt.name == "__init__" and info.init is None:
                info.init = _params(stmt.args)
                info.super_kwargs = _super_init_kwargs(stmt)
            last = stmt.body[-1] if stmt.body else None
            if (
                isinstance(last, ast.Return)
                and isinstance(last.value, (ast.Name, ast.Attribute))
                and stmt.name not in info.method_returns
            ):
                info.method_returns[stmt.name] = ast.unparse(last.value)
    refs: list[str] = []
    for sub in ast.walk(node):
        name = None
        if isinstance(sub, ast.Name):
            name = sub.id
        elif (
            isinstance(sub, ast.Constant)
            and isinstance(sub.value, str)
            and re.fullmatch(r"[A-Za-z_][\w.]*Hook", sub.value)
        ):
            name = sub.value  # string annotations ("SmtpHook")
        if name and name.endswith("Hook") and name not in refs:
            refs.append(name)
    info.hook_refs = refs
    return info


def _super_init_kwargs(func: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """Keyword names passed explicitly to ``super().__init__`` / ``Base.__init__(self, ...)``."""
    names: list[str] = []
    for sub in ast.walk(func):
        if not (
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Attribute)
            and sub.func.attr == "__init__"
        ):
            continue
        target = sub.func.value
        is_super = isinstance(target, ast.Call) and _tail(target.func) == "super"
        is_explicit = isinstance(target, (ast.Name, ast.Attribute)) and bool(sub.args)
        if not (is_super or is_explicit):
            continue
        for kw in sub.keywords:
            if kw.arg and kw.arg not in names:
                names.append(kw.arg)
    return names


def _params(args: ast.arguments) -> list[ParamDef]:
    out: list[ParamDef] = []
    positional = [*args.posonlyargs, *args.args]
    defaults: list[ast.expr | None] = [None] * (len(positional) - len(args.defaults))
    defaults += list(args.defaults)
    for a, d in zip(positional, defaults, strict=True):
        out.append(ParamDef(a.arg, _ann(a.annotation), _src(d), "positional"))
    if args.vararg:
        out.append(ParamDef(args.vararg.arg, None, None, "var_positional"))
    for a, kd in zip(args.kwonlyargs, args.kw_defaults, strict=True):
        out.append(ParamDef(a.arg, _ann(a.annotation), _src(kd), "keyword"))
    if args.kwarg:
        out.append(ParamDef(args.kwarg.arg, None, None, "var_keyword"))
    return out


def _ann(node: ast.expr | None) -> str | None:
    if node is None:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return ast.unparse(node)


def _src(node: ast.expr | None) -> str | None:
    return None if node is None else ast.unparse(node)


# --------------------------------------------------------------------------- annotations


_PRIMITIVES = {
    "str": {"type": "string"},
    "int": {"type": "integer"},
    "float": {"type": "number"},
    "bool": {"type": "boolean"},
    "bytes": {"type": "string", "format": "binary"},
    "Path": {"type": "string", "format": "path"},
    "PathLike": {"type": "string", "format": "path"},
    "PurePath": {"type": "string", "format": "path"},
}
_ARRAYS = {
    "list",
    "List",
    "Sequence",
    "MutableSequence",
    "Iterable",
    "Collection",
    "Container",
    "Iterator",
    "set",
    "Set",
    "frozenset",
    "FrozenSet",
    "AbstractSet",
    "MutableSet",
    "tuple",
    "Tuple",
}
_OBJECTS = {"dict", "Dict", "Mapping", "MutableMapping", "OrderedDict", "defaultdict"}
_TRANSPARENT = {"Annotated", "ClassVar", "Final", "Required", "NotRequired", "ReadOnly"}
_ANY = {"Any", "object"}
_CALLABLE = {"Callable"}
_NOTSET = {"ArgNotSet", "NOTSET", "ArgNotSetType"}


class _Unsupported(Exception):
    pass


# Schema extension: the parameter takes a Python object (callable, class…) that can be given
# as an import reference ``{"$type": "import", "path": "pkg.module.name"}``.
PYTHON_REF = "x-autoair-python-ref"


class _Unknown(Exception):
    pass


@functools.lru_cache(maxsize=512)
def callable_docstring(path: str) -> str | None:
    """Docstring of a module-level function/class by dotted path, read statically (no import).

    Task decorators (``airflow.providers.docker.decorators.docker.docker_task``) often keep their
    documentation on the decorated operator class of the same module instead: fall back to the
    first ``*DecoratedOperator`` class there.
    """
    module, _, name = path.rpartition(".")
    file = find_module_file(module) if module else None
    if file is None:
        return None
    try:
        tree = ast.parse(file.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError):
        return None
    defs = {
        n.name: n
        for n in tree.body
        if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
    }
    target = defs.get(name)
    doc = ast.get_docstring(target) if target is not None else None
    if doc:
        return doc
    for node_name, node in defs.items():
        if isinstance(node, ast.ClassDef) and node_name.endswith("DecoratedOperator"):
            doc = ast.get_docstring(node)
            if doc:
                return doc
    return None


def short_annotation(annotation: str | None) -> str | None:
    """Readable type for the UI: ``datetime.timedelta | None`` → ``timedelta | None``."""
    if not annotation:
        return None
    text = " ".join(annotation.replace("typing.", "").split()).strip("'\"")
    text = re.sub(r"\b(?:[A-Za-z_]\w*\.)+([A-Za-z_]\w*)", r"\1", text)
    text = re.sub(r"\bOptional\[(.+)\]$", r"\1 | None", text)
    return text if len(text) <= 80 else text[:77] + "..."


def annotation_schema(annotation: str | None) -> tuple[dict[str, Any], bool]:
    """Map an annotation (source string) to a JSON Schema.

    Returns ``(schema, supported)``; ``supported`` is False when the annotation
    involves a ``Callable`` (cannot be configured in a no-code builder). Unknown
    types yield ``{}`` (any value).
    """
    if not annotation:
        return {}, True
    try:
        node = ast.parse(annotation.strip(), mode="eval").body
    except SyntaxError:
        return {}, True
    try:
        return _schema(node), True
    except _Unsupported:
        return {}, False
    except _Unknown:
        return {}, True


def _tail(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _schema(node: ast.expr) -> dict[str, Any]:
    if isinstance(node, ast.Constant):
        if node.value is None:
            return {"type": "null"}
        if isinstance(node.value, str):  # nested forward reference
            try:
                return _schema(ast.parse(node.value, mode="eval").body)
            except SyntaxError as exc:
                raise _Unknown from exc
        raise _Unknown
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _union([node.left, node.right])
    name = _tail(node)
    if name is not None:
        if name in _CALLABLE:
            raise _Unsupported
        if name == "None" or name == "NoneType":
            return {"type": "null"}
        if name in _PRIMITIVES:
            return dict(_PRIMITIVES[name])
        if name in _ARRAYS:
            return {"type": "array", "items": {}}
        if name in _OBJECTS:
            return {"type": "object"}
        if name in _ANY:
            return {}
        raise _Unknown
    if isinstance(node, ast.Subscript):
        outer = _tail(node.value)
        inner = node.slice
        args = list(inner.elts) if isinstance(inner, ast.Tuple) else [inner]
        if outer in _CALLABLE:
            raise _Unsupported
        if outer == "Optional":
            return _union([args[0], ast.Constant(None)])
        if outer == "Union":
            return _union(args)
        if outer in _TRANSPARENT:
            return _schema(args[0])
        if outer == "Literal":
            values = [_literal(a) for a in args]
            out: dict[str, Any] = {"enum": values}
            kinds = {type(v) for v in values}
            if len(kinds) == 1:
                t = {str: "string", int: "integer", bool: "boolean", float: "number"}.get(
                    kinds.pop()
                )
                if t:
                    out["type"] = t
            return out
        if outer in {"tuple", "Tuple"}:
            if len(args) == 2 and isinstance(args[1], ast.Constant) and args[1].value is ...:
                return {"type": "array", "items": _item(args[0])}
            items = [_item(a) for a in args]
            return {
                "type": "array",
                "prefixItems": items,
                "minItems": len(items),
                "maxItems": len(items),
            }
        if outer in _ARRAYS:
            out = {"type": "array", "items": _item(args[0])}
            if outer in {"set", "Set", "frozenset", "FrozenSet", "AbstractSet", "MutableSet"}:
                out["uniqueItems"] = True
            return out
        if outer in _OBJECTS:
            value = _item(args[1]) if len(args) > 1 else {}
            return (
                {"type": "object", "additionalProperties": value} if value else {"type": "object"}
            )
        if outer in {"type", "Type"}:
            raise _Unknown
        raise _Unknown
    raise _Unknown


def _item(node: ast.expr) -> dict[str, Any]:
    """Schema of a container item: unknown item types degrade to ``{}``."""
    try:
        return _schema(node)
    except _Unknown:
        return {}


def _literal(node: ast.expr) -> Any:
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError) as exc:
        raise _Unknown from exc


def _union(nodes: Iterable[ast.expr]) -> dict[str, Any]:
    members: list[dict[str, Any]] = []
    callable_member = False
    for n in nodes:
        if _tail(n) in _NOTSET:
            continue
        if isinstance(n, ast.BinOp) and isinstance(n.op, ast.BitOr):
            try:
                sub = _union([n.left, n.right])
            except _Unsupported:
                callable_member = True
                continue
            callable_member = callable_member or bool(sub.pop(PYTHON_REF, False))
            members.extend(sub.get("anyOf", [sub]))
            continue
        try:
            s = _schema(n)  # raises _Unknown for the whole union
        except _Unsupported:  # e.g. ``str | Callable``: the other members stay configurable
            callable_member = True
            continue
        if s == {}:
            raise _Unknown
        if s not in members:
            members.append(s)
    if callable_member and not members:
        raise _Unsupported
    if not members:
        return {}
    out: dict[str, Any] = dict(members[0]) if len(members) == 1 else {"anyOf": members}
    if callable_member:
        out[PYTHON_REF] = "optional"  # also accepts a Python reference
    return out


# --------------------------------------------------------------------------- resolution

RootKind = Literal["operator", "notifier"]
_ROOT_NAMES: dict[str, RootKind] = {"BaseOperator": "operator", "BaseNotifier": "notifier"}
CANONICAL = {
    "BaseOperator": ("airflow.sdk.bases.operator", "airflow.models.baseoperator"),
    "BaseSensorOperator": ("airflow.sdk.bases.sensor", "airflow.sensors.base"),
    "BaseNotifier": ("airflow.sdk.bases.notifier", "airflow.notifications.basenotifier"),
}
_KNOWN_NON_ROOT = {
    "object",
    "ABC",
    "Generic",
    "Protocol",
    "Enum",
    "IntEnum",
    "StrEnum",
    "Exception",
    "BaseException",
    "NamedTuple",
    "TypedDict",
    "BaseModel",
    "dict",
    "list",
    "str",
    "int",
    "LoggingMixin",
}
_FALLBACK_BASE_PARAMS = frozenset(
    {
        "task_id", "owner", "email", "email_on_retry", "email_on_failure", "retries",
        "retry_delay", "retry_exponential_backoff", "max_retry_delay", "start_date",
        "end_date", "depends_on_past", "ignore_first_depends_on_past",
        "wait_for_past_depends_before_skipping", "wait_for_downstream", "dag", "params",
        "default_args", "priority_weight", "weight_rule", "queue", "pool", "pool_slots",
        "sla", "execution_timeout", "on_execute_callback", "on_failure_callback",
        "on_success_callback", "on_retry_callback", "on_skipped_callback",
        "pre_execute", "post_execute", "trigger_rule", "resources", "run_as_user",
        "task_concurrency", "map_index_template", "max_active_tis_per_dag",
        "max_active_tis_per_dagrun", "executor", "executor_config", "do_xcom_push",
        "multiple_outputs", "inlets", "outlets", "task_group", "doc", "doc_md",
        "doc_json", "doc_yaml", "doc_rst", "task_display_name", "logger_name",
        "allow_nested_operators", "render_template_as_native_obj", "retry_policy",
        "has_on_execute_callback", "has_on_failure_callback", "has_on_success_callback",
        "has_on_retry_callback", "has_on_skipped_callback",
    }
)  # fmt: skip


@dataclass(frozen=True)
class Root:
    kind: RootKind
    cls: ClassInfo | None = None


Resolved = ClassInfo | Root | None  # None == not resolvable


class Unresolvable(Exception):
    """Static analysis cannot answer; the caller should fall back to importing."""


@dataclass
class StaticParam:
    name: str
    schema: dict[str, Any]
    required: bool
    supported: bool
    default: Any = None
    has_default: bool = False


@dataclass
class StaticClassResult:
    info: ClassInfo
    root: RootKind
    is_sensor: bool
    lineage: list[ClassInfo]
    params: dict[str, StaticParam]
    template_fields: list[str]
    renderers: dict[str, str]
    ui_color: str | None
    docstring: str
    summary: str
    hook_refs: list[ClassInfo]

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


_MISSING = object()


class StaticAnalyzer:
    """Resolve classes across statically parsed modules (thread-unsafe; use per build)."""

    def __init__(self, modules: dict[str, ModuleInfo] | None = None) -> None:
        self._modules: dict[str, ModuleInfo | None] = dict(modules or {})
        self._root_params: dict[RootKind, frozenset[str]] = {}
        # Modules whose source could not be parsed: name -> error (catalog diagnostics).
        self.parse_errors: dict[str, str] = {}

    # ------------------------------------------------------------ modules
    def add(self, info: ModuleInfo) -> None:
        if info.error is not None:
            self.parse_errors[info.name] = info.error
            self._modules[info.name] = None
            return
        self._modules[info.name] = info

    def module(self, name: str) -> ModuleInfo | None:
        if name not in self._modules:
            path = find_module_file(name)
            info = parse_module(name, path) if path else None
            if info is not None and info.error is not None:
                self.parse_errors[name] = info.error
            self._modules[name] = info if info is None or info.error is None else None
        return self._modules[name]

    def canonical(self, name: str) -> ClassInfo | None:
        for mod in CANONICAL.get(name, ()):
            mi = self.module(mod)
            if mi and name in mi.classes:
                return mi.classes[name]
        return None

    def root_param_names(self, kind: RootKind) -> frozenset[str]:
        if kind not in self._root_params:
            cls = self.canonical("BaseOperator" if kind == "operator" else "BaseNotifier")
            init = cls.init if cls is not None else None
            names = {p.name for p in init or [] if p.name not in _SKIP_PARAMS}
            if kind == "operator":
                names |= _FALLBACK_BASE_PARAMS if not names else set()
            self._root_params[kind] = frozenset(names)
        return self._root_params[kind]

    # ------------------------------------------------------------ symbols
    def resolve(self, module: str, expr: str, depth: int = 0) -> Resolved:
        """Resolve a (possibly dotted / subscripted) name used in ``module``."""
        expr = expr.split("[", 1)[0].strip()
        if depth > 12 or not expr:
            return None
        mi = self.module(module)
        head, _, rest = expr.partition(".")
        if mi is None:
            return None
        if not rest:
            if head in mi.classes:
                return mi.classes[head]
            if head in mi.imports:
                return self.resolve_fqn(mi.imports[head], depth + 1)
            lazy = self._lazy_target(mi, head)
            if lazy:
                return self.resolve_fqn(lazy, depth + 1)
            return None
        if head in mi.imports:
            return self.resolve_fqn(f"{mi.imports[head]}.{rest}", depth + 1)
        return None

    def resolve_fqn(self, fqn: str, depth: int = 0) -> Resolved:
        module, _, name = fqn.rpartition(".")
        if not module:
            return None
        if fqn.startswith("airflow.") and name in _ROOT_NAMES:
            canonical = self.canonical(name)
            return Root(_ROOT_NAMES[name], canonical)
        if fqn.startswith("airflow.") and name in CANONICAL:
            canonical = self.canonical(name)
            if canonical is not None:
                return canonical
        if self.module(module) is not None:
            return self.resolve(module, name, depth)
        # ``pkg.mod.Name`` where ``pkg.mod`` is itself an attribute path
        return None

    def _lazy_target(self, mi: ModuleInfo, name: str) -> str | None:
        """``__getattr__``-style lazy tables: ``{"Name": "module"}`` / ``("mod1", "mod2")``."""
        pattern = re.compile(rf"""["']{re.escape(name)}["']\s*:\s*(\(.*?\)|["'][^"']+["'])""")
        for src in mi.assigns.values():
            if name not in src:
                continue
            m = pattern.search(src)
            if not m:
                continue
            try:
                value = ast.literal_eval(m.group(1))
            except (ValueError, SyntaxError):
                continue
            candidates = [value] if isinstance(value, str) else list(value)
            for cand in candidates:
                if not isinstance(cand, str):
                    continue
                cand = _resolve_relative(
                    mi.name if mi.is_package else mi.name.rpartition(".")[0],
                    cand.lstrip("."),
                    len(cand) - len(cand.lstrip(".")),
                )
                if cand:
                    return f"{cand}.{name}" if not cand.endswith(f".{name}") else cand
        return None

    # ------------------------------------------------------------ hierarchy
    def bases_of(self, cls: ClassInfo) -> list[Resolved | str]:
        out: list[Resolved | str] = []
        for b in cls.bases:
            bare = b.split("[", 1)[0].strip()
            if bare.rsplit(".", 1)[-1] in _KNOWN_NON_ROOT:
                out.append("nonroot")
                continue
            if bare.startswith(("metaclass", "Generic")):
                continue
            out.append(self.resolve(cls.module, bare))
        return out

    def classify(self, cls: ClassInfo, _seen: frozenset[int] = frozenset()) -> RootKind | None:
        """Root kind the class derives from; raises :class:`Unresolvable` when unknown."""
        if id(cls) in _seen:
            return None
        seen = _seen | {id(cls)}
        unknown = False
        for b in self.bases_of(cls):
            if isinstance(b, Root):
                return b.kind
            if isinstance(b, ClassInfo):
                try:
                    kind = self.classify(b, seen)
                except Unresolvable:
                    unknown = True
                    continue
                if kind:
                    return kind
            elif b is None:
                unknown = True
        if unknown:
            raise Unresolvable(f"cannot resolve bases of {cls.module}.{cls.name}")
        return None

    def mro(self, cls: ClassInfo) -> list[ClassInfo | Root]:
        """C3 linearisation over resolvable classes (unresolvable bases are dropped)."""
        memo: dict[int, list[ClassInfo | Root]] = {}

        def lin(c: ClassInfo | Root, stack: frozenset[int]) -> list[ClassInfo | Root]:
            if isinstance(c, Root):
                return [c]
            if id(c) in memo:
                return memo[id(c)]
            if id(c) in stack:
                return [c]
            parents = [b for b in self.bases_of(c) if isinstance(b, (ClassInfo, Root))]
            seqs = [lin(p, stack | {id(c)}) for p in parents] + [list(parents)]
            result: list[ClassInfo | Root] = [c]
            seqs = [list(s) for s in seqs if s]
            while seqs:
                for s in seqs:
                    head = s[0]
                    if not any(head in t[1:] for t in seqs):
                        break
                else:  # inconsistent hierarchy: fall back to depth-first order
                    for s in seqs:
                        for item in s:
                            if item not in result:
                                result.append(item)
                    break
                result.append(head)
                seqs = [[x for x in t if x != head] for t in seqs]
                seqs = [t for t in seqs if t]
            memo[id(c)] = result
            return result

        return lin(cls, frozenset())

    # ------------------------------------------------------------ evaluation
    def _eval_value(
        self, cls_module: str, src: str, owner: ClassInfo | None = None, depth: int = 0
    ) -> Any:
        try:
            return ast.literal_eval(src)
        except (ValueError, SyntaxError, TypeError):
            pass
        if depth > 6:
            return _MISSING
        if re.fullmatch(r"[A-Za-z_]\w*", src):
            if owner is not None and src in owner.attrs and owner.attrs[src] != src:
                return self._eval_value(cls_module, owner.attrs[src], None, depth + 1)
            mi = self.module(cls_module)
            if mi is not None and src in mi.assigns:
                return self._eval_value(cls_module, mi.assigns[src], None, depth + 1)
            if mi is not None and src in mi.imports:
                target_mod, _, target_name = mi.imports[src].rpartition(".")
                tm = self.module(target_mod) if target_mod else None
                if tm is not None and target_name in tm.assigns:
                    return self._eval_value(target_mod, tm.assigns[target_name], None, depth + 1)
            return _MISSING
        m = re.fullmatch(r"([\w.]+)\.(default_conn_name|conn_type)", src)
        if m:
            target = self.resolve(cls_module, m.group(1))
            if isinstance(target, ClassInfo):
                value = self.class_attr(target, m.group(2))
                if value is not _MISSING:
                    return value
        return _MISSING

    def class_attr(self, cls: ClassInfo, attr: str) -> Any:
        """Literal class attribute looked up along the (static) MRO."""
        for k in self.mro(cls):
            info = k.cls if isinstance(k, Root) else k
            if info is None or attr not in info.attrs:
                continue
            return self._eval_value(info.module, info.attrs[attr], info)
        return _MISSING

    def _attr_owner(self, mro: list[ClassInfo | Root], attr: str) -> ClassInfo | None:
        for k in mro:
            info = k.cls if isinstance(k, Root) else k
            if info is not None and attr in info.attrs:
                return info
        return None

    def string_seq(self, cls: ClassInfo, attr: str = "template_fields") -> list[str]:
        owner = self._attr_owner(self.mro(cls), attr)
        if owner is None:
            return []
        node = ast.parse(owner.attrs[attr], mode="eval").body
        return _dedupe(self._seq(owner, node, attr))

    def _seq(self, owner: ClassInfo, node: ast.expr, attr: str) -> list[str]:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return [node.value]
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            out: list[str] = []
            for e in node.elts:
                out += self._seq(owner, e.value if isinstance(e, ast.Starred) else e, attr)
            return out
        if isinstance(node, ast.Call) and _tail(node.func) in {
            "tuple",
            "list",
            "set",
            "sorted",
            "frozenset",
        }:
            return self._seq(owner, node.args[0], attr) if node.args else []
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"union", "__add__"}
        ):
            out = self._seq(owner, node.func.value, attr)
            for a in node.args:
                out += self._seq(owner, a, attr)
            return out
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.BitOr)):
            return self._seq(owner, node.left, attr) + self._seq(owner, node.right, attr)
        if isinstance(node, ast.Attribute) and node.attr in {attr, "template_fields"}:
            target = self._class_expr(owner, node.value)
            return self.string_seq(target, node.attr)
        if isinstance(node, ast.Name):
            mi = self.module(owner.module)
            if mi and node.id in mi.assigns:
                return self._seq(owner, ast.parse(mi.assigns[node.id], mode="eval").body, attr)
        raise Unresolvable(f"cannot evaluate {attr} of {owner.module}.{owner.name}")

    def _class_expr(self, owner: ClassInfo, node: ast.expr) -> ClassInfo:
        src = ast.unparse(node)
        if src in {"self", "cls"}:
            raise Unresolvable("self reference")
        target = self.resolve(owner.module, src)
        if isinstance(target, Root):
            if target.cls is None:
                raise Unresolvable(src)
            return target.cls
        if not isinstance(target, ClassInfo):
            raise Unresolvable(src)
        return target

    def string_map(self, cls: ClassInfo, attr: str = "template_fields_renderers") -> dict[str, str]:
        owner = self._attr_owner(self.mro(cls), attr)
        if owner is None:
            return {}
        try:
            return self._map(owner, ast.parse(owner.attrs[attr], mode="eval").body, attr)
        except Unresolvable:
            return {}

    def _map(self, owner: ClassInfo, node: ast.expr, attr: str) -> dict[str, str]:
        if isinstance(node, ast.Dict):
            out: dict[str, str] = {}
            for k, v in zip(node.keys, node.values, strict=True):
                if k is None:
                    out.update(self._map(owner, v, attr))
                elif isinstance(k, ast.Constant) and isinstance(v, ast.Constant):
                    out[str(k.value)] = str(v.value)
            return out
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            return self._map(owner, node.left, attr) | self._map(owner, node.right, attr)
        if isinstance(node, ast.Call) and _tail(node.func) == "dict":
            out = {}
            for a in node.args:
                out.update(self._map(owner, a, attr))
            for kw in node.keywords:
                if kw.arg and isinstance(kw.value, ast.Constant):
                    out[kw.arg] = str(kw.value.value)
                elif kw.arg is None:
                    out.update(self._map(owner, kw.value, attr))
            return out
        if isinstance(node, ast.Attribute) and node.attr == attr:
            return self.string_map(self._class_expr(owner, node.value), attr)
        raise Unresolvable(attr)

    # ------------------------------------------------------------ analysis
    def analyze(self, cls: ClassInfo) -> StaticClassResult:
        """Full static analysis; raises :class:`Unresolvable` when import is required."""
        root = self.classify(cls)
        if root is None:
            raise ValueError(f"{cls.name} is not an operator or notifier")
        mro = self.mro(cls)
        lineage: list[ClassInfo] = []
        for k in mro:
            if isinstance(k, Root):
                break
            try:
                if self.classify(k) == root:
                    lineage.append(k)
            except Unresolvable:
                continue
        is_sensor = any(
            k.name == "BaseSensorOperator" and k.module.startswith("airflow.") for k in lineage
        )
        template_fields = self.string_seq(cls)
        renderers = self.string_map(cls)
        ui = self.class_attr(cls, "ui_color")
        docs: dict[str, str] = {}
        for k in reversed(lineage):
            docs.update(parse_param_docs(k.doc or ""))
        base_names = self.root_param_names(root)
        params: dict[str, StaticParam] = {}
        supplied: set[str] = set()  # kwargs a subclass passes to its parent itself
        for k in lineage:
            for p in k.init or []:
                if p.name in supplied and p.name not in params:
                    continue
                if (
                    p.name in _SKIP_PARAMS
                    or p.name in base_names
                    or p.name in params
                    or p.kind in ("var_positional", "var_keyword")
                ):
                    continue
                params[p.name] = self._param(k, p, docs, template_fields, renderers)
            supplied.update(k.super_kwargs)
        hooks: list[ClassInfo] = []
        for k in lineage:
            for ref in k.hook_refs:
                target = self.resolve(k.module, ref)
                if isinstance(target, ClassInfo) and target not in hooks:
                    hooks.append(target)
        return StaticClassResult(
            info=cls,
            root=root,
            is_sensor=is_sensor,
            lineage=lineage,
            params=params,
            template_fields=template_fields,
            renderers=renderers,
            ui_color=ui if isinstance(ui, str) else None,
            docstring=inspect.cleandoc(cls.doc or ""),
            summary=summarize_doc(cls.doc or ""),
            hook_refs=hooks,
        )

    def _param(
        self,
        owner: ClassInfo,
        p: ParamDef,
        docs: dict[str, str],
        template_fields: list[str],
        renderers: dict[str, str],
    ) -> StaticParam:
        schema, supported = annotation_schema(p.annotation)
        schema = dict(schema)
        schema.setdefault("title", p.name.replace("_", " ").capitalize())
        if shown := short_annotation(p.annotation):
            schema["x-autoair-annotation"] = shown
        if p.name in docs:
            schema["description"] = docs[p.name]
        has_default = p.default is not None
        notset = has_default and (p.default or "").rsplit(".", 1)[-1] in _NOTSET
        default: Any = _MISSING
        if has_default and not notset:
            default = self._eval_value(owner.module, p.default or "", owner)
            if default is not _MISSING and _jsonable(default):
                if isinstance(default, tuple):
                    default = list(default)
                schema["default"] = default
            else:
                default = _MISSING
        if p.annotation is None and default is not _MISSING and "type" not in schema:
            # untyped parameter: infer a (lenient) type from its literal default
            inferred = {str: "string", bool: "boolean", int: "number", float: "number"}.get(
                type(default)
            )
            if inferred:
                schema["type"] = inferred
        if p.name in template_fields:
            schema["x-autoair-templated"] = True
            renderer = renderers.get(p.name) or next(
                (v for k, v in _RENDERER_HINTS.items() if p.name.endswith(k)), None
            )
            if renderer:
                schema["x-autoair-renderer"] = renderer
        if _CONN_RE.search(p.name):
            schema["x-autoair-widget"] = "connection"
        elif p.name == "pool" or p.name.endswith("_pool"):
            schema["x-autoair-widget"] = "pool"
        if not supported:
            schema[PYTHON_REF] = "required"  # only a Python reference can express it
        return StaticParam(
            name=p.name,
            schema=schema,
            # NOTSET / ArgNotSet defaults mean "not set": the param is optional.
            required=not has_default,
            supported=supported,
            default=None if default is _MISSING else default,
            has_default=default is not _MISSING,
        )

    def hook_conn_type(self, hook: ClassInfo) -> tuple[str | None, str | None]:
        """``(conn_type, default_conn_name)`` of a hook class (literal attributes only)."""
        ct = self.class_attr(hook, "conn_type")
        dn = self.class_attr(hook, "default_conn_name")
        return (
            ct if isinstance(ct, str) and ct else None,
            dn if isinstance(dn, str) and dn else None,
        )


def _dedupe(items: Iterable[str]) -> list[str]:
    out: list[str] = []
    for i in items:
        if i not in out:
            out.append(i)
    return out


def _jsonable(value: Any) -> bool:
    if value is None or isinstance(value, (str, bool, int, float)):
        return True
    if isinstance(value, (list, tuple)):
        return all(_jsonable(v) for v in value)
    if isinstance(value, dict):
        return all(isinstance(k, str) and _jsonable(v) for k, v in value.items())
    return False


def candidate_classes(info: ModuleInfo) -> list[ClassInfo]:
    """Public, concrete classes defined in a module (``Base*`` / ``_*`` excluded)."""
    return [
        c for c in info.classes.values() if not c.name.startswith(("_", "Base")) and not c.abstract
    ]


def looks_like_task_class(cls: ClassInfo) -> bool:
    """Heuristic for unresolvable classes worth an import-based fallback."""
    names = [cls.name, *cls.bases]
    return any(re.search(r"(Operator|Sensor|Notifier)\b", n) for n in names)


__all__ = [
    "ClassInfo",
    "ModuleInfo",
    "ParamDef",
    "Root",
    "StaticAnalyzer",
    "StaticClassResult",
    "StaticParam",
    "Unresolvable",
    "annotation_schema",
    "candidate_classes",
    "clear_locator_cache",
    "find_module_file",
    "looks_like_task_class",
    "parse_module",
]
