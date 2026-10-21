"""Apply a spec change to a hand-written DAG file with a minimal LibCST patch.

The change is computed in *generated-code space*: ``old`` is the code AutoAir generates for
the spec the file currently represents (the file imported), ``new`` the code it generates
for the intended spec. Both come from the same generator, so whatever differs between them
is exactly the change, expressed per task id: keyword arguments of the DAG, of task groups
and of tasks, TaskFlow function bodies and signatures, added and removed tasks, groups,
dependency edges and other DAG statements. Each difference is then located in the file
(``base``) through the static map (:mod:`autoair.preserve.mapper`) and patched there --
only that keyword, statement or block changes; comments, formatting and ordering elsewhere
stay as they are.

A difference that cannot be located, or only by rewriting more than the change itself, is
*refused* with a message; ``strict`` also refuses the *risky* edits that preserve mode
makes with a warning (replacing a value that refers to a variable, changing an operator
class, moving a task to another group through ``task_group=``). The caller verifies the
result with a round trip (:mod:`autoair.preserve.service`); this module never executes code.
"""

from __future__ import annotations

import ast
import difflib
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import libcst as cst

from autoair.preserve.mapper import (
    BUILTIN_NAMES,
    GROUP_PREFIX,
    Container,
    DepStatement,
    GroupSite,
    MapError,
    SourceMap,
    TaskSite,
    bound_names,
    dotted,
    keyword,
    literal_string,
    map_source,
    read_names,
)
from autoair.spec.models import KEY_RE

IssueCode = Literal[
    "unmappable",
    "unsupported",
    "risky",
    "conflict",
    "roundtrip",
    "syntax",
    "import",
    "policy",
]


@dataclass
class PatchIssue:
    code: IssueCode
    message: str
    task_id: str | None = None


