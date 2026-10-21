"""Static map of a Python DAG file (LibCST): which CST node defines which task.

Nothing is executed. The map answers, for one DAG of the file:

* the DAG definition (``with DAG(...)``, ``@dag(...)``, ``dag = DAG(...)``) and its call;
* each task with a *literal* ``task_id``: classic operator calls (also ``Op.partial(...)
  .expand(...)``), TaskFlow functions (``@task`` / ``@task.<kind>``: the task id is the
  ``task_id=`` of the decorator or the function name) and their call sites;
* task groups (``with TaskGroup(...)``, ``g = TaskGroup(...)``) and their members;
* dependency statements (``a >> b``, ``a << b``, ``[a, b] >> c``, labels, ``chain(...)``,
  ``a.set_downstream(b)``) as task-level edges.

Tasks the map cannot pin to one node -- non-literal ids, tasks built in loops, comprehensions
or factory functions, TaskFlow functions called more than once -- are *limited*: the patcher
(:mod:`autoair.preserve.patcher`) refuses structural changes to them and every change it
cannot locate. The map only ever narrows what is patched; it never guesses.
"""

from __future__ import annotations

import builtins
import itertools
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Literal

import libcst as cst

DAG_CLASS_NAMES = frozenset({"DAG"})
DAG_DECORATOR_NAMES = frozenset({"dag"})
TASK_DECORATOR_NAMES = frozenset({"task"})
TASK_GROUP_CLASS_NAMES = frozenset({"TaskGroup"})
TASK_GROUP_DECORATOR_NAMES = frozenset({"task_group"})
CHAIN_NAMES = frozenset({"chain", "chain_linear"})
LABEL_NAMES = frozenset({"Label"})
GROUP_PREFIX = "@"  # edge endpoints that are task groups: "@<group id>"

Container = cst.Module | cst.IndentedBlock


class MapError(ValueError):
    """The file cannot be mapped (syntax error, DAG not found)."""


@dataclass(eq=False)
class TaskSite:
    """Where one task is defined."""

    key: str  # Airflow task id (group prefix applied)
    own_id: str  # the id as written
    kind: Literal["operator", "taskflow"]
    # The call carrying the task's keyword arguments: the operator constructor (or its
    # ``.partial(...)``) / the TaskFlow decorator call (None for a bare ``@task``).
    call: cst.Call | None
    expand: cst.Call | None = None  # ``.expand(...)`` / ``.expand_kwargs(...)``
    func: cst.FunctionDef | None = None  # TaskFlow function
    decorator: cst.Decorator | None = None
    invocation: cst.Call | None = None  # TaskFlow call site ``fn(...)``
    statement: cst.SimpleStatementLine | None = None  # the line that only defines this task
    container: Container | None = None  # block holding ``statement``
    def_container: Container | None = None  # block holding the TaskFlow function
    var: str | None = None
    group: str | None = None  # group key
    task_id_arg: cst.Arg | None = None  # literal task_id argument (None: function name)
    limited: str | None = None  # why structural changes are refused


@dataclass(eq=False)
class GroupSite:
    key: str
    own_id: str
    call: cst.Call
    prefix: bool
    parent: str | None
    var: str | None = None
    with_stmt: cst.With | None = None
    statement: cst.SimpleStatementLine | None = None
    container: Container | None = None
    limited: str | None = None


@dataclass(eq=False)
class DepStatement:
    """A dependency statement and the task-level edges it creates."""

    statement: cst.SimpleStatementLine
    container: Container
    kind: Literal["binop", "chain", "method"]
    edges: set[tuple[str, str]]
    labels: dict[tuple[str, str], str] = field(default_factory=dict)
    # binop chains: real operands (CST) with their resolved endpoint keys, and the operator
    # between operand i and i+1 (">>" / "<<"); None when an operand is not resolvable.
    operands: list[tuple[cst.BaseExpression, list[str]]] | None = None
    ops: list[cst.BaseBinaryOp] = field(default_factory=list)
    has_labels: bool = False


@dataclass(eq=False)
class DagSite:
    dag_id: str
    style: Literal["with", "decorator", "assign"]
    call: cst.Call | None  # DAG(...) / @dag(...) (None: bare @dag)
    statement: cst.CSTNode  # the With / FunctionDef / assignment line
    body: Container  # where the DAG's tasks are written
    var: str | None = None
    decorator: cst.Decorator | None = None
    func: cst.FunctionDef | None = None
    dag_id_arg: cst.Arg | None = None


