"""Normalisation of ``@task`` function bodies typed or pasted into the builder.

A body copied out of a function keeps its indentation (``    x = 1``); Python would report
"unexpected indent". Bodies whose lines share a common leading indentation are dedented
before they are parsed, linted, analysed or generated, so pasted bodies parse. Line numbers
do not change (only leading whitespace is removed).
"""

from __future__ import annotations

import ast
import textwrap


def _parses(code: str) -> bool:
    try:
        ast.parse(code)
    except SyntaxError:
        return False
    return True


def _dedent_tail(code: str) -> str:
    """Selection started after the first line's indentation: only the following lines are
    indented (``x = 1\\n    y = 2``)."""
    first, sep, rest = code.partition("\n")
    if not sep or first[:1] in (" ", "\t"):
        return code
    return first + sep + textwrap.dedent(rest)


def normalize_task_body(code: str) -> str:
    """``code`` dedented when that is what makes it a valid body (else unchanged, except a
    uniform indentation, which a top-level body never has, is always removed)."""
    if not code or _parses(code):
        return code
    dedented = textwrap.dedent(code)
    if dedented != code:
        return dedented
    tail = _dedent_tail(code)
    if tail != code and _parses(tail):
        return tail
    return code


__all__ = ["normalize_task_body"]
