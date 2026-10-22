"""Shared state while turning a :class:`DagSpec` into a Python AST."""

from __future__ import annotations

import ast
import datetime
import keyword
import math
import re
import sys
from dataclasses import dataclass, field
from typing import Any

from autoair._compat import airflow_version, sdk_symbol_module
from autoair.spec import (
    AssetAliasLeaf,
    AssetCombination,
    AssetExpr,
    AssetLeaf,
    AssetRef,
    BlueprintNode,
    CallbackRef,
    Callbacks,
    DagSpec,
    FileAsset,
    InlineFunction,
    Node,
    NoteNode,
    OutputModel,
    PythonRef,
    PythonTaskNode,
    RetryPolicySpec,
    TaskGroupNode,
    TaskNode,
    ToolsetSpec,
)

SDK = "airflow.sdk"
# Modules the generated file may ``import`` by name: a function, task or variable with one of
# these names would shadow the module for every later use (the materialised-script helper
# runs ``os.path`` at parse time).
PLAIN_MODULES = frozenset({"contextlib", "datetime", "pendulum", "hashlib", "os", "tempfile"})
# ``datetime.timedelta(seconds=...)`` raises OverflowError beyond timedelta.max / .min.
MAX_TIMEDELTA_SECONDS = 999_999_999 * 86_400 + 86_399


def timedelta_problem(seconds: float) -> str | None:
    """Why ``datetime.timedelta(seconds=seconds)`` cannot be generated, or None."""
    if not math.isfinite(seconds):
        return f"duration {seconds!r} is not a finite number of seconds"
    if abs(seconds) > MAX_TIMEDELTA_SECONDS:
        return (
            f"duration of {seconds:g} seconds is larger than a datetime.timedelta can hold "
            f"(at most {MAX_TIMEDELTA_SECONDS} seconds)"
        )
    return None


