"""Apply environment aliases to explicit connection and Variable references.

Only literal references are rewritten. Dynamic expressions and ordinary string values
remain unchanged; mapping never imports or executes user code.
"""

from __future__ import annotations

import ast
from typing import Any

from autoair.spec.models import DagSpec


def _template(text: str, connections: dict[str, str], variables: dict[str, str]) -> str:
    from jinja2 import Environment, TemplateSyntaxError

    if "{{" not in text and "{%" not in text:
        return text
    try:
        tokens = list(Environment(keep_trailing_newline=True, autoescape=True).lex(text))
    except TemplateSyntaxError:
        return text
    output = [token[2] for token in tokens]
    significant = [i for i, token in enumerate(tokens) if token[1] != "whitespace"]
    changed = False
    cursor = 0
    while cursor < len(significant):
        start = significant[cursor]
        _, kind, root = tokens[start]
        if kind != "name" or root not in {"conn", "var"}:
            cursor += 1
            continue
        if cursor and tokens[significant[cursor - 1]][2] == ".":
            cursor += 1
            continue
        pos = cursor + 1
        mapping = connections
        if root == "var":
            if pos + 1 >= len(significant):
                break
            dot, namespace = (tokens[significant[i]][2] for i in (pos, pos + 1))
            if dot != "." or namespace not in {"value", "json"}:
                cursor += 1
                continue
            root += "." + namespace
            mapping = variables
            pos += 2
        if pos + 1 >= len(significant):
            break
        operator = tokens[significant[pos]][2]
        _, key_kind, key = tokens[significant[pos + 1]]
        end_pos = pos + 1
        if operator == "[" and key_kind == "string":
            if pos + 2 >= len(significant) or tokens[significant[pos + 2]][2] != "]":
                cursor += 1
                continue
            key = ast.literal_eval(key)
            end_pos += 1
        elif operator != "." or key_kind != "name":
            cursor += 1
            continue
        if key in mapping:
            end = significant[end_pos]
            output[start] = f"{root}[{mapping[key]!r}]"
            output[start + 1 : end + 1] = [""] * (end - start)
            changed = True
        cursor = end_pos + 1
    return "".join(output) if changed else text


def _python(code: str, connections: dict[str, str], variables: dict[str, str]) -> str:
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return code  # The normal validation path reports incomplete editor input.
    lines = code.encode().splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    edits: dict[tuple[int, int], bytes] = {}

    def replace(node: ast.expr, mapping: dict[str, str]) -> None:
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            return
        if node.value not in mapping or node.end_lineno is None or node.end_col_offset is None:
            return
        start = offsets[node.lineno - 1] + node.col_offset
        end = offsets[node.end_lineno - 1] + node.end_col_offset
        edits[start, end] = repr(mapping[node.value]).encode()

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg and (keyword.arg == "conn_id" or keyword.arg.endswith("_conn_id")):
                replace(keyword.value, connections)
        if not isinstance(node.func, ast.Attribute):
            continue
        func = node.func
        if isinstance(func.value, ast.Name) and func.value.id == "Variable" and func.attr == "get":
            if node.args:
                replace(node.args[0], variables)
            for keyword in node.keywords:
                if keyword.arg == "key":
                    replace(keyword.value, variables)
        elif func.attr in {"get_connection", "get_hook"}:
            if node.args:
                replace(node.args[0], connections)
    data = code.encode()
    for (start, end), replacement in sorted(edits.items(), reverse=True):
        data = data[:start] + replacement + data[end:]
    return data.decode()


def apply_mapping(spec: DagSpec, connections: dict[str, str], variables: dict[str, str]) -> DagSpec:
    """Return a validated copy; preserve overlay definitions and the original spec."""

    def visit(value: Any, key: str = "") -> Any:
        if isinstance(value, dict):
            if value.get("$type") == "function":
                return {**value, "source": _python(value["source"], connections, variables)}
            return {k: visit(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [visit(item, key) for item in value]
        if isinstance(value, str):
            if key == "conn_id" or key.endswith("_conn_id"):
                return connections.get(value, value)
            if key in {"code", "until_code"}:
                return _python(value, connections, variables)
            return _template(value, connections, variables)
        return value

    data = spec.model_dump(mode="json", by_alias=True)
    overlays = data.pop("environments")
    data = visit(data)
    data["environments"] = overlays
    return DagSpec.model_validate(data)


def mapping_targets(spec: DagSpec, env: str) -> tuple[dict[str, str], dict[str, str]]:
    """``(connections, variables)`` logical -> actual ids of environment ``env``."""
    overlay = spec.environments.get(env)
    if overlay is None:
        raise KeyError(f"unknown environment {env!r}")
    return dict(overlay.connections), dict(overlay.variables)