@dataclass(eq=False)
class SourceMap:
    module: cst.Module
    dag: DagSite
    tasks: dict[str, TaskSite]
    groups: dict[str, GroupSite]
    deps: list[DepStatement]
    unmapped: list[str]  # human-readable notes on code the map could not pin down
    var_keys: dict[str, str]  # variable -> task key / "@group key"
    bound_names: set[str]  # names bound at module level and in the DAG body
    implicit_edges: set[tuple[str, str]]  # data dependencies (a task var passed to a task)
    dag_ids: list[str]  # every DAG the file defines (literal ids)

    def task_var(self, key: str) -> str | None:
        site = self.tasks.get(key)
        return site.var if site else None

    def endpoint_var(self, key: str) -> str | None:
        if key.startswith(GROUP_PREFIX):
            group = self.groups.get(key[len(GROUP_PREFIX) :])
            return group.var if group else None
        return self.task_var(key)


# --------------------------------------------------------------------------- helpers


def literal_string(node: cst.BaseExpression | None) -> str | None:
    """The value of a plain string literal (no f-string, no concatenation of f-strings)."""
    if isinstance(node, cst.SimpleString):
        value = node.evaluated_value
        return value if isinstance(value, str) else None
    if isinstance(node, cst.ConcatenatedString):
        left, right = literal_string(node.left), literal_string(node.right)
        return left + right if left is not None and right is not None else None
    return None


def literal_bool(node: cst.BaseExpression | None) -> bool | None:
    if isinstance(node, cst.Name) and node.value in ("True", "False"):
        return node.value == "True"
    return None


def keyword(call: cst.Call | None, name: str) -> cst.Arg | None:
    if call is None:
        return None
    for arg in call.args:
        if arg.keyword is not None and arg.keyword.value == name:
            return arg
    return None


def dotted(node: cst.BaseExpression) -> str | None:
    if isinstance(node, cst.Name):
        return node.value
    if isinstance(node, cst.Attribute):
        base = dotted(node.value)
        return f"{base}.{node.attr.value}" if base else None
    return None


@dataclass
class _Imports:
    """Local names bound by ``import`` statements -> the name they import."""

    names: dict[str, str] = field(default_factory=dict)

    def resolve(self, expr: cst.BaseExpression) -> str | None:
        """The imported (last) name ``expr`` stands for: ``DAG`` for ``DAG``, ``models.DAG``,
        ``airflow.DAG`` or an alias of them."""
        path = dotted(expr)
        if path is None:
            return None
        head, _, rest = path.partition(".")
        if not rest:
            return self.names.get(head, head)
        return path.rsplit(".", 1)[-1]


def _collect_imports(module: cst.Module) -> _Imports:
    out = _Imports()
    for stmt in module.body:
        if not isinstance(stmt, cst.SimpleStatementLine):
            continue
        for small in stmt.body:
            if isinstance(small, cst.ImportFrom) and not isinstance(small.names, cst.ImportStar):
                for alias in small.names:
                    name = dotted(alias.name) or ""
                    local = (
                        alias.asname.name.value
                        if alias.asname and isinstance(alias.asname.name, cst.Name)
                        else name
                    )
                    out.names[local] = name.rsplit(".", 1)[-1]
    return out


def bound_names(statements: Sequence[cst.BaseStatement]) -> set[str]:
    """Names bound by ``statements`` (imports, definitions, assignments, ``with ... as``,
    loop targets), not descending into functions or classes."""
    names: set[str] = set()

    def targets(expr: cst.BaseExpression) -> None:
        if isinstance(expr, cst.Name):
            names.add(expr.value)
        elif isinstance(expr, (cst.Tuple, cst.List)):
            for el in expr.elements:
                targets(el.value)
        elif isinstance(expr, cst.StarredElement):
            targets(expr.value)

    def visit(stmts: Sequence[cst.BaseStatement]) -> None:
        for stmt in stmts:
            if isinstance(stmt, (cst.FunctionDef, cst.ClassDef)):
                names.add(stmt.name.value)
                continue
            if isinstance(stmt, cst.SimpleStatementLine):
                for small in stmt.body:
                    if isinstance(small, cst.Import):
                        for alias in small.names:
                            if alias.asname and isinstance(alias.asname.name, cst.Name):
                                names.add(alias.asname.name.value)
                            else:
                                names.add((dotted(alias.name) or "").split(".", 1)[0])
                    elif isinstance(small, cst.ImportFrom):
                        if isinstance(small.names, cst.ImportStar):
                            continue
                        for alias in small.names:
                            if alias.asname and isinstance(alias.asname.name, cst.Name):
                                names.add(alias.asname.name.value)
                            else:
                                names.add(dotted(alias.name) or "")
                    elif isinstance(small, cst.Assign):
                        for t in small.targets:
                            targets(t.target)
                    elif isinstance(small, (cst.AnnAssign, cst.AugAssign)):
                        targets(small.target)
                continue
            if isinstance(stmt, cst.With):
                for item in stmt.items:
                    if item.asname is not None:
                        targets(item.asname.name)
            if isinstance(stmt, cst.For):
                targets(stmt.target)
            for child in _child_blocks(stmt):
                visit(child.body)

    visit(statements)
    return names


