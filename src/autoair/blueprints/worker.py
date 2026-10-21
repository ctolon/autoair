"""Child-process side of the blueprint catalog: import modules, read schemas, preview.

Runs as ``python -c "from autoair.blueprints.worker import main; main()"`` with a request on
stdin, so blueprint modules (user code) never execute in the API process.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
from typing import Any

MARKER = "@@AUTOAIR-BLUEPRINTS@@"
PREVIEW_DAG_ID = "autoair_blueprint_preview"
PREVIEW_STEP_ID = "step"


def _first_error_line(exc: BaseException) -> str:
    text = str(exc).strip().splitlines()
    return f"{type(exc).__name__}: {text[0] if text else ''}".rstrip(": ")


def render_preview(cls: Any, config: dict[str, Any], step_id: str) -> dict[str, Any]:
    """Render ``cls`` with ``config`` inside a throwaway DAG; the task ids it creates."""
    from airflow.sdk import DAG, BaseOperator

    try:
        validated = cls.get_config_type().model_validate(config)
    except Exception as exc:  # pydantic.ValidationError: the defaults are not enough
        return {"error": "config needed: " + _first_error_line(exc)}
    with DAG(dag_id=PREVIEW_DAG_ID, schedule=None) as dag:
        instance = cls()
        instance.step_id = step_id
        result = instance.render(validated)
    prefix = step_id + "."
    tasks = [t[len(prefix) :] if t.startswith(prefix) else t for t in sorted(dag.task_dict)]
    return {"kind": "task" if isinstance(result, BaseOperator) else "group", "tasks": tasks}


def describe(module_name: str, class_names: list[str], preview: bool) -> list[dict[str, Any]]:
    import importlib

    from blueprint import Blueprint

    module = importlib.import_module(module_name)
    out: list[dict[str, Any]] = []
    for class_name in class_names:
        cls = getattr(module, class_name, None)
        record: dict[str, Any] = {"module": module_name, "class_name": class_name}
        if not (isinstance(cls, type) and issubclass(cls, Blueprint) and cls is not Blueprint):
            record["error"] = f"{class_name} is not a Blueprint subclass"
            out.append(record)
            continue
        try:
            name, version = cls.parse_name_and_version()
            config_type = cls.get_config_type()
            record.update(
                name=name,
                version=version,
                schema=cls.get_schema(),
                supports_params=bool(getattr(cls, "supports_params", False)),
                docstring=cls.__doc__,
                config_class=(
                    config_type.__name__
                    if getattr(module, config_type.__name__, None) is config_type
                    else None
                ),
            )
        except Exception as exc:
            record["error"] = _first_error_line(exc)
            out.append(record)
            continue
        if preview:
            try:
                record["preview"] = render_preview(cls, {}, PREVIEW_STEP_ID)
            except Exception as exc:
                record["preview"] = {"error": _first_error_line(exc)}
        out.append(record)
    return out


def main() -> None:  # pragma: no cover - executed in a child process
    """stdin: ``{"path": [dirs], "modules": [{module, classes}], "preview": bool}``."""
    import logging

    logging.disable(logging.CRITICAL)
    request = json.loads(sys.stdin.read())
    # Appended, never prepended: a file in a template folder must not shadow an installed
    # package (blueprint, pydantic) in this process.
    sys.path.extend(p for p in request.get("path", []) if p not in sys.path)
    results: list[dict[str, Any]] = []
    errors: dict[str, str] = {}
    try:
        import blueprint  # noqa: F401
    except ImportError as exc:
        errors["*"] = f"airflow-blueprint is not installed: {exc}"
    else:
        for item in request.get("modules", []):
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    results.extend(
                        describe(item["module"], item["classes"], bool(request.get("preview")))
                    )
            except BaseException as exc:
                errors[item["module"]] = _first_error_line(exc)
    sys.stdout.write(MARKER + json.dumps({"results": results, "errors": errors}, default=str))
    sys.stdout.flush()
