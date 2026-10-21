"""List hard-coded values in ``src/autoair`` and render the audit table.

Walks the Python AST of every module and reports:

* ``constant`` - module-level ``UPPER_CASE`` assignments;
* ``default`` - numeric literals used as function parameter defaults;
* ``kwarg`` - numeric literals passed as keyword arguments (``timeout=30``, ``max_bytes=…``);
* ``number`` - other numeric literals >= 100 (sizes, truncation lengths, slices…);
* ``regex`` - patterns given to ``re.compile`` / ``re.match`` … and ``Field(pattern=…)``;
* ``url`` - ``http(s)://`` / ``ws(s)://`` string literals;
* ``path`` - string literals that look like file system paths.

Each finding has a stable *key* (file, scope, kind, name — no line number) so the triage
decisions in ``scripts/hardcoded_decisions.json`` (the committed source of truth) survive
unrelated edits. ``--check`` fails when a finding has no decision (a new hard-coded value must be
triaged) or a decision names a value that no longer exists. ``--write`` renders the audit as a
local report in the gitignored ``.artifacts/`` folder; it is not committed::

    uv run python scripts/scan_hardcoded.py            # print findings without a decision
    uv run python scripts/scan_hardcoded.py --json     # every finding as JSON
    uv run python scripts/scan_hardcoded.py --write    # write .artifacts/hardcoded-audit.md
    uv run python scripts/scan_hardcoded.py --check    # CI: every finding triaged, none stale

Paths owned by other areas (AI, MCP, IAM/auth) are audited by their owners and skipped here
(``EXCLUDED``).
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
PACKAGE = SRC / "autoair"
DECISIONS = ROOT / "scripts" / "hardcoded_decisions.json"
AUDIT = ROOT / ".artifacts" / "hardcoded-audit.md"

# Owned (and audited) by other areas.
EXCLUDED = (
    "autoair/ai/",
    "autoair/mcp/",
    "autoair/iam/",
    "autoair/auth.py",
    "autoair/api/routers/ai.py",
    # Jinja templates of generated projects (autoair new-node), not AutoAir code.
    "autoair/scaffold/custom-node/",
)
REGEX_FUNCS = {
    "compile",
    "match",
    "fullmatch",
    "search",
    "sub",
    "subn",
    "findall",
    "split",
    "finditer",
}
_UPPER = re.compile(r"^_?[A-Z][A-Z0-9_]*$")
_PATHLIKE = re.compile(
    r"^(?:~?/|\./|\.\./)[\w.~/{}<>-]*$|^[\w.-]+/[\w./-]*\.(?:json|py|yaml|yml|cfg|ini|lock|sqlite3|db|txt|md)$"
)
_URL = re.compile(r"^(?:https?|wss?)://", re.I)


@dataclass
class Finding:
    key: str
    file: str
    line: int
    kind: str
    name: str
    value: str


def _clip(text: str, limit: int = 90) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _unparsed(node: ast.AST) -> str:
    """``ast.unparse`` text: only for numeric expressions, whose form is stable across versions.

    ``ast.unparse`` is not stable for everything: Python 3.10 writes comprehension targets as
    ``for (a, b) in``, 3.11+ as ``for a, b in``. Displayed values therefore come from the
    source text (``_Visitor._short``), and keys only ever digest numbers.
    """
    try:
        return _clip(ast.unparse(node))
    except Exception:  # pragma: no cover - unparse handles every node
        return "<?>"


def _is_number(node: ast.AST) -> bool:
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        node = node.operand
    if isinstance(node, ast.BinOp):  # 5 * 1024 * 1024
        return _is_number(node.left) and _is_number(node.right)
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, (int, float))
        and not isinstance(node.value, bool)
    )


def _num_value(node: ast.AST) -> float | None:
    try:
        value = ast.literal_eval(node)
    except Exception:
        try:
            value = eval(compile(ast.Expression(node), "<n>", "eval"), {"__builtins__": {}})  # noqa: S307
        except Exception:
            return None
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode(), usedforsecurity=False).hexdigest()[:8]


class _Visitor(ast.NodeVisitor):
    def __init__(self, rel: str, source: str) -> None:
        self.rel = rel
        self.source = source
        self.scope: list[str] = []
        self.findings: list[Finding] = []
        self.seen: set[int] = set()  # node ids already reported (no double counting)
        self.counts: dict[str, int] = {}

    # ----------------------------------------------------------------- helpers
    def _add(self, node: ast.AST, kind: str, name: str, value: str) -> None:
        scope = ".".join(self.scope) or "<module>"
        base = f"{self.rel}::{scope}::{kind}::{name}"
        n = self.counts.get(base, 0)
        self.counts[base] = n + 1
        key = base if n == 0 else f"{base}#{n}"
        self.findings.append(Finding(key, self.rel, getattr(node, "lineno", 0), kind, name, value))

    def _short(self, node: ast.AST) -> str:
        """The value as written in the source, so it reads the same on every Python version.

        String constants use ``repr``: before 3.12, the parts of an f-string carry the position
        of the whole f-string, so their source segment would be the f-string itself.
        """
        if isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
            return _clip(repr(node.value))
        segment = ast.get_source_segment(self.source, node)
        return _clip(segment) if segment is not None else _unparsed(node)

    def _mark(self, node: ast.AST) -> None:
        for sub in ast.walk(node):
            self.seen.add(id(sub))

    # ----------------------------------------------------------------- scopes
    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        args = node.args
        positional = [*args.posonlyargs, *args.args]
        with_default = positional[len(positional) - len(args.defaults) :]
        defaults = list(zip(with_default, args.defaults, strict=True))
        kw_pairs = zip(args.kwonlyargs, args.kw_defaults, strict=True)
        defaults += [(a, d) for a, d in kw_pairs if d is not None]
        self.scope.append(node.name)
        for arg, default in defaults:
            if _is_number(default):
                self._add(default, "default", arg.arg, self._short(default))
                self._mark(default)
        self.generic_visit(node)
        self.scope.pop()

    visit_FunctionDef = _function
    visit_AsyncFunctionDef = _function

    # ----------------------------------------------------------------- constants
    def _module_assign(self, targets: list[ast.expr], value: ast.AST | None, node: ast.AST) -> None:
        if self.scope or value is None:
            return
        for target in targets:
            if isinstance(target, ast.Name) and _UPPER.match(target.id):
                self._add(node, "constant", target.id, self._short(value))
                self._mark(value)
                return

    def visit_Assign(self, node: ast.Assign) -> None:
        self._module_assign(node.targets, node.value, node)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._module_assign([node.target], node.value, node)
        self.generic_visit(node)

    # ----------------------------------------------------------------- calls
    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        fname = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        owner = (
            func.value.id
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
            else ""
        )
        if owner == "re" and fname in REGEX_FUNCS and node.args:
            pattern = node.args[0]
            if (
                isinstance(pattern, ast.Constant)
                and isinstance(pattern.value, (str, bytes))
                and id(pattern) not in self.seen
            ):
                text = pattern.value if isinstance(pattern.value, str) else repr(pattern.value)
                self._add(
                    pattern, "regex", f"re.{fname}:{_digest(str(text))}", self._short(pattern)
                )
                self._mark(pattern)
        for kw in node.keywords:
            if kw.arg is None or id(kw.value) in self.seen:
                continue
            if (
                kw.arg == "pattern"
                and isinstance(kw.value, ast.Constant)
                and isinstance(kw.value.value, str)
            ):
                self._add(
                    kw.value, "regex", f"pattern:{_digest(kw.value.value)}", self._short(kw.value)
                )
                self._mark(kw.value)
            elif _is_number(kw.value):
                self._add(kw.value, "kwarg", f"{fname}({kw.arg}=)", self._short(kw.value))
                self._mark(kw.value)
        self.generic_visit(node)

    # ----------------------------------------------------------------- literals
    def visit_Constant(self, node: ast.Constant) -> None:
        if id(node) in self.seen:
            return
        value = node.value
        if isinstance(value, str):
            text = value.strip()
            if _URL.match(text):
                self._add(node, "url", _digest(text), self._short(node))
            elif "\n" not in text and _PATHLIKE.match(text) and len(text) > 1:
                self._add(node, "path", _digest(text), self._short(node))
        elif _is_number(node):
            number = _num_value(node)
            if number is not None and abs(number) >= 100:
                self._add(node, "number", f"{_digest(repr(value))}", self._short(node))

    def visit_BinOp(self, node: ast.BinOp) -> None:
        if id(node) not in self.seen and _is_number(node):
            number = _num_value(node)
            if number is not None and abs(number) >= 100:
                self._add(node, "number", _digest(_unparsed(node)), self._short(node))
            self._mark(node)
            return
        self.generic_visit(node)


def scan(package: Path = PACKAGE) -> list[Finding]:
    findings: list[Finding] = []
    for path in sorted(package.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if any(rel.startswith(prefix) for prefix in EXCLUDED):
            continue
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        # Docstrings are prose, not values.
        for node in ast.walk(tree):
            body = getattr(node, "body", None)
            if (
                isinstance(body, list)
                and body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                body.pop(0)
        visitor = _Visitor(rel, source)
        visitor.visit(tree)
        findings.extend(visitor.findings)
    return findings


# --------------------------------------------------------------------------- decisions


def load_decisions() -> dict[str, dict[str, str]]:
    """``{key: {"decision": "config"|"keep", "setting"|"reason": …}}``.

    The JSON file holds explicit ``decisions`` (by key) and ordered ``rules`` (``match``: a
    regex searched in ``"<file>::<kind>::<name>::<value>"``) for whole families of values
    (HTTP status codes, REST paths, spec schema bounds…); explicit decisions win.
    """
    if not DECISIONS.exists():
        return {}
    data = json.loads(DECISIONS.read_text(encoding="utf-8"))
    explicit: dict[str, dict[str, str]] = data.get("decisions", {})
    rules: list[dict[str, str]] = data.get("rules", [])
    out: dict[str, dict[str, str]] = {}
    for f in scan():
        if f.key in explicit:
            out[f.key] = explicit[f.key]
            continue
        subject = f"{f.file}::{f.kind}::{f.name}::{f.value}"
        for rule in rules:
            if re.search(rule["match"], subject):
                out[f.key] = {k: v for k, v in rule.items() if k != "match"}
                break
    return out


def explicit_keys() -> set[str]:
    if not DECISIONS.exists():
        return set()
    return set(json.loads(DECISIONS.read_text(encoding="utf-8")).get("decisions", {}))


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def render(findings: list[Finding], decisions: dict[str, dict[str, str]]) -> str:
    configurable = [f for f in findings if decisions.get(f.key, {}).get("decision") == "config"]
    kept = [f for f in findings if decisions.get(f.key, {}).get("decision") == "keep"]
    lines = [
        "# Hard-coded values audit",
        "",
        "Generated by `scripts/scan_hardcoded.py --write` from the AST scan of `src/autoair`",
        "and the triage in `scripts/hardcoded_decisions.json`. It is a local report, not a",
        "committed file: `--check` (and a test) fails when a value has no decision or a",
        "decision is stale.",
        "",
        "* **config** — made configurable; the setting is named (see",
        "  [configuration-reference.md](../docs/configuration-reference.md)).",
        "* **keep** — kept in code, with the reason (protocol/spec constants, internal mechanics,",
        "  values whose change would break compatibility…).",
        "",
        "Not covered here: `autoair/ai/**`, `autoair/mcp/**`, `api/routers/ai.py` (AI area) and",
        "`autoair/auth.py`, `autoair/iam/**` (IAM area) — audited by their owners.",
        "",
        f"**{len(findings)} findings: {len(configurable)} configurable, {len(kept)} kept.**",
        "",
    ]
    by_file: dict[str, list[Finding]] = {}
    for f in findings:
        by_file.setdefault(f.file, []).append(f)
    for file in sorted(by_file):
        lines += [
            f"## `{file}`",
            "",
            "| Line | Kind | Name | Value | Decision |",
            "|---|---|---|---|---|",
        ]
        for f in by_file[file]:
            d = decisions.get(f.key, {})
            if d.get("decision") == "config":
                verdict = f"config: `{d.get('setting', '?')}`"
            elif d.get("decision") == "keep":
                verdict = f"keep: {d.get('reason', '')}"
            else:
                verdict = "**UNTRIAGED**"
            name = f.name if f.kind in ("constant", "default", "kwarg") else ""
            lines.append(
                f"| {f.line} | {f.kind} | {_cell(name)} | `{_cell(f.value)}` | {_cell(verdict)} |"
            )
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--json", action="store_true", help="print every finding as JSON")
    parser.add_argument("--write", action="store_true", help="write .artifacts/hardcoded-audit.md")
    parser.add_argument(
        "--check", action="store_true", help="fail on untriaged findings or stale decisions"
    )
    args = parser.parse_args(argv)

    findings = scan()
    decisions = load_decisions()
    if args.json:
        json.dump([asdict(f) for f in findings], sys.stdout, indent=1)
        sys.stdout.write("\n")
        return 0
    missing = [
        f for f in findings if decisions.get(f.key, {}).get("decision") not in ("config", "keep")
    ]
    stale = sorted(explicit_keys() - {f.key for f in findings})
    if args.write:
        AUDIT.parent.mkdir(parents=True, exist_ok=True)
        AUDIT.write_text(render(findings, decisions) + "\n", encoding="utf-8")
        print(f"wrote {AUDIT.relative_to(ROOT)} ({len(findings)} findings)")
    if args.check:
        problems = []
        if missing:
            problems.append(f"{len(missing)} untriaged finding(s)")
        if stale:
            problems.append(f"{len(stale)} decision(s) for values that no longer exist")
        for f in missing:
            print(f"untriaged: {f.file}:{f.line} {f.kind} {f.name} = {f.value}  [{f.key}]")
        for key in stale:
            print(f"stale decision: {key}")
        if problems:
            print("; ".join(problems), file=sys.stderr)
            return 1
        return 0
    if not args.write:
        for f in missing:
            print(f"{f.file}:{f.line}\t{f.kind}\t{f.name}\t{f.value}\t[{f.key}]")
        print(f"{len(findings)} findings, {len(missing)} without a decision", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