def _child_blocks(stmt: cst.BaseStatement) -> Iterator[cst.IndentedBlock]:
    """Nested statement blocks of a compound statement (not functions or classes)."""
    if isinstance(stmt, (cst.With, cst.For, cst.While)):
        if isinstance(stmt.body, cst.IndentedBlock):
            yield stmt.body
        orelse = getattr(stmt, "orelse", None)
        if orelse is not None and isinstance(orelse.body, cst.IndentedBlock):
            yield orelse.body
    elif isinstance(stmt, cst.If):
        if isinstance(stmt.body, cst.IndentedBlock):
            yield stmt.body
        branch: cst.If | cst.Else | None = stmt.orelse
        while branch is not None:
            if isinstance(branch.body, cst.IndentedBlock):
                yield branch.body
            branch = branch.orelse if isinstance(branch, cst.If) else None
    elif isinstance(stmt, (cst.Try, cst.TryStar)):
        if isinstance(stmt.body, cst.IndentedBlock):
            yield stmt.body
        for handler in stmt.handlers:
            if isinstance(handler.body, cst.IndentedBlock):
                yield handler.body
        for extra in (stmt.orelse, stmt.finalbody):
            if extra is not None and isinstance(extra.body, cst.IndentedBlock):
                yield extra.body


class _Calls(cst.CSTVisitor):
    """Calls inside an expression statement, and whether they sit in a lambda or a
    comprehension (where one call creates any number of tasks)."""

    def __init__(self) -> None:
        self.calls: list[tuple[cst.Call, bool]] = []
        self._dynamic = 0

    def _enter(self, _node: cst.CSTNode) -> None:
        self._dynamic += 1

    def _leave(self, _node: cst.CSTNode) -> None:
        self._dynamic -= 1

    def visit_Lambda(self, node: cst.Lambda) -> None:
        self._enter(node)

    def leave_Lambda(self, node: cst.Lambda) -> None:
        self._leave(node)

    def visit_ListComp(self, node: cst.ListComp) -> None:
        self._enter(node)

    def leave_ListComp(self, node: cst.ListComp) -> None:
        self._leave(node)

    def visit_SetComp(self, node: cst.SetComp) -> None:
        self._enter(node)

    def leave_SetComp(self, node: cst.SetComp) -> None:
        self._leave(node)

    def visit_DictComp(self, node: cst.DictComp) -> None:
        self._enter(node)

    def leave_DictComp(self, node: cst.DictComp) -> None:
        self._leave(node)

    def visit_GeneratorExp(self, node: cst.GeneratorExp) -> None:
        self._enter(node)

    def leave_GeneratorExp(self, node: cst.GeneratorExp) -> None:
        self._leave(node)

    def visit_Call(self, node: cst.Call) -> None:
        self.calls.append((node, self._dynamic > 0))


class _Names(cst.CSTVisitor):
    """Names read by an expression (not attribute names, keywords or parameters)."""

    def __init__(self) -> None:
        self.names: list[cst.Name] = []
        self._skip: set[int] = set()

    def visit_Attribute(self, node: cst.Attribute) -> None:
        self._skip.add(id(node.attr))

    def visit_Arg(self, node: cst.Arg) -> None:
        if node.keyword is not None:
            self._skip.add(id(node.keyword))

    def visit_Param(self, node: cst.Param) -> None:
        self._skip.add(id(node.name))

    def visit_FunctionDef(self, node: cst.FunctionDef) -> None:
        self._skip.add(id(node.name))

    def visit_ClassDef(self, node: cst.ClassDef) -> None:
        self._skip.add(id(node.name))

    def visit_ImportAlias(self, node: cst.ImportAlias) -> bool:
        return False

    def visit_Name(self, node: cst.Name) -> None:
        if id(node) not in self._skip:
            self.names.append(node)


def read_names(node: cst.CSTNode) -> list[cst.Name]:
    visitor = _Names()
    node.visit(visitor)
    return visitor.names


BUILTIN_NAMES = frozenset(dir(builtins))


# --------------------------------------------------------------------------- the mapper


@dataclass
class _Scope:
    group: str | None = None
    limited: str | None = None