@dataclass
class PatchResult:
    source: str | None
    changed: bool = False
    operations: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    refusals: list[PatchIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.source is not None and not self.refusals


class _Refused(Exception):
    pass


# --------------------------------------------------------------------------- normalisation


def _norm(m: SourceMap, node: cst.CSTNode | None, rename: Callable[[str], str] = str) -> str:
    """A comparable form of ``node``: its AST dump with task/group variables replaced by the
    task id they stand for (generated variable names depend on the order of the spec)."""
    if node is None:
        return ""
    code = m.module.code_for_node(node)
    try:
        if isinstance(node, cst.BaseExpression):
            tree: ast.AST = ast.parse("(\n" + code + "\n)", mode="eval")
        else:
            tree = ast.parse(_dedent(code))
    except SyntaxError:
        return code

    class _Keys(ast.NodeTransformer):
        def visit_Name(self, n: ast.Name) -> ast.AST:
            key = m.var_keys.get(n.id)
            if key is not None:
                return ast.Constant(value="\0" + rename(key))
            return n

    return ast.dump(_Keys().visit(tree), include_attributes=False)


def _dedent(code: str) -> str:
    import textwrap

    return textwrap.dedent(code)


def _body_dump(m: SourceMap, func: cst.FunctionDef) -> str:
    stmts = func.body.body if isinstance(func.body, cst.IndentedBlock) else []
    return "|".join(_norm(m, s) for s in stmts)


def _params_dump(m: SourceMap, func: cst.FunctionDef) -> str:
    code = m.module.code_for_node(func.params)
    try:
        tree = ast.parse(f"def _f({code}): pass")
    except SyntaxError:
        return code
    fn = tree.body[0]
    assert isinstance(fn, ast.FunctionDef)
    return ast.dump(fn.args, include_attributes=False)


def _kwargs(call: cst.Call | None, skip: Iterable[str] = ()) -> dict[str, cst.Arg]:
    out: dict[str, cst.Arg] = {}
    if call is None:
        return out
    skipped = set(skip)
    for arg in call.args:
        if arg.keyword is not None and arg.keyword.value not in skipped:
            out[arg.keyword.value] = arg
    return out


# --------------------------------------------------------------------------- edits


@dataclass
class _CallEdit:
    put: dict[str, cst.BaseExpression] = field(default_factory=dict)
    drop: set[str] = field(default_factory=set)
    positional: dict[int, cst.BaseExpression] = field(default_factory=dict)
    func: cst.BaseExpression | None = None


@dataclass
class _FuncEdit:
    body: cst.BaseSuite | None = None
    params: cst.Parameters | None = None


@dataclass
class _Insert:
    after: cst.CSTNode | None  # insert after this statement (None: at the end)
    statements: list[cst.BaseStatement]
    before_return: bool = False
    first: bool = False  # at the start of the block (after a docstring)


class _Apply(cst.CSTTransformer):
    def __init__(self, plan: _Plan) -> None:
        self.plan = plan

    def leave_Call(self, original: cst.Call, updated: cst.Call) -> cst.BaseExpression:
        edit = self.plan.calls.get(id(original))
        if edit is None:
            return updated
        return _edit_call(updated, edit)

    def leave_Decorator(self, original: cst.Decorator, updated: cst.Decorator) -> cst.Decorator:
        edit = self.plan.decorators.get(id(original))
        if edit is None:
            return updated
        call = cst.Call(func=updated.decorator, args=[])
        return updated.with_changes(decorator=_edit_call(call, edit))

    def leave_SimpleString(
        self, original: cst.SimpleString, updated: cst.SimpleString
    ) -> cst.BaseExpression:
        new = self.plan.strings.get(id(original))
        return new if new is not None else updated

    def leave_FunctionDef(
        self, original: cst.FunctionDef, updated: cst.FunctionDef
    ) -> cst.FunctionDef:
        edit = self.plan.functions.get(id(original))
        if edit is None:
            return updated
        changes: dict[str, Any] = {}
        if edit.body is not None:
            changes["body"] = edit.body
        if edit.params is not None:
            changes["params"] = edit.params
        return updated.with_changes(**changes)

    def _block(self, original: Container, updated: Container) -> Container:
        removals = self.plan.removals
        replace = self.plan.replacements
        inserts = self.plan.inserts.get(id(original), [])
        if not (inserts or any(id(s) in removals or id(s) in replace for s in original.body)):
            return updated
        body: list[cst.BaseStatement] = []
        for orig, upd in zip(original.body, updated.body, strict=True):
            if id(orig) in replace:
                body.extend(replace[id(orig)])
            elif id(orig) not in removals:
                body.append(upd)
            for ins in inserts:
                if ins.after is orig:
                    body.extend(ins.statements)
        firsts = [s for ins in inserts if ins.first for s in ins.statements]
        if firsts:
            at = 1 if body and _is_docstring(body[0]) else 0
            body[at:at] = firsts
        for ins in inserts:
            if ins.after is None and not ins.first:
                if ins.before_return and body and _is_return(body[-1]):
                    body[-1:-1] = ins.statements
                else:
                    body.extend(ins.statements)
        if isinstance(updated, cst.IndentedBlock) and not body:
            body = [cst.SimpleStatementLine(body=[cst.Pass()])]
        return updated.with_changes(body=body)

    def leave_IndentedBlock(
        self, original: cst.IndentedBlock, updated: cst.IndentedBlock
    ) -> cst.IndentedBlock:
        block = self._block(original, updated)
        assert isinstance(block, cst.IndentedBlock)
        return block

    def leave_Module(self, original: cst.Module, updated: cst.Module) -> cst.Module:
        block = self._block(original, updated)
        assert isinstance(block, cst.Module)
        return block


def _is_docstring(stmt: cst.BaseStatement) -> bool:
    return (
        isinstance(stmt, cst.SimpleStatementLine)
        and len(stmt.body) == 1
        and isinstance(stmt.body[0], cst.Expr)
        and isinstance(stmt.body[0].value, (cst.SimpleString, cst.ConcatenatedString))
    )


def _is_return(stmt: cst.BaseStatement) -> bool:
    return isinstance(stmt, cst.SimpleStatementLine) and any(
        isinstance(s, cst.Return) for s in stmt.body
    )


def _edit_call(call: cst.Call, edit: _CallEdit) -> cst.Call:
    args = list(call.args)
    if edit.func is not None:
        call = call.with_changes(func=edit.func)
    out: list[cst.Arg] = []
    positional = 0
    for arg in args:
        if arg.keyword is None and not arg.star:
            if positional in edit.positional:
                arg = arg.with_changes(value=edit.positional[positional])
            positional += 1
        if arg.keyword is not None:
            name = arg.keyword.value
            if name in edit.drop:
                continue
            if name in edit.put:
                arg = arg.with_changes(value=edit.put[name])
        out.append(arg)
    last = args[-1] if args else None
    if out and last is not None and last.keyword is not None and last.keyword.value in edit.drop:
        # The new last argument takes over the closing layout of the removed one (trailing
        # comma, the line before ")"), keeping its own end-of-line comment.
        out[-1] = _take_tail(out[-1], last)
    present = {a.keyword.value for a in out if a.keyword is not None}
    new_names = [n for n in edit.put if n not in present and n not in edit.drop]
    for name in new_names:
        out = _append_arg(call, out, name, edit.put[name])
    return call.with_changes(args=out)


def _comment_line(node: cst.BaseParenthesizableWhitespace | None) -> cst.TrailingWhitespace | None:
    return node.first_line if isinstance(node, cst.ParenthesizedWhitespace) else None


def _ws_after(comma: cst.Comma | cst.MaybeSentinel) -> cst.BaseParenthesizableWhitespace | None:
    return comma.whitespace_after if isinstance(comma, cst.Comma) else None


def _take_tail(keep: cst.Arg, removed: cst.Arg) -> cst.Arg:
    own = _comment_line(_ws_after(keep.comma))
    comma = removed.comma
    after_arg = removed.whitespace_after_arg
    if own is not None:
        ws = _ws_after(comma)
        if isinstance(ws, cst.ParenthesizedWhitespace) and isinstance(comma, cst.Comma):
            comma = comma.with_changes(whitespace_after=ws.with_changes(first_line=own))
        elif isinstance(after_arg, cst.ParenthesizedWhitespace):
            after_arg = after_arg.with_changes(first_line=own)
    return keep.with_changes(comma=comma, whitespace_after_arg=after_arg)


def _append_arg(
    call: cst.Call, args: list[cst.Arg], name: str, value: cst.BaseExpression
) -> list[cst.Arg]:
    new = cst.Arg(
        keyword=cst.Name(name),
        value=value,
        equal=cst.AssignEqual(
            whitespace_before=cst.SimpleWhitespace(""), whitespace_after=cst.SimpleWhitespace("")
        ),
    )
    if not args:
        return [new]
    last = args[-1]
    if len(args) >= 2 and isinstance(args[-2].comma, cst.Comma):
        sep = args[-2].comma
    elif isinstance(call.whitespace_before_args, cst.ParenthesizedWhitespace):
        sep = cst.Comma(whitespace_after=call.whitespace_before_args)
    else:
        sep = cst.Comma(whitespace_after=cst.SimpleWhitespace(" "))
    # The last argument keeps its end-of-line comment; the new one takes the closing layout.
    own = _comment_line(_ws_after(last.comma)) or _comment_line(last.whitespace_after_arg)
    sep_ws = sep.whitespace_after
    if own is not None and isinstance(sep_ws, cst.ParenthesizedWhitespace):
        sep = sep.with_changes(whitespace_after=sep_ws.with_changes(first_line=own))
    blank = cst.TrailingWhitespace()
    trailing = last.comma
    tail_ws = _ws_after(trailing)
    if isinstance(trailing, cst.Comma) and isinstance(tail_ws, cst.ParenthesizedWhitespace):
        trailing = trailing.with_changes(whitespace_after=tail_ws.with_changes(first_line=blank))
    after_arg = last.whitespace_after_arg
    if isinstance(after_arg, cst.ParenthesizedWhitespace):
        after_arg = after_arg.with_changes(first_line=blank)
    kept = last.with_changes(comma=sep, whitespace_after_arg=cst.SimpleWhitespace(""))
    return [*args[:-1], kept, new.with_changes(comma=trailing, whitespace_after_arg=after_arg)]


@dataclass
class _Plan:
    calls: dict[int, _CallEdit] = field(default_factory=dict)
    decorators: dict[int, _CallEdit] = field(default_factory=dict)
    strings: dict[int, cst.BaseExpression] = field(default_factory=dict)
    functions: dict[int, _FuncEdit] = field(default_factory=dict)
    removals: set[int] = field(default_factory=set)
    replacements: dict[int, list[cst.BaseStatement]] = field(default_factory=dict)
    inserts: dict[int, list[_Insert]] = field(default_factory=dict)

    def call(self, call: cst.Call) -> _CallEdit:
        return self.calls.setdefault(id(call), _CallEdit())

    def insert(self, container: Container, ins: _Insert) -> None:
        self.inserts.setdefault(id(container), []).append(ins)


class _Rename(cst.CSTTransformer):
    """Rename variables (Load names only) of generated code to the file's variables."""

    def __init__(self, names: dict[str, str]) -> None:
        self.names = names
        self.skip: set[int] = set()

    def visit_Attribute(self, node: cst.Attribute) -> None:
        self.skip.add(id(node.attr))

    def visit_Arg(self, node: cst.Arg) -> None:
        if node.keyword is not None:
            self.skip.add(id(node.keyword))

    def visit_Param(self, node: cst.Param) -> None:
        self.skip.add(id(node.name))

    def leave_Name(self, original: cst.Name, updated: cst.Name) -> cst.Name:
        if id(original) in self.skip:
            return updated
        new = self.names.get(original.value)
        return updated.with_changes(value=new) if new else updated


# --------------------------------------------------------------------------- the patcher


class _Patcher:
    def __init__(
        self,
        base: SourceMap,
        old: SourceMap,
        new: SourceMap,
        *,
        strict: bool,
        renames: dict[str, str],
    ) -> None:
        self.b, self.o, self.n = base, old, new
        self.strict = strict
        self.renames = renames
        self.reverse = {v: k for k, v in renames.items()}
        self.plan = _Plan()
        self.ops: list[str] = []
        self.warnings: list[str] = []
        self.refusals: list[PatchIssue] = []
        # new-space key -> the file's variable (existing tasks and inserted ones)
        self.vars: dict[str, str] = {}
        for key, site in base.tasks.items():
            if site.var:
                self.vars[renames.get(key, key)] = site.var
        for key, group in base.groups.items():
            if group.var:
                self.vars[GROUP_PREFIX + key] = group.var
        self.taken = set(base.bound_names)
        self.inserted: list[cst.CSTNode] = []
        self.removed_vars: dict[str, str] = {}  # var -> task key
        self.removed_functions: set[str] = set()
        self.imported = _import_names(base.module)
        self.data_targets: set[str] = set()  # new-space keys whose arguments are patched
        named = [t for t in base.tasks.values() if t.var]
        # The file names its variables after the task ids (``extract = ...``): new ones too.
        self.plain_names = bool(named) and 2 * sum(
            t.var == _identifier(t.own_id) for t in named
        ) >= len(named)

    # ------------------------------------------------------------- issue helpers
    def refuse(self, code: IssueCode, message: str, task_id: str | None = None) -> None:
        self.refusals.append(PatchIssue(code, message, task_id))

    def risky(self, message: str, task_id: str | None = None) -> bool:
        """A risky edit: refused in strict mode, a warning otherwise. True: go ahead."""
        if self.strict:
            self.refuse("risky", message + " (refused by strict-preserve)", task_id)
            return False
        self.warnings.append(message)
        return True

    # ------------------------------------------------------------- expressions
    def gen_vars(self) -> dict[str, str]:
        """Generated variable -> the file's variable, for every key known in the file."""
        out: dict[str, str] = {}
        for var, key in self.n.var_keys.items():
            if key in self.vars:
                out[var] = self.vars[key]
        return out

    def adopt(self, node: cst.CSTNode, what: str, task_id: str | None = None) -> Any:
        """``node`` of the new generated code, ready for the file: variables renamed; None
        (refused) when it refers to a task the file has no variable for."""
        for name in read_names(node):
            key = self.n.var_keys.get(name.value)
            if key is not None and key not in self.vars:
                self.refuse(
                    "unsupported",
                    f"{what} refers to {key.lstrip(GROUP_PREFIX)!r}, which has no variable in "
                    "the file",
                    task_id,
                )
                return None
        adopted = node.visit(_Rename(self.gen_vars()))
        assert isinstance(adopted, cst.CSTNode)
        self.inserted.append(adopted)
        return adopted

    def uses_variables(self, node: cst.CSTNode) -> bool:
        """The file's value refers to a variable of the file (not an import or builtin)."""
        for name in read_names(node):
            v = name.value
            if v in BUILTIN_NAMES or v in self.imported:
                continue
            if v in self.b.bound_names:
                return True
        return False

    # ------------------------------------------------------------- keyword arguments
    def kwargs(
        self,
        what: str,
        base_call: cst.Call | None,
        old_call: cst.Call | None,
        new_call: cst.Call | None,
        *,
        skip: Iterable[str] = (),
        task_id: str | None = None,
        decorator: cst.Decorator | None = None,
        params: Sequence[str] = (),
        rename_old: Callable[[str], str] = str,
    ) -> bool:
        """Patch the keyword arguments that differ between ``old_call`` and ``new_call``
        into ``base_call``. Returns whether anything changed."""
        skip = set(skip)
        old_kw, new_kw = _kwargs(old_call, skip), _kwargs(new_call, skip)
        changed = False
        for name in sorted(old_kw.keys() | new_kw.keys()):
            o = _norm(self.o, old_kw[name].value, rename_old) if name in old_kw else None
            n = _norm(self.n, new_kw[name].value) if name in new_kw else None
            if o == n:
                continue
            changed = True
            base_arg = keyword(base_call, name)
            pos_index = None
            if base_arg is None and name in params and base_call is not None:
                index = list(params).index(name)
                plain = [a for a in base_call.args if a.keyword is None and not a.star]
                if index < len(plain):
                    base_arg, pos_index = plain[index], index
            if n is None:
                if base_arg is None or pos_index is not None:
                    self.refuse(
                        "unsupported",
                        f"{what}: the argument {name!r} cannot be removed: it is not written "
                        "as a keyword argument there (it may come from default_args or a "
                        "variable)",
                        task_id,
                    )
                    continue
                assert base_call is not None
                self.plan.call(base_call).drop.add(name)
                self.ops.append(f"{what}: removed {name}")
                continue
            value = self.adopt(new_kw[name].value, f"{what}: {name}", task_id)
            if value is None:
                continue
            if name in new_kw:
                self.data_targets.add(task_id or "")
            if base_arg is not None:
                if self.uses_variables(base_arg.value) and not self.risky(
                    f"{what}: {name} was "
                    f"{self.b.module.code_for_node(base_arg.value).strip()[:60]!r}, which refers "
                    "to a variable; it is replaced by the literal value",
                    task_id,
                ):
                    continue
                assert base_call is not None
                if pos_index is not None:
                    self.plan.call(base_call).positional[pos_index] = value
                else:
                    self.plan.call(base_call).put[name] = value
                self.ops.append(f"{what}: changed {name}")
            elif base_call is not None:
                self.plan.call(base_call).put[name] = value
                self.ops.append(f"{what}: added {name}")
            elif decorator is not None:
                edit = self.plan.decorators.setdefault(id(decorator), _CallEdit())
                edit.put[name] = value
                self.ops.append(f"{what}: added {name}")
            else:
                self.refuse("unsupported", f"{what}: cannot add {name!r}", task_id)
        return changed

    # ------------------------------------------------------------- the DAG
    def dag(self) -> None:
        b, o, n = self.b.dag, self.o.dag, self.n.dag
        if o.dag_id != n.dag_id:
            self.refuse(
                "unsupported",
                "renaming the DAG (dag_id) is not applied to an existing file; switch the "
                "source policy to 'generate' for a renamed DAG",
            )
            return
        self.kwargs("DAG", b.call, o.call, n.call, skip={"dag_id"}, decorator=b.decorator)

    # ------------------------------------------------------------- groups
    def groups(self) -> None:
        for key in self.o.groups.keys() - self.n.groups.keys():
            self.remove_group(key)
        for key, ng in self.n.groups.items():
            og = self.o.groups.get(key)
            if og is None:
                continue
            bg = self.b.groups.get(key)
            if og.parent != ng.parent:
                self.refuse("unsupported", f"moving the task group {key!r} is not supported")
                continue
            if bg is None:
                if _norm(self.o, og.call) != _norm(self.n, ng.call):
                    self.refuse(
                        "unmappable",
                        f"the task group {key!r} changed, but its definition cannot be "
                        "located in the file",
                    )
                continue
            self.kwargs(
                f"task group {key}",
                bg.call,
                og.call,
                ng.call,
                skip={"group_id", "parent_group"},
            )

    def add_group(self, key: str) -> GroupSite | None:
        ng = self.n.groups[key]
        if ng.statement is None:
            self.refuse("unsupported", f"the new task group {key!r} cannot be inserted")
            return None
        if ng.parent is not None and GROUP_PREFIX + ng.parent not in self.vars:
            self.refuse(
                "unsupported",
                f"the new task group {key!r} belongs to {ng.parent!r}, which has no "
                "variable in the file",
            )
            return None
        var = self.fresh(self.var_name(ng.own_id, ng.var, "g"))
        self.vars[GROUP_PREFIX + key] = var
        stmt = self.adopt(ng.statement, f"task group {key}")
        if stmt is None:
            return None
        stmt = _rename_target(stmt, var)
        self.insert_dag_statements([stmt], group=None)
        self.ops.append(f"added task group {key}")
        return ng

    def remove_group(self, key: str) -> None:
        bg = self.b.groups.get(key)
        if bg is None or bg.limited:
            self.refuse(
                "unmappable",
                f"the task group {key!r} was removed, but its definition cannot be located "
                "in the file" + (f" ({bg.limited})" if bg and bg.limited else ""),
            )
            return
        members = [t for t in self.b.tasks.values() if t.group == key]
        # old (file) key space, like the members' keys
        removed = {k for k in self.o.tasks if self.renames.get(k, k) not in self.n.tasks}
        if any(t.key not in removed for t in members):
            self.refuse(
                "unsupported",
                f"the task group {key!r} was removed but some of its tasks stay; moving tasks "
                "out of a group is not supported",
            )
            return
        if bg.with_stmt is not None:
            body = bg.with_stmt.body
            assert isinstance(body, cst.IndentedBlock)
            member_statements = {id(t.statement) for t in members if t.statement is not None}
            member_funcs = {id(t.func) for t in members if t.func is not None}
            others = [
                s for s in body.body if id(s) not in member_statements and id(s) not in member_funcs
            ]
            if others:
                self.refuse(
                    "unsupported",
                    f"the task group {key!r} was removed, but its 'with' block holds other code",
                )
                return
            self.plan.removals.add(id(bg.with_stmt))
        elif bg.statement is not None:
            self.plan.removals.add(id(bg.statement))
        else:
            self.refuse("unsupported", f"the task group {key!r} is not defined on its own line")
            return
        if bg.var:
            self.removed_vars[bg.var] = GROUP_PREFIX + key
        self.ops.append(f"removed task group {key}")

    # ------------------------------------------------------------- tasks
    def mapped_new(self) -> set[str]:
        return {self.renames.get(k, k) for k in self.o.tasks} & set(self.n.tasks)

    def tasks(self) -> None:
        paired = {}
        for ok in self.o.tasks:
            nk = self.renames.get(ok, ok)
            if nk in self.n.tasks:
                paired[ok] = nk
        for ok in self.o.tasks:
            if ok not in paired:
                self.remove_task(ok)
        for ok, nk in paired.items():
            self.change_task(ok, nk)
        added_groups: set[str] = set()
        for nk in self.n.tasks:
            if nk in paired.values():
                continue
            group = self.n.tasks[nk].group
            chain: list[str] = []
            while group is not None and group not in self.o.groups and group not in added_groups:
                chain.insert(0, group)
                group = self.n.groups[group].parent if group in self.n.groups else None
            for g in chain:
                if self.add_group(g) is None:
                    return
                added_groups.add(g)
            self.add_task(nk)
        for key in self.n.groups.keys() - self.o.groups.keys() - added_groups:
            self.add_group(key)

    def change_task(self, ok: str, nk: str) -> None:
        ot, nt = self.o.tasks[ok], self.n.tasks[nk]
        bt = self.b.tasks.get(ok)
        rename_old = self.rename_key
        label = f"task {nk}"
        if ot.kind != nt.kind:
            self.refuse(
                "unsupported",
                f"{label}: changing a task between an operator and a TaskFlow function is not "
                "supported",
                nk,
            )
            return
        differs = self.task_differs(ot, nt)
        if not differs and ok == nk:
            return
        if bt is None:
            self.refuse(
                "unmappable",
                f"{label} changed, but its definition cannot be located in the file (a "
                "computed task_id, a loop, a factory function...)",
                nk,
            )
            return
        if bt.limited and (ot.group != nt.group or (ot.expand is None) != (nt.expand is None)):
            self.refuse("unmappable", f"{label}: {bt.limited}", nk)
            return
        if ok != nk:
            self.rename_task(bt, ot, nt)
        if ot.group != nt.group:
            self.move_task(bt, nt)
        if ot.kind == "operator":
            changed = _norm(self.o, ot.call.func if ot.call else None) != _norm(
                self.n, nt.call.func if nt.call else None
            )
            if changed and nt.call is not None and bt.call is not None:
                func = self.adopt(nt.call.func, label, nk)
                if func is not None and self.risky(f"{label}: the operator class changes", nk):
                    self.plan.call(bt.call).func = func
                    self.ops.append(f"{label}: changed the operator class")
            self.kwargs(
                label,
                bt.call,
                ot.call,
                nt.call,
                skip={"task_id", "task_group"},
                task_id=nk,
                rename_old=rename_old,
            )
        else:
            self.change_taskflow(bt, ot, nt, label, nk)
        if (ot.expand is None) != (nt.expand is None):
            self.refuse(
                "unsupported",
                f"{label}: adding or removing dynamic task mapping is not supported",
                nk,
            )
        elif ot.expand is not None and nt.expand is not None:
            if bt.expand is None:
                self.refuse("unmappable", f"{label}: the .expand(...) call cannot be located", nk)
            else:
                if ot.expand.func.attr.value != nt.expand.func.attr.value:  # type: ignore[attr-defined]
                    self.refuse("unsupported", f"{label}: changing expand/expand_kwargs", nk)
                self.kwargs(
                    f"{label} (mapping)",
                    bt.expand,
                    ot.expand,
                    nt.expand,
                    task_id=nk,
                    rename_old=rename_old,
                )

    def rename_key(self, key: str) -> str:
        return self.renames.get(key, key)

    def task_differs(self, ot: TaskSite, nt: TaskSite) -> bool:
        def calls(m: SourceMap, t: TaskSite, rename: Callable[[str], str] = str) -> list[str]:
            parts = [_norm(m, t.call, rename), _norm(m, t.expand, rename)]
            if t.kind == "taskflow":
                parts.append(_norm(m, t.invocation, rename))
                if t.func is not None:
                    parts += [_body_dump(m, t.func), _params_dump(m, t.func)]
                if t.decorator is not None:
                    parts.append(_norm(m, t.decorator.decorator, rename))
            return parts

        return calls(self.o, ot, self.rename_key) != calls(self.n, nt) or ot.group != nt.group

    def rename_task(self, bt: TaskSite, ot: TaskSite, nt: TaskSite) -> None:
        label = f"task {nt.key}"
        if ot.own_id == nt.own_id:
            return  # only the group prefix changes (a move)
        arg = bt.task_id_arg
        if arg is not None:
            if not isinstance(arg.value, cst.SimpleString):
                self.refuse("unsupported", f"{label}: its task_id is not a plain string", nt.key)
                return
            if not KEY_RE.fullmatch(nt.own_id):
                self.refuse("unsupported", f"{label}: {nt.own_id!r} is not a valid task id", nt.key)
                return
            # A task id never needs escaping: keep the file's quote style when it is a plain
            # (unprefixed, single-line) literal, else write it the way Python prints it.
            quote = arg.value.quote
            plain = arg.value.prefix == "" and quote in ("'", '"')
            text = f"{quote}{nt.own_id}{quote}" if plain else repr(nt.own_id)
            self.plan.strings[id(arg.value)] = cst.SimpleString(text)
            self.ops.append(f"renamed task {ot.key} to {nt.key}")
            return
        if bt.kind == "taskflow":
            value = cst.SimpleString(repr(nt.own_id))
            if bt.call is not None:
                self.plan.call(bt.call).put["task_id"] = value
            elif bt.decorator is not None:
                edit = self.plan.decorators.setdefault(id(bt.decorator), _CallEdit())
                edit.put["task_id"] = value
            self.ops.append(f"renamed task {ot.key} to {nt.key} (task_id= added)")
            return
        self.refuse("unsupported", f"{label}: its task_id cannot be renamed", nt.key)

    def move_task(self, bt: TaskSite, nt: TaskSite) -> None:
        label = f"task {nt.key}"
        group = self.b.groups.get(bt.group) if bt.group else None
        if group is not None and group.with_stmt is not None:
            self.refuse(
                "unsupported",
                f"{label} moves to another task group, but it is written inside a 'with "
                "TaskGroup' block (moving code between blocks is not supported)",
                nt.key,
            )
            return
        target = GROUP_PREFIX + nt.group if nt.group else None
        if target is not None and target not in self.vars:
            self.refuse(
                "unsupported", f"{label}: the new group has no variable in the file", nt.key
            )
            return
        call = bt.call
        if call is None:
            self.refuse("unsupported", f"{label}: cannot set task_group on a bare @task", nt.key)
            return
        if not self.risky(f"{label} moves to another task group through task_group=", nt.key):
            return
        if target is None:
            self.plan.call(call).drop.add("task_group")
        else:
            self.plan.call(call).put["task_group"] = cst.Name(self.vars[target])
        self.ops.append(f"{label}: moved to group {nt.group or '(none)'}")

    def change_taskflow(
        self, bt: TaskSite, ot: TaskSite, nt: TaskSite, label: str, nk: str
    ) -> None:
        if ot.decorator is not None and nt.decorator is not None:
            od = ot.decorator.decorator
            nd = nt.decorator.decorator
            o_func = od.func if isinstance(od, cst.Call) else od
            n_func = nd.func if isinstance(nd, cst.Call) else nd
            changed = _norm(self.o, o_func) != _norm(self.n, n_func)
            if changed and self.risky(f"{label}: the TaskFlow decorator changes", nk):
                new_func = self.adopt(n_func, label, nk)
                if new_func is not None and bt.call is not None:
                    self.plan.call(bt.call).func = new_func
                    self.ops.append(f"{label}: changed the TaskFlow decorator")
                elif new_func is not None:
                    self.refuse(
                        "unsupported",
                        f"{label}: changing a bare @task decorator is not supported",
                        nk,
                    )
        self.kwargs(
            label,
            bt.call,
            ot.call,
            nt.call,
            skip={"task_id", "task_group"},
            task_id=nk,
            decorator=bt.decorator,
            rename_old=self.rename_key,
        )
        if ot.func is not None and nt.func is not None and bt.func is not None:
            if _body_dump(self.o, ot.func) != _body_dump(self.n, nt.func):
                adopted = self.adopt(nt.func.body, f"{label}: function body", nk)
                if adopted is not None:
                    self.plan.functions.setdefault(id(bt.func), _FuncEdit()).body = adopted
                    self.ops.append(f"{label}: replaced the function body")
            if _params_dump(self.o, ot.func) != _params_dump(self.n, nt.func):
                self.plan.functions.setdefault(id(bt.func), _FuncEdit()).params = nt.func.params
                self.ops.append(f"{label}: changed the function parameters")
        params = [p.name.value for p in bt.func.params.params] if bt.func is not None else []
        if ot.invocation is not None and nt.invocation is not None:
            if bt.invocation is None:
                self.refuse("unmappable", f"{label}: its call cannot be located", nk)
                return
            self.kwargs(
                f"{label} (arguments)",
                bt.invocation,
                ot.invocation,
                nt.invocation,
                task_id=nk,
                params=params,
                rename_old=self.rename_key,
            )

    def remove_task(self, ok: str) -> None:
        bt = self.b.tasks.get(ok)
        label = f"task {ok}"
        if bt is None:
            self.refuse(
                "unmappable",
                f"{label} was removed, but its definition cannot be located in the file",
                ok,
            )
            return
        if bt.limited:
            self.refuse("unmappable", f"{label} was removed, but it is {bt.limited}", ok)
            return
        if bt.statement is None:
            self.refuse(
                "unsupported",
                f"{label} was removed, but it is created inside another statement",
                ok,
            )
            return
        self.plan.removals.add(id(bt.statement))
        if bt.var:
            self.removed_vars[bt.var] = ok
        if bt.kind == "taskflow" and bt.func is not None:
            name = bt.func.name.value
            uses = [n for n in read_names(self.b.module) if n.value == name]
            if len(uses) <= 1:
                self.plan.removals.add(id(bt.func))
                self.removed_functions.add(name)
        self.ops.append(f"removed {label}")

    def add_task(self, nk: str) -> None:
        nt = self.n.tasks[nk]
        label = f"task {nk}"
        if self.b.dag.style == "assign":
            self.refuse(
                "unsupported",
                f"{label} cannot be added: the DAG is created with 'dag = DAG(...)' and its "
                "tasks are attached through dag=",
                nk,
            )
            return
        if nt.statement is None:
            self.refuse("unsupported", f"{label} cannot be inserted", nk)
            return
        statements: list[cst.BaseStatement] = []
        if nt.kind == "taskflow" and nt.func is not None:
            if nt.func.name.value in self.taken:
                self.refuse(
                    "conflict",
                    f"{label}: a function or variable named {nt.func.name.value!r} already "
                    "exists in the file",
                    nk,
                )
                return
            self.taken.add(nt.func.name.value)
            func = self.adopt(nt.func, label, nk)
            if func is None:
                return
            statements.append(func)
        var = self.fresh(self.var_name(nt.own_id, nt.var, "t"))
        self.vars[nk] = var  # before adopting: the statement assigns it
        stmt = self.adopt(nt.statement, label, nk)
        if stmt is None:
            return
        stmt = _rename_target(stmt, var)
        statements.append(stmt)
        group = self.b.groups.get(nt.group) if nt.group else None
        if group is not None and group.with_stmt is not None:
            statements = [_drop_task_group(s) for s in statements]
            body = group.with_stmt.body
            assert isinstance(body, cst.IndentedBlock)
            self.plan.insert(body, _Insert(after=None, statements=statements))
        else:
            self.insert_dag_statements(statements, group=nt.group)
        self.ops.append(f"added {label}")

    def var_name(self, own_id: str, generated: str | None, prefix: str) -> str:
        if self.plain_names and _identifier(own_id):
            return _identifier(own_id)
        return generated or f"{prefix}_{_identifier(own_id) or 'node'}"

    def fresh(self, base: str) -> str:
        name, i = base, 2
        while name in self.taken:
            name = f"{base}_{i}"
            i += 1
        self.taken.add(name)
        return name

    def insert_dag_statements(self, statements: list[cst.BaseStatement], group: str | None) -> None:
        body = self.b.dag.body
        anchor: cst.CSTNode | None = None
        defining: set[int] = set()
        for site in self.b.tasks.values():
            if site.container is body and site.statement is not None:
                defining.add(id(site.statement))
            if site.def_container is body and site.func is not None:
                defining.add(id(site.func))
        for g in self.b.groups.values():
            if g.container is body:
                defining.add(id(g.with_stmt or g.statement))
        for stmt in body.body:
            if id(stmt) in defining:
                anchor = stmt
        if anchor is None:
            self.plan.insert(body, _Insert(after=None, statements=statements, first=True))
        else:
            existing = [i for i in self.plan.inserts.get(id(body), []) if i.after is anchor]
            if existing:
                existing[-1].statements.extend(statements)
            else:
                self.plan.insert(body, _Insert(after=anchor, statements=statements))

    # ------------------------------------------------------------- edges
    def edges(self) -> None:
        old = _edge_view(self.o, self.rename_key)
        new = _edge_view(self.n)
        removed = old.keys() - new.keys()
        added = new.keys() - old.keys()
        for edge in old.keys() & new.keys():
            if (old[edge] or None) != (new[edge] or None):
                self.refuse(
                    "unsupported",
                    f"changing the label of the edge {edge[0]} -> {edge[1]} is not supported",
                )
        removed_tasks = {self.rename_key(k) for k in self.o.tasks} - set(self.n.tasks)
        self.remove_edges({e for e in removed}, removed_tasks)
        self.add_edges(sorted(added), new)

    def remove_edges(self, removed: set[tuple[str, str]], removed_tasks: set[str]) -> None:
        if not removed:
            return
        base_rev = {self.rename_key(k): k for k in self.b.tasks}
        # In the file's key space.
        targets = {(base_rev.get(a, a), base_rev.get(b, b)) for a, b in removed}
        found: set[tuple[str, str]] = set()
        roots, leaves = _group_ends(self.b)
        for dep in self.b.deps:
            edges = _expand(dep.edges, roots, leaves)
            hit = edges & targets
            if not hit:
                continue
            found |= hit
            if edges <= targets:
                self.plan.removals.add(id(dep.statement))
                self.ops.append("removed the dependency " + _code(self.b, dep.statement))
                continue
            replacement = _cut_chain(dep, targets, roots, leaves) if dep.kind == "binop" else None
            if replacement is None:
                self.refuse(
                    "unsupported",
                    "removing "
                    + ", ".join(f"{a} -> {b}" for a, b in sorted(hit))
                    + " needs rewriting the dependency statement "
                    + repr(_code(self.b, dep.statement)[:80]),
                )
                continue
            self.plan.replacements[id(dep.statement)] = replacement
            self.ops.append("changed the dependency " + _code(self.b, dep.statement))
        for a, b in sorted(targets - found):
            if (a, b) in self.b.implicit_edges:
                continue  # a data dependency: goes away with the argument (round trip checks)
            if a in removed_tasks or b in removed_tasks:
                pass
            self.refuse(
                "unsupported",
                f"the dependency {a} -> {b} cannot be removed: it is not written as a "
                "dependency statement AutoAir can edit",
            )

    def add_edges(self, added: list[tuple[str, str]], labels: dict[tuple[str, str], str]) -> None:
        by_source: dict[tuple[str, str], list[str]] = {}
        for a, b in added:
            if (a, b) in self.n.implicit_edges and b in self.data_targets | self.added_keys():
                continue  # the patched/added arguments create it
            by_source.setdefault((a, labels.get((a, b)) or ""), []).append(b)
        statements: list[cst.BaseStatement] = []
        for (a, label), targets in by_source.items():
            names = [self.vars.get(k) for k in [a, *targets]]
            if any(n is None for n in names):
                missing = [k for k, n in zip([a, *targets], names, strict=True) if n is None]
                self.refuse(
                    "unsupported",
                    f"the dependency {a} -> {', '.join(targets)} cannot be added: "
                    f"{', '.join(missing)} has no variable in the file",
                )
                continue
            right = (
                names[1] if len(targets) == 1 else "[" + ", ".join(str(n) for n in names[1:]) + "]"
            )
            if label:
                code = f"{names[0]} >> Label({label!r}) >> {right}"
            else:
                code = f"{names[0]} >> {right}"
            stmt = cst.parse_statement(code + "\n")
            statements.append(stmt)
            self.inserted.append(stmt)
            self.ops.append(f"added the dependency {code}")
        if statements:
            self.plan.insert(
                self.b.dag.body,
                _Insert(after=None, statements=statements, before_return=True),
            )

    def added_keys(self) -> set[str]:
        return set(self.n.tasks) - {self.rename_key(k) for k in self.o.tasks}

    # ------------------------------------------------------------- other statements
    def other_statements(self) -> None:
        old = _others(self.o, self.o.dag.body, self.rename_key)
        new = _others(self.n, self.n.dag.body)
        for dump, stmt in new.items():
            if dump in old:
                continue
            adopted = self.adopt(stmt, "a DAG statement")
            if adopted is not None:
                self.plan.insert(
                    self.b.dag.body, _Insert(after=None, statements=[adopted], before_return=True)
                )
                self.ops.append("added " + _code(self.n, stmt))
        base = _others(self.b, self.b.dag.body, self.rename_key)
        for dump, stmt in old.items():
            if dump in new:
                continue
            match = base.get(dump)
            if match is None:
                self.refuse(
                    "unsupported",
                    "the statement " + repr(_code(self.o, stmt)[:80]) + " was removed, but it "
                    "cannot be located in the file",
                )
                continue
            self.plan.removals.add(id(match))
            self.ops.append("removed " + _code(self.b, match))
        # Module level: definitions the generator adds (callbacks, models, helpers).
        old_m = _module_others(self.o)
        new_m = _module_others(self.n)
        dag_stmt = self.b.dag.statement
        for dump, stmt in new_m.items():
            if dump in old_m:
                continue
            name = stmt.name.value if isinstance(stmt, (cst.FunctionDef, cst.ClassDef)) else None
            if name is not None and name in self.taken:
                self.refuse("conflict", f"a definition named {name!r} already exists in the file")
                continue
            adopted = self.adopt(stmt, "a module-level definition")
            if adopted is None:
                continue
            index = list(self.b.module.body).index(dag_stmt)  # type: ignore[arg-type]
            anchor = self.b.module.body[index - 1] if index > 0 else None
            if anchor is None:
                self.plan.insert(
                    self.b.module, _Insert(after=None, statements=[adopted], first=True)
                )
            else:
                self.plan.insert(self.b.module, _Insert(after=anchor, statements=[adopted]))
            self.ops.append("added " + _code(self.n, stmt).splitlines()[0])
        for dump, stmt in old_m.items():
            if dump not in new_m:
                self.warnings.append(
                    "left in place a module-level definition the DAG no longer uses: "
                    + _code(self.o, stmt).splitlines()[0][:80]
                )

    # ------------------------------------------------------------- imports
    def imports(self) -> None:
        needed: dict[str, cst.BaseSmallStatement] = {}
        provided = _import_statements(self.n.module)
        local_names: set[str] = set()
        for node in self.inserted:
            if isinstance(node, (cst.FunctionDef, cst.ClassDef)):
                local_names.add(node.name.value)
        for node in self.inserted:
            inner: set[str] = set()
            if isinstance(node, cst.FunctionDef):
                inner = {p.name.value for p in node.params.params}
                if isinstance(node.body, cst.IndentedBlock):
                    inner |= bound_names(node.body.body)
            elif isinstance(node, cst.IndentedBlock):
                inner = bound_names(node.body)
            for ref in read_names(node):
                v = ref.value
                if (
                    v in self.b.bound_names
                    or v in BUILTIN_NAMES
                    or v in inner
                    or v in local_names
                    or v in self.vars.values()
                    or v in needed
                ):
                    continue
                if v in provided:
                    needed[v] = provided[v]
        if not needed:
            return
        lines: list[cst.BaseStatement] = []
        seen: set[str] = set()
        for name, small in needed.items():
            code = cst.Module(body=[cst.SimpleStatementLine(body=[small])]).code.strip()
            if code in seen:
                continue
            seen.add(code)
            lines.append(cst.parse_statement(code + "\n"))
            self.ops.append(f"added the import {code}")
            self.b.bound_names.add(name)
        anchor = None
        for stmt in self.b.module.body:
            if isinstance(stmt, cst.SimpleStatementLine) and all(
                isinstance(s, (cst.Import, cst.ImportFrom)) for s in stmt.body
            ):
                anchor = stmt
            elif anchor is not None:
                break
        if anchor is None:
            self.plan.insert(self.b.module, _Insert(after=None, statements=lines, first=True))
        else:
            self.plan.insert(self.b.module, _Insert(after=anchor, statements=lines))

    # ------------------------------------------------------------- run
    def run(self) -> PatchResult:
        self.dag()
        self.groups()
        self.tasks()
        self.edges()
        self.other_statements()
        self.imports()
        for note in self.b.unmapped:
            self.warnings.append(f"not mapped (left as written): {note}")
        if self.refusals:
            return PatchResult(None, False, self.ops, self.warnings, self.refusals)
        patched = self.b.module.visit(_Apply(self.plan))
        code = patched.code
        try:
            compile(code, "<patched>", "exec")
        except SyntaxError as exc:  # a bug of the patcher: never write it
            self.refuse(
                "syntax", f"the patched file does not compile: {exc.msg} (line {exc.lineno})"
            )
            return PatchResult(None, False, self.ops, self.warnings, self.refusals)
        still = {n.value for n in read_names(patched)}
        for var, key in self.removed_vars.items():
            if var in still:
                self.refuse(
                    "unsupported",
                    f"{key.lstrip(GROUP_PREFIX)!r} was removed, but the file still uses its "
                    f"variable {var!r}",
                    key,
                )
        if self.refusals:
            return PatchResult(None, False, self.ops, self.warnings, self.refusals)
        return PatchResult(code, code != self.b.module.code, self.ops, self.warnings, [])


# --------------------------------------------------------------------------- helpers


def _identifier(text: str) -> str:
    """``text`` as a Python identifier ("" when it cannot be one)."""
    import keyword
    import re

    name = re.sub(r"\W", "_", text)
    if not name or name[0].isdigit() or keyword.iskeyword(name):
        return ""
    return name


def _rename_target(stmt: cst.BaseStatement, var: str) -> cst.BaseStatement:
    if isinstance(stmt, cst.SimpleStatementLine) and len(stmt.body) == 1:
        small = stmt.body[0]
        if isinstance(small, cst.Assign) and len(small.targets) == 1:
            target = small.targets[0].with_changes(target=cst.Name(var))
            return stmt.with_changes(body=[small.with_changes(targets=[target])])
    return stmt


def _drop_task_group(stmt: cst.BaseStatement) -> cst.BaseStatement:
    class _Drop(cst.CSTTransformer):
        def leave_Call(self, original: cst.Call, updated: cst.Call) -> cst.Call:
            args = [a for a in updated.args if not (a.keyword and a.keyword.value == "task_group")]
            if len(args) == len(updated.args):
                return updated
            if args and updated.args:
                args[-1] = args[-1].with_changes(comma=updated.args[-1].comma)
            return updated.with_changes(args=args)

    result = stmt.visit(_Drop())
    assert isinstance(result, cst.BaseStatement)
    return result


def _code(m: SourceMap, node: cst.CSTNode) -> str:
    return m.module.code_for_node(node).strip()


def _import_names(module: cst.Module) -> set[str]:
    names: set[str] = set()
    for stmt in module.body:
        if isinstance(stmt, cst.SimpleStatementLine) and all(
            isinstance(s, (cst.Import, cst.ImportFrom)) for s in stmt.body
        ):
            names |= bound_names([stmt])
    return names


def _import_statements(module: cst.Module) -> dict[str, cst.BaseSmallStatement]:
    """Name -> an import statement binding just that name (from the generated module)."""
    out: dict[str, cst.BaseSmallStatement] = {}
    for stmt in module.body:
        if not isinstance(stmt, cst.SimpleStatementLine):
            continue
        for small in stmt.body:
            if isinstance(small, cst.ImportFrom) and not isinstance(small.names, cst.ImportStar):
                for alias in small.names:
                    local = (
                        alias.asname.name.value
                        if alias.asname and isinstance(alias.asname.name, cst.Name)
                        else dotted(alias.name)
                    )
                    if local:
                        out[local] = small.with_changes(
                            names=[alias.with_changes(comma=cst.MaybeSentinel.DEFAULT)]
                        )
            elif isinstance(small, cst.Import):
                for alias in small.names:
                    local = (
                        alias.asname.name.value
                        if alias.asname and isinstance(alias.asname.name, cst.Name)
                        else (dotted(alias.name) or "").split(".", 1)[0]
                    )
                    if local:
                        out[local] = small.with_changes(
                            names=[alias.with_changes(comma=cst.MaybeSentinel.DEFAULT)]
                        )
    return out


def _group_members(m: SourceMap) -> dict[str, set[str]]:
    members: dict[str, set[str]] = {g: set() for g in m.groups}
    for key, task in m.tasks.items():
        group = task.group
        while group is not None:
            members.setdefault(group, set()).add(key)
            parent = m.groups.get(group)
            group = parent.parent if parent else None
    return members


def _group_ends(m: SourceMap) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Per group: its roots (no upstream inside it) and leaves (no downstream inside)."""
    members = _group_members(m)
    plain: set[tuple[str, str]] = set(m.implicit_edges)
    for dep in m.deps:
        plain |= {
            e
            for e in dep.edges
            if not (e[0].startswith(GROUP_PREFIX) or e[1].startswith(GROUP_PREFIX))
        }
    roots: dict[str, set[str]] = {}
    leaves: dict[str, set[str]] = {}
    for group, keys in members.items():
        inner = {(a, b) for a, b in plain if a in keys and b in keys}
        roots[group] = {k for k in keys if not any(b == k for _, b in inner)}
        leaves[group] = {k for k in keys if not any(a == k for a, _ in inner)}
    return roots, leaves


def _expand(
    edges: Iterable[tuple[str, str]], roots: dict[str, set[str]], leaves: dict[str, set[str]]
) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for a, b in edges:
        ups = leaves.get(a[len(GROUP_PREFIX) :], set()) if a.startswith(GROUP_PREFIX) else {a}
        downs = roots.get(b[len(GROUP_PREFIX) :], set()) if b.startswith(GROUP_PREFIX) else {b}
        out |= {(u, d) for u in ups for d in downs}
    return out


def _edge_view(m: SourceMap, rename: Callable[[str], str] = str) -> dict[tuple[str, str], str]:
    """Task-level explicit edges (group endpoints expanded) -> label ("" when none)."""
    roots, leaves = _group_ends(m)
    out: dict[tuple[str, str], str] = {}
    for dep in m.deps:
        for edge in dep.edges:
            for a, b in _expand([edge], roots, leaves):
                key = (rename(a), rename(b))
                label = dep.labels.get(edge, "")
                out[key] = out.get(key) or label
    return out


def _cut_chain(
    dep: DepStatement,
    removed: set[tuple[str, str]],
    roots: dict[str, set[str]],
    leaves: dict[str, set[str]],
) -> list[cst.BaseStatement] | None:
    """``dep`` (``a >> b >> c``) without the ``removed`` edges, as 0..n statements; None when
    that needs more than cutting whole links (or shrinking the list at a chain end)."""
    if dep.operands is None or dep.has_labels:
        return None
    operands = [list(keys) for _, keys in dep.operands]
    nodes = [node for node, _ in dep.operands]
    ops = dep.ops

    def link_edges(i: int, left: list[str], right: list[str]) -> set[tuple[str, str]]:
        down = isinstance(ops[i], cst.RightShift)
        raw = {(a, b) if down else (b, a) for a in left for b in right}
        return _expand(raw, roots, leaves)

    cut: list[bool] = []
    for i in range(len(ops)):
        edges = link_edges(i, operands[i], operands[i + 1])
        hit = edges & removed
        if not hit:
            cut.append(False)
        elif hit == edges:
            cut.append(True)
        else:
            if len(ops) != 1:
                return None
            # one link: shrink a list operand
            for side in (0, 1):
                other = 1 - side
                keep = []
                for key in operands[side]:
                    pair = [key] if side == 0 else operands[0]
                    rest = operands[1] if side == 0 else [key]
                    if not (link_edges(0, pair, rest) <= removed):
                        keep.append(key)
                if len(keep) < len(operands[side]):
                    trial = [operands[0], operands[1]]
                    trial[side] = keep
                    remaining = (
                        link_edges(0, trial[0], trial[1]) if keep and trial[other] else set()
                    )
                    if remaining == edges - removed and keep:
                        shrunk = _shrink_list(nodes[side], operands[side], keep)
                        if shrunk is None:
                            return None
                        new_nodes = list(nodes)
                        new_nodes[side] = shrunk
                        return [_chain_statement(dep, new_nodes, list(ops))]
            return None
    pieces: list[tuple[list[cst.BaseExpression], list[cst.BaseBinaryOp]]] = []
    current: list[cst.BaseExpression] = [nodes[0]]
    current_ops: list[cst.BaseBinaryOp] = []
    for i, op in enumerate(ops):
        if cut[i]:
            pieces.append((current, current_ops))
            current, current_ops = [nodes[i + 1]], []
        else:
            current.append(nodes[i + 1])
            current_ops.append(op)
    pieces.append((current, current_ops))
    out: list[cst.BaseStatement] = []
    for k, (piece, piece_ops) in enumerate(pieces):
        if len(piece) < 2:
            continue
        stmt = _chain_statement(dep, piece, piece_ops)
        if out or k:  # later pieces: their own line without the original leading comments
            stmt = stmt.with_changes(leading_lines=[]) if out else stmt
        out.append(stmt)
    return out


def _shrink_list(
    node: cst.BaseExpression, keys: list[str], keep: list[str]
) -> cst.BaseExpression | None:
    if not isinstance(node, (cst.List, cst.Tuple)) or len(node.elements) != len(keys):
        return None
    elements = [el for el, key in zip(node.elements, keys, strict=True) if key in keep]
    if not elements:
        return None
    last_comma = node.elements[-1].comma
    elements[-1] = elements[-1].with_changes(comma=last_comma)
    return node.with_changes(elements=elements)


def _chain_statement(
    dep: DepStatement, operands: list[cst.BaseExpression], ops: list[cst.BaseBinaryOp]
) -> cst.SimpleStatementLine:
    expr: cst.BaseExpression = operands[0]
    for op, right in zip(ops, operands[1:], strict=True):
        expr = cst.BinaryOperation(left=expr, operator=op, right=right)
    small = dep.statement.body[0]
    assert isinstance(small, cst.Expr)
    return dep.statement.with_changes(body=[small.with_changes(value=expr)])


def _is_definition(m: SourceMap, stmt: cst.BaseStatement) -> bool:
    ids = {id(t.statement) for t in m.tasks.values() if t.statement is not None}
    ids |= {id(t.func) for t in m.tasks.values() if t.func is not None}
    ids |= {id(g.statement) for g in m.groups.values() if g.statement is not None}
    ids |= {id(g.with_stmt) for g in m.groups.values() if g.with_stmt is not None}
    ids |= {id(d.statement) for d in m.deps}
    return id(stmt) in ids


def _others(
    m: SourceMap, body: Container, rename: Callable[[str], str] = str
) -> dict[str, cst.BaseStatement]:
    """DAG-body statements that are neither task/group definitions nor dependencies
    (``t.as_setup()``, lineage hooks...), by normalised form."""
    out: dict[str, cst.BaseStatement] = {}
    for stmt in body.body:
        if _is_definition(m, stmt) or _is_docstring(stmt) or _is_return(stmt):
            continue
        if isinstance(stmt, cst.SimpleStatementLine) and all(
            isinstance(s, (cst.Pass, cst.Import, cst.ImportFrom)) for s in stmt.body
        ):
            continue
        if not isinstance(stmt, cst.SimpleStatementLine):
            continue  # compound statements (loops, ifs) are user code: never touched
        out[_norm(m, stmt, rename)] = stmt
    return out


def _module_others(m: SourceMap) -> dict[str, cst.BaseStatement]:
    out: dict[str, cst.BaseStatement] = {}
    for stmt in m.module.body:
        if stmt is m.dag.statement or _is_docstring(stmt) or _is_definition(m, stmt):
            continue
        if isinstance(stmt, cst.SimpleStatementLine) and all(
            isinstance(s, (cst.Import, cst.ImportFrom)) for s in stmt.body
        ):
            continue
        if isinstance(stmt, cst.FunctionDef) and stmt.name.value in {
            t.func.name.value for t in m.tasks.values() if t.func is not None
        }:
            continue
        out[_norm(m, stmt)] = stmt
    return out


def patch_source(
    base: str,
    old: str,
    new: str,
    *,
    dag_id: str,
    strict: bool = False,
    renames: dict[str, str] | None = None,
) -> PatchResult:
    """Patch ``base`` with the difference between the generated ``old`` and ``new`` files
    (see the module docstring). ``renames``: old task id -> new task id."""
    try:
        base_map = map_source(base, dag_id)
    except MapError as exc:
        return PatchResult(None, refusals=[PatchIssue("unmappable", str(exc))])
    try:
        old_map = map_source(old, dag_id)
        new_map = map_source(new, dag_id)
    except MapError as exc:  # generated code always maps: a bug, never patch blindly
        return PatchResult(None, refusals=[PatchIssue("unmappable", f"generated code: {exc}")])
    patcher = _Patcher(base_map, old_map, new_map, strict=strict, renames=dict(renames or {}))
    try:
        return patcher.run()
    except _Refused:
        return PatchResult(None, False, patcher.ops, patcher.warnings, patcher.refusals)


def unified_diff(before: str, after: str, path: str, *, limit: int | None = None) -> str:
    """A unified diff of ``before`` -> ``after`` (empty when they are equal). ``limit``:
    stop once the diff is longer than that many characters (the caller marks it cut)."""
    lines = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
    )
    parts: list[str] = []
    size = 0
    for line in lines:
        part = line if line.endswith("\n") else line + "\n\\ No newline at end of file\n"
        parts.append(part)
        size += len(part)
        if limit is not None and size > limit:
            break
    return "".join(parts)


__all__ = ["PatchIssue", "PatchResult", "literal_string", "patch_source", "unified_diff"]
