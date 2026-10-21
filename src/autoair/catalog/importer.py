"""Import-based introspection, in-process (``strategy="import"``) or in a subprocess.

The subprocess entry point (:func:`_main`) is used as the fallback for classes the
static analyser cannot resolve, so provider code never runs inside the API process
by default.
"""

from __future__ import annotations

import importlib
import inspect
import json
import subprocess
import sys
from collections.abc import Collection
from typing import Any, Literal

from autoair._subprocess import user_code_env
from autoair.catalog.introspection import IntrospectedOperator, introspect_operator

Kind = Literal["operator", "notifier"]
_MARKER = "@@AUTOAIR-INTROSPECT@@"


def _bases(kind: Kind) -> tuple[type, ...]:
    if kind == "notifier":
        from airflow.sdk import BaseNotifier

        return (BaseNotifier,)
    from autoair._compat import base_operator_classes

    return base_operator_classes()


def _sensor_base() -> type | None:
    try:
        from airflow.sdk import BaseSensorOperator
    except ImportError:  # pragma: no cover
        return None
    sensor: type = BaseSensorOperator
    return sensor


def introspect_module(
    module_name: str, kind: Kind = "operator", only: Collection[str] | None = None
) -> list[tuple[dict[str, Any], IntrospectedOperator]]:
    """Import ``module_name`` and introspect its operator (or notifier) classes.

    ``only`` restricts to the given class names (and then ``Base*`` names are allowed).
    Raises on import errors.
    """
    module = importlib.import_module(module_name)
    bases = _bases(kind)
    if not bases:
        raise RuntimeError("Airflow is not installed")
    base = bases[0]
    sensor = _sensor_base()
    out: list[tuple[dict[str, Any], IntrospectedOperator]] = []
    for _, cls in inspect.getmembers(module, inspect.isclass):
        if only is not None:
            if cls.__name__ not in only:
                continue
        elif cls.__name__.startswith(("_", "Base")):
            continue
        if (
            cls.__module__ != module.__name__
            or not issubclass(cls, bases)
            or cls in bases
            or inspect.isabstract(cls)
        ):
            continue
        intro = introspect_operator(cls, base)
        record: dict[str, Any] = {
            "name": cls.__name__,
            "qualname": cls.__qualname__,
            "module": cls.__module__,
            "params": [
                {
                    "name": p.name,
                    "schema": p.schema,
                    "required": p.required,
                    "supported": p.supported,
                }
                for p in intro.params.values()
            ],
            "template_fields": intro.template_fields,
            "docstring": intro.docstring,
            "summary": intro.summary,
            "ui_color": getattr(cls, "ui_color", None) if kind == "operator" else None,
            "is_sensor": bool(sensor is not None and issubclass(cls, sensor)),
            "deprecated": "deprecated" in (cls.__doc__ or "").lower()[:400],
        }
        out.append((record, intro))
    return out


def _main() -> None:  # pragma: no cover - executed in a subprocess
    """Read ``[{module, kind, only}]`` from stdin, print records as JSON."""
    import contextlib
    import io
    import logging

    logging.disable(logging.CRITICAL)
    requests = json.loads(sys.stdin.read())
    results: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for req in requests:
        module = req["module"]
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                recs = introspect_module(module, req.get("kind", "operator"), req.get("only"))
            results.setdefault(module, []).extend(r for r, _ in recs)
        except BaseException as exc:
            errors[module] = f"{type(exc).__name__}: {exc}"
    sys.stdout.write(_MARKER + json.dumps({"results": results, "errors": errors}, default=str))
    sys.stdout.flush()


def introspect_in_subprocess(
    requests: list[dict[str, Any]], timeout: float = 300
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str]]:
    """Run :func:`introspect_module` for each request in one isolated interpreter."""
    if not requests:
        return {}, {}
    code = "from autoair.catalog.importer import _main; _main()"
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            input=json.dumps(requests),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=user_code_env(),  # imports user code: never AutoAir's own secrets
        )
    except subprocess.TimeoutExpired:
        msg = f"introspection subprocess timed out after {timeout}s"
        return {}, {r["module"]: msg for r in requests}
    marker = proc.stdout.rfind(_MARKER)
    if marker < 0:
        tail = (proc.stderr or proc.stdout).strip().splitlines()[-5:]
        msg = "introspection subprocess failed: " + (" | ".join(tail) or f"rc={proc.returncode}")
        return {}, {r["module"]: msg for r in requests}
    data = json.loads(proc.stdout[marker + len(_MARKER) :])
    return data.get("results", {}), data.get("errors", {})


__all__ = ["introspect_in_subprocess", "introspect_module"]