class _Mapper:
    def __init__(self, module: cst.Module) -> None:
        self.module = module
        self.imports = _collect_imports(module)
        self.tasks: dict[str, TaskSite] = {}
        self.groups: dict[str, GroupSite] = {}
        self.deps: list[DepStatement] = []
        self.unmapped: list[str] = []
        self.var_keys: dict[str, str] = {}
        self.taskflow: dict[str, tuple[cst.FunctionDef, cst.Decorator, Container, str | None]] = {}
        self.invocations: dict[str, list[TaskSite]] = {}
        self.implicit: set[tuple[str, str]] = set()
        self._pending_refs: list[tuple[str, cst.CSTNode]] = []
        self._duplicates: set[str] = set()

    # ------------------------------------------------------------- name classification
    def _is(self, expr: cst.BaseExpression, names: frozenset[str]) -> bool:
        return self.imports.resolve(expr) in names

    def _task_decorator(self, func: cst.FunctionDef) -> cst.Decorator | None:
        for deco in func.decorators:
            target = deco.decorator.func if isinstance(deco.decorator, cst.Call) else deco.decorator
            root = target
            while isinstance(root, cst.Attribute):
                root = root.value
            if isinstance(root, cst.Name) and self.imports.names.get(root.value, root.value) in (
                TASK_DECORATOR_NAMES
            ):
                return deco
            if (
                isinstance(target, cst.Attribute)
                and target.attr.value == "task"
                and not (isinstance(target.value, cst.Attribute))
            ):
                return deco  # ``airflow.decorators.task`` style, rarely ``sdk.task``
        return None

    def _group_decorator(self, func: cst.FunctionDef) -> cst.Decorator | None:
        for deco in func.decorators:
            target = deco.decorator.func if isinstance(deco.decorator, cst.Call) else deco.decorator
            if self._is(target, TASK_GROUP_DECORATOR_NAMES):
                return deco
        return None

    # ------------------------------------------------------------- DAG discovery
    def find_dags(self) -> list[DagSite]:
        found: list[DagSite] = []
        for stmt in self.module.body:
            if isinstance(stmt, cst.With) and isinstance(stmt.body, cst.IndentedBlock):
                for item in stmt.items:
                    call = item.item
                    if isinstance(call, cst.Call) and self._is(call.func, DAG_CLASS_NAMES):
                        dag_id, arg = self._dag_id(call)
                        var = (
                            item.asname.name.value
                            if item.asname and isinstance(item.asname.name, cst.Name)
                            else None
                        )
                        if dag_id is not None:
                            found.append(
                                DagSite(dag_id, "with", call, stmt, stmt.body, var, dag_id_arg=arg)
                            )
            elif isinstance(stmt, cst.FunctionDef) and isinstance(stmt.body, cst.IndentedBlock):
                for deco in stmt.decorators:
                    target = deco.decorator
                    dcall = target if isinstance(target, cst.Call) else None
                    if self._is(dcall.func if dcall else target, DAG_DECORATOR_NAMES):
                        dag_id, arg = self._dag_id(dcall) if dcall else (None, None)
                        found.append(
                            DagSite(
                                dag_id or stmt.name.value,
                                "decorator",
                                dcall,
                                stmt,
                                stmt.body,
                                decorator=deco,
                                func=stmt,
                                dag_id_arg=arg,
                            )
                        )
            elif isinstance(stmt, cst.SimpleStatementLine) and len(stmt.body) == 1:
                small = stmt.body[0]
                if (
                    isinstance(small, cst.Assign)
                    and len(small.targets) == 1
                    and isinstance(small.targets[0].target, cst.Name)
                    and isinstance(small.value, cst.Call)
                    and self._is(small.value.func, DAG_CLASS_NAMES)
                ):
                    dag_id, arg = self._dag_id(small.value)
                    if dag_id is not None:
                        found.append(
                            DagSite(
                                dag_id,
                                "assign",
                                small.value,
                                stmt,
                                self.module,
                                small.targets[0].target.value,
                                dag_id_arg=arg,
                            )
                        )
        return found

    @staticmethod
    def _dag_id(call: cst.Call) -> tuple[str | None, cst.Arg | None]:
        arg = keyword(call, "dag_id")
        if arg is None and call.args and call.args[0].keyword is None and not call.args[0].star:
            arg = call.args[0]
        return (literal_string(arg.value) if arg else None), arg

    # ------------------------------------------------------------- scanning
    def collect_taskflow(self, block: Sequence[cst.BaseStatement], container: Container) -> None:
        for stmt in block:
            if isinstance(stmt, cst.FunctionDef):
                deco = self._task_decorator(stmt)
                if deco is not None:
                    call = deco.decorator if isinstance(deco.decorator, cst.Call) else None
                    tid_arg = keyword(call, "task_id")
                    tid = literal_string(tid_arg.value) if tid_arg else stmt.name.value
                    self.taskflow[stmt.name.value] = (stmt, deco, container, tid)

    def scan(
        self, statements: Sequence[cst.BaseStatement], container: Container, scope: _Scope
    ) -> None:
        for stmt in statements:
            if isinstance(stmt, cst.SimpleStatementLine):
                self._simple(stmt, container, scope)
            elif isinstance(stmt, cst.With):
                self._with(stmt, container, scope)
            elif isinstance(stmt, cst.FunctionDef):
                self._function(stmt, container, scope)
            elif isinstance(stmt, (cst.For, cst.While)):
                inner = _Scope(scope.group, scope.limited or "created in a loop")
                for child in _child_blocks(stmt):
                    self.scan(child.body, child, inner)
            elif isinstance(stmt, (cst.If, cst.Try, cst.TryStar)):
                inner = _Scope(scope.group, scope.limited or "defined conditionally")
                for child in _child_blocks(stmt):
                    self.scan(child.body, child, inner)
            elif isinstance(stmt, cst.ClassDef):
                continue

    def _function(self, func: cst.FunctionDef, container: Container, scope: _Scope) -> None:
        if func.name.value in self.taskflow:
            return  # a TaskFlow function: the task is created where it is called
        if self._task_decorator(func) is not None:
            # defined in a nested block (a task group, an if): known from here on
            self.collect_taskflow([func], container)
            return
        deco = self._group_decorator(func)
        if deco is not None:
            call = deco.decorator if isinstance(deco.decorator, cst.Call) else None
            gid_arg = keyword(call, "group_id")
            gid = literal_string(gid_arg.value) if gid_arg else func.name.value
            note = f"inside the @task_group function {func.name.value!r}"
            if gid is not None and isinstance(func.body, cst.IndentedBlock):
                inner = _Scope(self._group_key(gid, scope.group, True), scope.limited or note)
                self.collect_taskflow(func.body.body, func.body)
                self.scan(func.body.body, func.body, inner)
            return
        if isinstance(func.body, cst.IndentedBlock):
            # A factory: whatever it creates depends on how it is called.
            inner = _Scope(scope.group, f"created by the function {func.name.value!r}")
            self.scan(func.body.body, func.body, inner)

    def _group_key(self, own: str, parent: str | None, parent_prefix: bool | None = None) -> str:
        if parent is None:
            return own
        site = self.groups.get(parent)
        prefix = site.prefix if site is not None else bool(parent_prefix)
        return f"{parent}.{own}" if prefix else own

    def _task_key(self, own: str, group: str | None) -> str:
        if group is None:
            return own
        site = self.groups.get(group)
        if site is None or site.prefix:
            return f"{group}.{own}"
        return own

    def _with(self, stmt: cst.With, container: Container, scope: _Scope) -> None:
        group: GroupSite | None = None
        for item in stmt.items:
            call = item.item
            if isinstance(call, cst.Call) and self._is(call.func, TASK_GROUP_CLASS_NAMES):
                var = (
                    item.asname.name.value
                    if item.asname and isinstance(item.asname.name, cst.Name)
                    else None
                )
                group = self._group(call, var, scope, container, with_stmt=stmt)
        inner = _Scope(group.key if group else scope.group, scope.limited)
        if isinstance(stmt.body, cst.IndentedBlock):
            self.scan(stmt.body.body, stmt.body, inner)

    def _group(
        self,
        call: cst.Call,
        var: str | None,
        scope: _Scope,
        container: Container,
        *,
        with_stmt: cst.With | None = None,
        statement: cst.SimpleStatementLine | None = None,
    ) -> GroupSite | None:
        gid_arg = keyword(call, "group_id")
        if gid_arg is None and call.args and call.args[0].keyword is None:
            gid_arg = call.args[0]
        own = literal_string(gid_arg.value) if gid_arg else None
        if own is None:
            self.unmapped.append("a task group without a literal group_id")
            return None
        parent = scope.group
        parent_arg = keyword(call, "parent_group")
        if parent_arg is not None:
            name = dotted(parent_arg.value)
            parent_key = self.var_keys.get(name or "")
            parent = parent_key[len(GROUP_PREFIX) :] if parent_key else parent
        prefix_arg = keyword(call, "prefix_group_id")
        prefix = literal_bool(prefix_arg.value) if prefix_arg is not None else True
        key = self._group_key(own, parent)
        site = GroupSite(
            key=key,
            own_id=own,
            call=call,
            prefix=prefix is not False,
            parent=parent,
            var=var,
            with_stmt=with_stmt,
            statement=statement,
            container=container,
            limited=scope.limited,
        )
        if key in self.groups:
            self._duplicates.add(GROUP_PREFIX + key)
        self.groups[key] = site
        if var:
            self.var_keys[var] = GROUP_PREFIX + key
        return site

    def _simple(self, stmt: cst.SimpleStatementLine, container: Container, scope: _Scope) -> None:
        single = len(stmt.body) == 1
        for small in stmt.body:
            var: str | None = None
            value: cst.BaseExpression | None = None
            if isinstance(small, cst.Assign):
                if len(small.targets) == 1 and isinstance(small.targets[0].target, cst.Name):
                    var = small.targets[0].target.value
                value = small.value
            elif isinstance(small, cst.AnnAssign):
                if isinstance(small.target, cst.Name):
                    var = small.target.value
                value = small.value
            elif isinstance(small, cst.Expr):
                value = small.value
            if value is None:
                continue
            if isinstance(value, cst.Call) and self._is(value.func, TASK_GROUP_CLASS_NAMES):
                self._group(value, var, scope, container, statement=stmt if single else None)
                continue
            top = self._top_task_call(value)
            visitor = _Calls()
            value.visit(visitor)
            for call, dynamic in visitor.calls:
                self._call(
                    call,
                    scope,
                    dynamic=dynamic,
                    var=var if call is top else None,
                    statement=stmt if (call is top and single) else None,
                    container=container,
                    owner=value,
                )
            if isinstance(small, cst.Expr):
                self._dependency(small.value, stmt if single else None, container)

    @staticmethod
    def _top_task_call(value: cst.BaseExpression) -> cst.Call | None:
        """The call creating the task an assignment / expression statement is about:
        ``Op(...)`` itself, or the inner ``Op.partial(...)`` / ``fn(...)`` of a mapped one."""
        if not isinstance(value, cst.Call):
            return None
        func = value.func
        if isinstance(func, cst.Attribute) and func.attr.value in ("expand", "expand_kwargs"):
            inner = func.value
            if isinstance(inner, cst.Call):
                return inner
        return value

    def _call(
        self,
        call: cst.Call,
        scope: _Scope,
        *,
        dynamic: bool,
        var: str | None,
        statement: cst.SimpleStatementLine | None,
        container: Container,
        owner: cst.BaseExpression,
    ) -> None:
        func = call.func
        limited = scope.limited or ("created in a comprehension or lambda" if dynamic else None)
        # TaskFlow call sites: fn(...), fn.override(...)(...), fn.partial(...)
        name = None
        override: cst.Call | None = None
        if isinstance(func, cst.Name):
            name = func.value
        elif (
            isinstance(func, cst.Call)
            and isinstance(func.func, cst.Attribute)
            and func.func.attr.value == "override"
            and isinstance(func.func.value, cst.Name)
        ):
            name, override = func.func.value.value, func
        elif (
            isinstance(func, cst.Attribute)
            and func.attr.value == "partial"
            and isinstance(func.value, cst.Name)
        ):
            name = func.value.value
        if name is not None and name in self.taskflow:
            self._invocation(name, call, override, scope, limited, var, statement, container, owner)
            return
        tid_arg = keyword(call, "task_id")
        if tid_arg is None:
            return
        if isinstance(func, cst.Attribute) and func.attr.value == "override":
            return  # fn.override(task_id=...): handled with its call
        own = literal_string(tid_arg.value)
        if own is None:
            self.unmapped.append(
                f"a task with a computed task_id ({self.module.code_for_node(tid_arg.value)})"
            )
            return
        group = scope.group
        tg_arg = keyword(call, "task_group")
        if tg_arg is not None:
            ref = self.var_keys.get(dotted(tg_arg.value) or "")
            if ref is None:
                limited = limited or "its task_group is not a known group variable"
            else:
                group = ref[len(GROUP_PREFIX) :]
        key = self._task_key(own, group)
        expand = _expand_of(owner, call)
        site = TaskSite(
            key=key,
            own_id=own,
            kind="operator",
            call=call,
            expand=expand,
            statement=statement,
            container=container,
            var=var,
            group=group,
            task_id_arg=tid_arg,
            limited=limited,
        )
        self._add(site)
        self._data_refs(key, call)
        if expand is not None:
            self._data_refs(key, expand)

    def _invocation(
        self,
        name: str,
        call: cst.Call,
        override: cst.Call | None,
        scope: _Scope,
        limited: str | None,
        var: str | None,
        statement: cst.SimpleStatementLine | None,
        container: Container,
        owner: cst.BaseExpression,
    ) -> None:
        func, deco, def_container, tid = self.taskflow[name]
        tid_arg = None
        deco_call = deco.decorator if isinstance(deco.decorator, cst.Call) else None
        if deco_call is not None:
            tid_arg = keyword(deco_call, "task_id")
        if override is not None:
            o_arg = keyword(override, "task_id")
            if o_arg is not None:
                tid_arg = o_arg
                tid = literal_string(o_arg.value)
            limited = limited or "it is created with .override(...)"
        if tid is None:
            self.unmapped.append(f"the TaskFlow function {name!r} has a computed task_id")
            return
        group = scope.group
        tg_arg = keyword(deco_call, "task_group")
        if tg_arg is not None:
            ref = self.var_keys.get(dotted(tg_arg.value) or "")
            if ref is not None:
                group = ref[len(GROUP_PREFIX) :]
        key = self._task_key(tid, group)
        expand = _expand_of(owner, call)
        site = TaskSite(
            key=key,
            own_id=tid,
            kind="taskflow",
            call=deco_call,
            expand=expand,
            func=func,
            decorator=deco,
            invocation=call,
            statement=statement,
            container=container,
            def_container=def_container,
            var=var,
            group=group,
            task_id_arg=tid_arg,
            limited=limited,
        )
        self.invocations.setdefault(name, []).append(site)
        self._add(site)
        self._data_refs(key, call)
        if expand is not None:
            self._data_refs(key, expand)

    def _add(self, site: TaskSite) -> None:
        if site.key in self.tasks:
            self._duplicates.add(site.key)
        self.tasks[site.key] = site
        if site.var:
            self.var_keys[site.var] = site.key

    def _data_refs(self, key: str, call: cst.Call) -> None:
        for arg in call.args:
            self._pending_refs.append((key, arg.value))

    def finish(self) -> None:
        for key in self._duplicates:
            if key.startswith(GROUP_PREFIX):
                group = self.groups.get(key[len(GROUP_PREFIX) :])
                if group is not None:
                    group.limited = group.limited or "its group_id is used more than once"
            elif key in self.tasks:
                self.tasks[key].limited = self.tasks[key].limited or "its task_id is not unique"
        for name, sites in self.invocations.items():
            if len(sites) > 1:
                for site in sites:
                    site.limited = site.limited or f"the function {name!r} is called more than once"
        for key, value in self._pending_refs:
            for ref in read_names(value):
                upstream = self.var_keys.get(ref.value)
                if upstream and not upstream.startswith(GROUP_PREFIX) and upstream != key:
                    self.implicit.add((upstream, key))

    # ------------------------------------------------------------- dependencies
    def _operand(self, expr: cst.BaseExpression) -> list[str] | None:
        if isinstance(expr, cst.Name):
            key = self.var_keys.get(expr.value)
            return [key] if key else None
        if isinstance(expr, cst.Attribute) and expr.attr.value == "output":
            return self._operand(expr.value)
        if isinstance(expr, (cst.List, cst.Tuple)):
            keys: list[str] = []
            for el in expr.elements:
                if isinstance(el, cst.StarredElement):
                    return None
                sub = self._operand(el.value)
                if sub is None:
                    return None
                keys += sub
            return keys
        return None

    def _is_label(self, expr: cst.BaseExpression) -> str | None:
        if isinstance(expr, cst.Call) and self._is(expr.func, LABEL_NAMES) and expr.args:
            return literal_string(expr.args[0].value) or ""
        return None

    def _dependency(
        self,
        expr: cst.BaseExpression,
        statement: cst.SimpleStatementLine | None,
        container: Container,
    ) -> None:
        if isinstance(expr, cst.BinaryOperation) and isinstance(
            expr.operator, (cst.RightShift, cst.LeftShift)
        ):
            self._binop(expr, statement, container)
        elif isinstance(expr, cst.Call):
            if self._is(expr.func, CHAIN_NAMES) and statement is not None:
                self._chain(expr, statement, container)
            elif (
                isinstance(expr.func, cst.Attribute)
                and expr.func.attr.value in ("set_downstream", "set_upstream")
                and statement is not None
                and expr.args
            ):
                left = self._operand(expr.func.value)
                right = self._operand(expr.args[0].value)
                if left is None or right is None:
                    self.unmapped.append("a set_upstream/set_downstream call AutoAir cannot read")
                    return
                down = expr.func.attr.value == "set_downstream"
                edges = {(a, b) if down else (b, a) for a in left for b in right}
                self.deps.append(DepStatement(statement, container, "method", edges))

    def _binop(
        self,
        expr: cst.BinaryOperation,
        statement: cst.SimpleStatementLine | None,
        container: Container,
    ) -> None:
        flat: list[cst.BaseExpression] = []
        ops: list[cst.BaseBinaryOp] = []
        node: cst.BaseExpression = expr
        while isinstance(node, cst.BinaryOperation) and isinstance(
            node.operator, (cst.RightShift, cst.LeftShift)
        ):
            flat.insert(0, node.right)
            ops.insert(0, node.operator)
            node = node.left
        flat.insert(0, node)
        # Labels are transparent: a >> Label("x") >> b is the edge a -> b labelled x.
        real: list[tuple[cst.BaseExpression, list[str] | None]] = []
        real_ops: list[cst.BaseBinaryOp] = []
        labels_between: list[str | None] = []
        pending_label: str | None = None
        has_labels = False
        for i, operand in enumerate(flat):
            label = self._is_label(operand)
            if label is not None:
                pending_label = label
                has_labels = True
                continue
            if real:
                real_ops.append(ops[i - 1])
                labels_between.append(pending_label)
                pending_label = None
            real.append((operand, self._operand(operand)))
        edges: set[tuple[str, str]] = set()
        labels: dict[tuple[str, str], str] = {}
        resolvable = all(keys is not None for _, keys in real)
        for i, op in enumerate(real_ops):
            left, right = real[i][1], real[i + 1][1]
            if left is None or right is None:
                continue
            down = isinstance(op, cst.RightShift)
            for a in left:
                for b in right:
                    edge = (a, b) if down else (b, a)
                    edges.add(edge)
                    if labels_between[i]:
                        labels[edge] = labels_between[i] or ""
        if not resolvable:
            self.unmapped.append(
                "a dependency statement with operands AutoAir cannot read: "
                + self.module.code_for_node(expr).strip()[:120]
            )
        if statement is None:
            return
        self.deps.append(
            DepStatement(
                statement,
                container,
                "binop",
                edges,
                labels,
                [(o, k or []) for o, k in real] if resolvable else None,
                real_ops,
                has_labels,
            )
        )

    def _chain(
        self, expr: cst.Call, statement: cst.SimpleStatementLine, container: Container
    ) -> None:
        groups: list[list[str]] = []
        for arg in expr.args:
            keys = self._operand(arg.value) if not arg.star else None
            if keys is None:
                self.unmapped.append("a chain(...) call AutoAir cannot read")
                return
            groups.append(keys)
        edges: set[tuple[str, str]] = set()
        for left, right in itertools.pairwise(groups):
            if len(left) > 1 and len(right) > 1 and self.imports.resolve(expr.func) == "chain":
                if len(left) != len(right):
                    return
                edges |= set(zip(left, right, strict=True))
            else:
                edges |= {(a, b) for a in left for b in right}
        self.deps.append(DepStatement(statement, container, "chain", edges))