def inline_import_names(source: str) -> set[str]:
    """Module-level names the imports of an inline function bind (they are emitted at the
    top of the generated file)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    names: set[str] = set()
    for stmt in tree.body:
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            names |= {(a.asname or a.name).split(".")[0] for a in stmt.names if a.name != "*"}
    return names


def spec_inline_import_names(spec: DagSpec) -> set[str]:
    names: set[str] = set()
    stack: list[Any] = [spec.model_dump(by_alias=True, include={"nodes"})]
    while stack:
        value = stack.pop()
        if isinstance(value, dict):
            if value.get("$type") == "function" and isinstance(value.get("source"), str):
                names |= inline_import_names(value["source"])
            else:
                stack.extend(value.values())
        elif isinstance(value, list):
            stack.extend(value)
    return names


COMMON_AI_TOOLSETS = {
    "sql": ("airflow.providers.common.ai.toolsets.sql", "SQLToolset", "db_conn_id"),
    "mcp": ("airflow.providers.common.ai.toolsets.mcp", "MCPToolset", "mcp_conn_id"),
}
MESSAGE_QUEUE_TRIGGER = (
    "airflow.providers.common.messaging.triggers.msg_queue",
    "MessageQueueTrigger",
)


class CodegenError(ValueError):
    """Raised when a spec cannot be turned into code (should be caught by validation)."""

    def __init__(self, message: str, node_id: str | None = None) -> None:
        super().__init__(message)
        self.node_id = node_id


def _sanitize(name: str) -> str:
    ident = re.sub(r"\W", "_", name)
    if not ident or ident[0].isdigit():
        ident = f"_{ident}"
    if keyword.iskeyword(ident):
        ident = f"{ident}_"
    return ident


MATERIALIZE_TEMPLATE = '''
def {name}(name: str, content: str) -> str:
    """Write a script embedded in this DAG to a content-addressed file; return its path.

    Runs when the DAG is parsed (on every worker), so the file exists wherever tasks run.
    The folder is private to this user (0700, checked: not a symlink, owned by us, no group
    or other access), and an existing file is used only when it is a regular file of ours
    with exactly this content; anything else is replaced atomically.
    """
    uid = os.getuid() if hasattr(os, "getuid") else None

    def private(folder: str) -> bool:
        try:
            os.mkdir(folder, 0o700)
        except FileExistsError:
            pass
        st = os.lstat(folder)
        if os.path.islink(folder) or not os.path.isdir(folder):
            return False
        return uid is None or (st.st_uid == uid and not st.st_mode & 0o077)

    owner = uid if uid is not None else "user"
    root = os.path.join(tempfile.gettempdir(), f"airflow-scripts-{{owner}}")
    if not private(root):
        root = tempfile.mkdtemp(prefix="airflow-scripts-")  # never trust a foreign folder
    digest = hashlib.sha256(content.encode()).hexdigest()[:16]
    folder = root
    for part in ({dag_id!r}, digest):
        folder = os.path.join(folder, part)
        if not private(folder):
            raise RuntimeError(f"unsafe script folder {{folder}}")
    path = os.path.join(folder, name)
    expected = content.encode()
    try:
        st = os.lstat(path)
        if (
            not os.path.islink(path)
            and os.path.isfile(path)
            and (uid is None or st.st_uid == uid)
            and st.st_size == len(expected)
        ):
            with open(path, "rb") as fh:
                if fh.read() == expected:
                    return path
    except FileNotFoundError:
        pass
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".script-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(expected)
        os.chmod(tmp, 0o700)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
    return path
'''


def parse_inline_function(
    source: str,
) -> tuple[ast.FunctionDef | ast.AsyncFunctionDef, list[ast.stmt]]:
    """``(def, imports)`` of an inline function; anything else at top level is rejected."""
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        raise CodegenError(f"inline function: line {exc.lineno}: {exc.msg}") from exc
    imports: list[ast.stmt] = []
    funcs: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    for stmt in tree.body:
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            imports.append(stmt)
        elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs.append(stmt)
        elif isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            continue  # docstring / comment string
        else:
            raise CodegenError(
                "an inline function may only contain imports and one `def` at top level"
            )
    if len(funcs) != 1:
        raise CodegenError(f"an inline function needs exactly one `def` (found {len(funcs)})")
    return funcs[0], imports


def _import_bindings(stmt: ast.stmt) -> list[tuple[str, str]]:
    """``(name, target)`` for each name an import statement binds in its scope."""
    out: list[tuple[str, str]] = []
    if isinstance(stmt, ast.Import):
        for alias in stmt.names:
            if alias.asname:
                out.append((alias.asname, f"import {alias.name}"))
            else:
                top = alias.name.split(".")[0]
                out.append((top, f"import {top}"))
    elif isinstance(stmt, ast.ImportFrom):
        base = "." * stmt.level + (stmt.module or "")
        for alias in stmt.names:
            out.append((alias.asname or alias.name, f"from {base} import {alias.name}"))
    return out


@dataclass
class CodegenContext:
    spec: DagSpec
    airflow_version: tuple[int, int, int] | None = None
    # module -> {name -> local alias}
    _from_imports: dict[str, dict[str, str]] = field(default_factory=dict)
    _plain_imports: set[str] = field(default_factory=set)
    _used_names: set[str] = field(default_factory=lambda: {"dag", *PLAIN_MODULES})
    _node_vars: dict[str, str] = field(default_factory=dict)
    # Tasks defined inside a loop group's @task_group function: the group is implicit.
    in_loop_body: set[str] = field(default_factory=set)
    # Module-level statements emitted before ``with DAG`` (inline functions, helpers).
    module_defs: list[ast.stmt] = field(default_factory=list)
    _inline_functions: dict[str, str] = field(default_factory=dict)  # source -> name
    _output_models: dict[str, str] = field(default_factory=dict)  # model json -> class name
    _module_imports: set[str] = field(default_factory=set)
    # Module-level name bound by an inline function's import -> what it is bound to.
    _inline_bindings: dict[str, str] = field(default_factory=dict)
    _materialize: str | None = None
    # spec.node_map() once per render: looking nodes up per reference was O(n^2) on large DAGs.
    _node_map: dict[str, Node] | None = None

    def __post_init__(self) -> None:
        # Names bound by inline functions' imports are module-level too: reserve them before
        # any task function or variable takes one.
        self._used_names |= spec_inline_import_names(self.spec)

    @property
    def nodes(self) -> dict[str, Node]:
        if self._node_map is None:
            self._node_map = self.spec.node_map()
        return self._node_map

    # ----------------------------------------------------------------- names
    def _reserve(self, base: str) -> str:
        name, i = base, 2
        while name in self._used_names or keyword.iskeyword(name):
            name = f"{base}_{i}"
            i += 1
        self._used_names.add(name)
        return name

    def import_name(self, module: str, name: str) -> ast.Name:
        """Return a reference to ``module.name``, adding ``from module import name``."""
        aliases = self._from_imports.setdefault(module, {})
        if name not in aliases:
            aliases[name] = self._reserve(name)
        return ast.Name(id=aliases[name], ctx=ast.Load())

    def import_module(self, module: str) -> ast.Name:
        self._plain_imports.add(module)
        self._used_names.add(module.split(".")[0])
        return ast.Name(id=module, ctx=ast.Load())

    def sdk(self, name: str) -> ast.Name:
        """Authoring symbol, imported from where the target Airflow version defines it."""
        return self.import_name(sdk_symbol_module(name, self.airflow_version), name)

    def node_var(self, node_id: str) -> str:
        if node_id not in self._node_vars:
            node = self.nodes[node_id]
            prefix = "g" if isinstance(node, (TaskGroupNode, BlueprintNode)) else "t"
            if isinstance(node, TaskGroupNode):
                key = node.group_id
            elif isinstance(node, BlueprintNode):
                key = node.step_id
            elif isinstance(node, NoteNode):
                raise CodegenError("notes are not part of the generated DAG", node_id)
            else:
                key = node.task_id
            self._node_vars[node_id] = self._reserve(f"{prefix}_{_sanitize(key)}")
        return self._node_vars[node_id]

    def reserve_function_name(self, base: str) -> str:
        return self._reserve(_sanitize(base))

    def import_statements(self) -> list[ast.stmt]:
        stmts: list[ast.stmt] = [
            ast.Import(names=[ast.alias(name=m)]) for m in sorted(self._plain_imports)
        ]
        for module in sorted(self._from_imports, key=lambda m: (m != SDK, m)):
            names = self._from_imports[module]
            stmts.append(
                ast.ImportFrom(
                    module=module,
                    names=[
                        ast.alias(name=n, asname=None if a == n else a)
                        for n, a in sorted(names.items())
                    ],
                    level=0,
                )
            )
        return stmts

    # ----------------------------------------------------------------- values
    def xcom_expr(self, node_id: str, key: str | None) -> ast.expr:
        node = self.nodes.get(node_id)
        if node is None or isinstance(node, (TaskGroupNode, BlueprintNode)):
            raise CodegenError(f"XCom reference to unknown task node {node_id!r}")
        base: ast.expr = ast.Name(id=self.node_var(node_id), ctx=ast.Load())
        if not isinstance(node, PythonTaskNode):
            base = ast.Attribute(value=base, attr="output", ctx=ast.Load())
        if key is not None:
            base = ast.Subscript(value=base, slice=ast.Constant(key), ctx=ast.Load())
        return base

    def timedelta(self, seconds: float) -> ast.expr:
        problem = timedelta_problem(float(seconds))
        if problem is not None:
            raise CodegenError(problem)
        mod = self.import_module("datetime")
        return ast.Call(
            func=ast.Attribute(value=mod, attr="timedelta", ctx=ast.Load()),
            args=[],
            keywords=[ast.keyword(arg="seconds", value=ast.Constant(_num(seconds)))],
        )

    def datetime_in(self, value: datetime.datetime, tz: str | None) -> ast.expr:
        """A DAG date: with a DAG timezone, ``pendulum.datetime(..., tz=...)`` holding the
        wall-clock time in that zone (aware values are converted to it first)."""
        if not tz:
            return self.datetime(value)
        import zoneinfo

        if value.tzinfo is not None:
            value = value.astimezone(zoneinfo.ZoneInfo(tz))
        parts = [value.year, value.month, value.day, value.hour, value.minute, value.second]
        parts.append(value.microsecond)  # pendulum.datetime(..., second, microsecond, tz=)
        while len(parts) > 3 and parts[-1] == 0:
            parts.pop()
        return ast.Call(
            func=ast.Attribute(
                value=self.import_module("pendulum"), attr="datetime", ctx=ast.Load()
            ),
            args=[ast.Constant(p) for p in parts],
            keywords=[ast.keyword(arg="tz", value=ast.Constant(tz))],
        )

    def datetime(self, value: datetime.datetime) -> ast.expr:
        mod = self.import_module("datetime")
        parts = [value.year, value.month, value.day, value.hour, value.minute, value.second]
        if value.microsecond:
            parts.append(value.microsecond)
        while len(parts) > 3 and parts[-1] == 0:
            parts.pop()
        keywords: list[ast.keyword] = []
        if value.tzinfo is not None:
            offset = value.utcoffset() or datetime.timedelta(0)
            tz_mod = ast.Attribute(value=mod, attr="timezone", ctx=ast.Load())
            tz: ast.expr
            if offset == datetime.timedelta(0):
                tz = ast.Attribute(value=tz_mod, attr="utc", ctx=ast.Load())
            else:
                tz = ast.Call(
                    func=tz_mod, args=[self.timedelta(offset.total_seconds())], keywords=[]
                )
            keywords.append(ast.keyword(arg="tzinfo", value=tz))
        return ast.Call(
            func=ast.Attribute(value=mod, attr="datetime", ctx=ast.Load()),
            args=[ast.Constant(p) for p in parts],
            keywords=keywords,
        )

    def value(self, value: Any) -> ast.expr:
        """Convert a JSON (+tagged) parameter value to a *literal* AST expression."""
        if value is None or isinstance(value, (bool, str)):
            return ast.Constant(value)
        if isinstance(value, (int, float)):
            if isinstance(value, float) and not math.isfinite(value):
                raise CodegenError(f"non-finite number {value!r} is not allowed")
            return ast.Constant(value)
        if isinstance(value, list):
            return ast.List(elts=[self.value(v) for v in value], ctx=ast.Load())
        if isinstance(value, dict):
            tag = value.get("$type")
            if tag == "xcom":
                return self.xcom_expr(str(value["node"]), value.get("key"))
            if tag == "timedelta":
                return self.timedelta(float(value["seconds"]))
            if tag == "datetime":
                dt = value["value"]
                if isinstance(dt, str):
                    # Browsers send "...Z" (toISOString); Python 3.10 rejects the suffix.
                    dt = datetime.datetime.fromisoformat(dt.replace("Z", "+00:00"))
                return self.datetime(dt)
            if tag == "import":
                ref = PythonRef.model_validate(value)  # identifier path: no injection
                return self.import_name(ref.module, ref.name)
            if tag == "function":
                return self.inline_function(InlineFunction.model_validate(value).source)
            if tag == "file":
                return self.file_asset(FileAsset.model_validate(value))
            if tag == "output_model":
                return self.output_model(OutputModel.model_validate(value))
            if tag == "toolset":
                return self.toolset(ToolsetSpec.model_validate(value))
            if tag is not None:
                raise CodegenError(f"unknown tagged value type {tag!r}")
            keys: list[ast.expr | None] = []
            for k in value:
                if not isinstance(k, str):
                    raise CodegenError("dictionary keys must be strings")
                keys.append(ast.Constant(k))
            return ast.Dict(keys=keys, values=[self.value(v) for v in value.values()])
        if isinstance(value, datetime.datetime):
            return self.datetime(value)
        raise CodegenError(f"unsupported value of type {type(value).__name__}")

    # ----------------------------------------------------------- inline code
    def inline_function(self, source: str) -> ast.Name:
        """Emit ``source`` (imports + one ``def``) at module level; returns its name."""
        if source in self._inline_functions:
            return ast.Name(id=self._inline_functions[source], ctx=ast.Load())
        func, imports = parse_inline_function(source)
        bindings = [b for stmt in imports for b in _import_bindings(stmt)]
        if any(self._inline_bindings.get(name, what) != what for name, what in bindings):
            # Another inline function binds one of these names to something else (``floor
            # as calc`` vs ``ceil as calc``): a shared module-level import would make both
            # use one object. This function imports inside its own body instead.
            docstring = (
                1
                if func.body
                and isinstance(func.body[0], ast.Expr)
                and isinstance(func.body[0].value, ast.Constant)
                and isinstance(func.body[0].value.value, str)
                else 0
            )
            func.body[docstring:docstring] = imports
            imports = []
        for name, what in bindings if imports else []:
            self._inline_bindings.setdefault(name, what)
        for stmt in imports:
            key = ast.dump(stmt)
            if key not in self._module_imports:
                self._module_imports.add(key)
                self.module_defs.insert(0, stmt)
        func.name = self._reserve(func.name if func.name.isidentifier() else "fn")
        self.module_defs.append(func)
        self._inline_functions[source] = func.name
        return ast.Name(id=func.name, ctx=ast.Load())

    def output_model(self, model: OutputModel) -> ast.Name:
        """``class <Name>(BaseModel)`` at module level (common.ai ``output_type``): the
        class must be importable from the DAG module for XCom deserialization. Annotations
        come from a closed set of types and every text is a constant: no user code."""
        key = model.model_dump_json()
        if key in self._output_models:
            return ast.Name(id=self._output_models[key], ctx=ast.Load())
        base = self.import_name("pydantic", "BaseModel")
        field_fn = self.import_name("pydantic", "Field")
        body: list[ast.stmt] = []
        if model.description:
            body.append(ast.Expr(value=ast.Constant(model.description)))
        for f in model.fields:
            annotation: ast.expr
            if f.choices:
                literal = self.import_name("typing", "Literal")
                elts: list[ast.expr] = [ast.Constant(c) for c in f.choices]
                annotation = ast.Subscript(
                    value=literal,
                    slice=elts[0] if len(elts) == 1 else ast.Tuple(elts=elts, ctx=ast.Load()),
                    ctx=ast.Load(),
                )
            else:
                annotation = ast.parse(f.type, mode="eval").body  # closed Literal set
            if not f.required:
                annotation = ast.BinOp(left=annotation, op=ast.BitOr(), right=ast.Constant(None))
            kws: list[ast.keyword] = []
            if not f.required:
                kws.append(ast.keyword(arg="default", value=ast.Constant(None)))
            if f.description:
                kws.append(ast.keyword(arg="description", value=ast.Constant(f.description)))
            default: ast.expr | None = (
                ast.Call(func=field_fn, args=[], keywords=kws) if kws else None
            )
            body.append(
                ast.AnnAssign(
                    target=ast.Name(id=f.name, ctx=ast.Store()),
                    annotation=annotation,
                    value=default,
                    simple=1,
                )
            )
        name = self._reserve(model.name)
        cls = ast.ClassDef(
            name=name, bases=[base], keywords=[], body=body or [ast.Pass()], decorator_list=[]
        )
        if sys.version_info >= (3, 12):
            cls.type_params = []
        self.module_defs.append(cls)
        self._output_models[key] = name
        return ast.Name(id=name, ctx=ast.Load())

    def toolset(self, spec: ToolsetSpec) -> ast.expr:
        """``SQLToolset(db_conn_id=...)`` / ``MCPToolset(mcp_conn_id=...)``."""
        module, cls, conn_arg = COMMON_AI_TOOLSETS[spec.kind]
        kws = [ast.keyword(arg=conn_arg, value=ast.Constant(spec.conn_id))]
        options: dict[str, Any] = {
            "allowed_tables": spec.allowed_tables,
            "schema": spec.db_schema,
            "allow_writes": spec.allow_writes or None,
            "max_rows": spec.max_rows,
            "tool_prefix": spec.tool_prefix,
        }
        kws += [
            ast.keyword(arg=k, value=self.value(v)) for k, v in options.items() if v is not None
        ]
        return ast.Call(func=self.import_name(module, cls), args=[], keywords=kws)

    def file_asset(self, asset: FileAsset) -> ast.expr:
        """``<helper>(name, content)`` → path of the script, materialised at parse time."""
        helper = self._materializer()
        call: ast.expr = ast.Call(
            func=ast.Name(id=helper, ctx=ast.Load()),
            args=[ast.Constant(asset.name), ast.Constant(asset.content)],
            keywords=[],
        )
        if asset.run_with:  # "<interpreter> <path> " (trailing space: not a template file)
            call = ast.BinOp(
                left=ast.BinOp(left=ast.Constant(f"{asset.run_with} "), op=ast.Add(), right=call),
                op=ast.Add(),
                right=ast.Constant(" "),
            )
        return call

    def _materializer(self) -> str:
        if self._materialize:
            return self._materialize
        name = self._reserve("_script_file")
        for module in ("contextlib", "hashlib", "os", "tempfile"):
            self.import_module(module)
        template = MATERIALIZE_TEMPLATE.format(name=name, dag_id=self.spec.dag_id)
        self.module_defs.append(ast.parse(template).body[0])
        self._materialize = name
        return name

    def call(self, func: ast.expr, *args: ast.expr, **kwargs: ast.expr) -> ast.Call:
        return ast.Call(
            func=func,
            args=list(args),
            keywords=[ast.keyword(arg=k, value=v) for k, v in kwargs.items()],
        )

    def import_path(self, fqn: str) -> ast.Name:
        module, _, name = fqn.rpartition(".")
        return self.import_name(module, name)

    def asset(self, ref: AssetRef) -> ast.expr:
        kws = [
            ast.keyword(arg=k, value=ast.Constant(v))
            for k, v in (("name", ref.name), ("uri", ref.uri), ("group", ref.group))
            if v is not None
        ]
        if ref.extra:
            kws.append(ast.keyword(arg="extra", value=self.value(ref.extra)))
        if ref.watchers:
            trigger_cls = self.import_name(*MESSAGE_QUEUE_TRIGGER)
            watchers: list[ast.expr] = [
                self.call(
                    self.sdk("AssetWatcher"),
                    name=ast.Constant(w.name),
                    trigger=ast.Call(
                        func=trigger_cls,
                        args=[],
                        keywords=[
                            ast.keyword(arg="scheme", value=ast.Constant(w.scheme)),
                            *(ast.keyword(arg=k, value=self.value(v)) for k, v in w.kwargs.items()),
                        ],
                    ),
                )
                for w in ref.watchers
            ]
            kws.append(ast.keyword(arg="watchers", value=ast.List(elts=watchers, ctx=ast.Load())))
        return ast.Call(func=self.sdk("Asset"), args=[], keywords=kws)

    def asset_expr(self, expr: AssetExpr) -> ast.expr:
        if isinstance(expr, AssetLeaf):
            return self.asset(expr.asset)
        if isinstance(expr, AssetAliasLeaf):
            return self.call(self.sdk("AssetAlias"), ast.Constant(expr.name))
        assert isinstance(expr, AssetCombination)
        if len(expr.items) == 1:
            return self.asset_expr(expr.items[0])
        func = self.sdk("AssetAll" if expr.op == "all" else "AssetAny")
        return ast.Call(func=func, args=[self.asset_expr(i) for i in expr.items], keywords=[])

    # ----------------------------------------------------------------- tasks
    def task_kwargs(self, node: TaskNode, *, include_task_id: bool = True) -> list[ast.keyword]:
        """Keyword arguments shared by every task kind."""
        kws: list[ast.keyword] = []

        def add(arg: str, value: ast.expr) -> None:
            kws.append(ast.keyword(arg=arg, value=value))

        if include_task_id:
            add("task_id", ast.Constant(node.task_id))
        if node.parent_group and node.id not in self.in_loop_body:
            add("task_group", ast.Name(id=self.node_var(node.parent_group), ctx=ast.Load()))
        if node.trigger_rule:
            add("trigger_rule", ast.Constant(node.trigger_rule))
        if node.retries is not None:
            add("retries", ast.Constant(node.retries))
        if node.retry_delay_seconds is not None:
            add("retry_delay", self.timedelta(node.retry_delay_seconds))
        if node.execution_timeout_seconds is not None:
            add("execution_timeout", self.timedelta(node.execution_timeout_seconds))
        for attr in ("pool", "pool_slots", "queue", "owner", "doc_md"):
            val = getattr(node, attr)
            if val is not None:
                add(attr, ast.Constant(val))
        if node.outlets:
            add("outlets", ast.List(elts=[self.asset(a) for a in node.outlets], ctx=ast.Load()))
        if node.inlets:
            add("inlets", ast.List(elts=[self.asset(a) for a in node.inlets], ctx=ast.Load()))
        for attr in (
            "priority_weight",
            "weight_rule",
            "task_display_name",
            "executor",
            "max_active_tis_per_dagrun",
            "depends_on_past",
        ):
            val = getattr(node, attr)
            if val is not None:
                add(attr, ast.Constant(val))
        if node.retry_exponential_backoff is not None:
            backoff: float | bool = node.retry_exponential_backoff
            if (self.airflow_version or airflow_version() or (3, 2, 0)) < (3, 2, 0):
                # Before 3.2 the argument is a bool (backoff with base 2), not a multiplier.
                backoff = backoff > 0
            add("retry_exponential_backoff", ast.Constant(backoff))
        if node.executor_config is not None:
            add("executor_config", self.value(node.executor_config))
        if node.mapping and node.mapping.map_index_template:
            add("map_index_template", ast.Constant(node.mapping.map_index_template))
        if node.retry_policy is not None:
            add("retry_policy", self.retry_policy(node.retry_policy))
        kws.extend(self.callback_kwargs(node.callbacks))
        return kws

    # ----------------------------------------------------------------- behaviours
    def notifier(self, ref: CallbackRef) -> ast.expr:
        return ast.Call(
            func=self.import_path(ref.notifier),
            args=[],
            keywords=[ast.keyword(arg=k, value=self.value(v)) for k, v in ref.params.items()],
        )

    def callback_kwargs(self, callbacks: Callbacks) -> list[ast.keyword]:
        out: list[ast.keyword] = []
        for name in ("on_success", "on_failure", "on_retry", "on_execute", "on_skipped"):
            refs: list[CallbackRef] = getattr(callbacks, name)
            if refs:
                out.append(
                    ast.keyword(
                        arg=f"{name}_callback",
                        value=ast.List(elts=[self.notifier(r) for r in refs], ctx=ast.Load()),
                    )
                )
        return out

    def retry_policy(self, policy: RetryPolicySpec) -> ast.expr:
        action = self.sdk("RetryAction")

        def act(name: str) -> ast.expr:
            return ast.Attribute(value=action, attr=name.upper(), ctx=ast.Load())

        rules: list[ast.expr] = []
        for rule in policy.rules:
            kws: dict[str, ast.expr] = {
                "exception": ast.Constant(rule.exception),
                "action": act(rule.action),
            }
            if rule.retry_delay_seconds is not None:
                kws["retry_delay"] = self.timedelta(rule.retry_delay_seconds)
            if rule.reason:
                kws["reason"] = ast.Constant(rule.reason)
            if not rule.match_subclasses:
                kws["match_subclasses"] = ast.Constant(False)
            rules.append(self.call(self.sdk("RetryRule"), **kws))
        kwargs: dict[str, ast.expr] = {"rules": ast.List(elts=rules, ctx=ast.Load())}
        if policy.default != "default":
            kwargs["default"] = act(policy.default)
        return self.call(self.sdk("ExceptionRetryPolicy"), **kwargs)


def _num(value: float) -> int | float:
    return int(value) if float(value).is_integer() else value