def _expand_of(owner: cst.BaseExpression, call: cst.Call) -> cst.Call | None:
    """``owner`` when it is ``call.expand(...)`` / ``call.expand_kwargs(...)``."""
    if (
        isinstance(owner, cst.Call)
        and isinstance(owner.func, cst.Attribute)
        and owner.func.attr.value in ("expand", "expand_kwargs")
        and owner.func.value is call
    ):
        return owner
    return None


def parse(source: str) -> cst.Module:
    try:
        return cst.parse_module(source)
    except cst.ParserSyntaxError as exc:
        raise MapError(f"the file cannot be parsed: {exc.message} (line {exc.raw_line})") from exc


def map_source(source: str | cst.Module, dag_id: str | None = None) -> SourceMap:
    """Map the DAG ``dag_id`` (the only DAG of the file when ``None``) of ``source``."""
    module = parse(source) if isinstance(source, str) else source
    mapper = _Mapper(module)
    dags = mapper.find_dags()
    if not dags:
        raise MapError("no DAG definition with a literal dag_id was found in the file")
    if dag_id is None:
        if len(dags) > 1:
            raise MapError("the file defines several DAGs; name the one to map")
        dag = dags[0]
    else:
        matching = [d for d in dags if d.dag_id == dag_id]
        if not matching:
            found = ", ".join(sorted(d.dag_id for d in dags))
            raise MapError(f"the file does not define the DAG {dag_id!r} (found: {found})")
        if len(matching) > 1:
            raise MapError(f"the DAG {dag_id!r} is defined more than once")
        dag = matching[0]
    mapper.collect_taskflow(module.body, module)
    body = dag.body
    if dag.style == "assign":
        others = [s for s in module.body if s is not dag.statement]
        mapper.scan(others, module, _Scope())
    else:
        mapper.collect_taskflow(body.body, body)
        mapper.scan(body.body, body, _Scope())
    mapper.finish()
    bound = bound_names(module.body)
    if dag.style != "assign":
        bound |= bound_names(body.body)
    if dag.func is not None:
        bound |= {p.name.value for p in dag.func.params.params}
    return SourceMap(
        module=module,
        dag=dag,
        tasks=mapper.tasks,
        groups=mapper.groups,
        deps=mapper.deps,
        unmapped=mapper.unmapped,
        var_keys=mapper.var_keys,
        bound_names=bound,
        implicit_edges=mapper.implicit,
        dag_ids=[d.dag_id for d in dags],
    )


def dag_ids(source: str) -> list[str]:
    """The literal dag_ids the file defines (empty when it cannot be parsed)."""
    try:
        module = cst.parse_module(source)
    except cst.ParserSyntaxError:
        return []
    return [d.dag_id for d in _Mapper(module).find_dags()]


__all__ = [
    "GROUP_PREFIX",
    "DagSite",
    "DepStatement",
    "GroupSite",
    "MapError",
    "SourceMap",
    "TaskSite",
    "dag_ids",
    "keyword",
    "literal_string",
    "map_source",
    "parse",
    "read_names",
]
