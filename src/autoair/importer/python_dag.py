"""Best-effort import of hand-written Python DAGs into an AutoAir spec.

The DAG file is imported with Airflow's ``DagBag`` in an isolated subprocess (user code
never runs in the API server). The live DAG objects are then reverse-engineered:
operators and their constructor arguments, TaskFlow functions (source extracted with
``inspect``), task groups, dependencies, schedule, params and common task settings.
Anything that cannot be represented is reported as a warning.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from autoair._subprocess import user_code_env
from autoair.spec import DagSpec


class ImportedDag(BaseModel):
    dag_id: str
    spec: DagSpec | None
    warnings: list[str]
    error: str | None = None


class PythonImportResult(BaseModel):
    dags: list[ImportedDag]
    import_errors: list[str]
    source: str | None = Field(
        default=None,
        description="The imported file's code (imports from Airflow), e.g. to open a DAG that "
        "cannot be converted in the code editor.",
    )


def import_python_source(
    source: str, filename: str = "dag.py", timeout: float = 90
) -> PythonImportResult:
    with tempfile.TemporaryDirectory(prefix="autoair-import-") as tmp:
        path = Path(tmp) / filename
        path.write_text(source, encoding="utf-8")
        return import_python_file(path, timeout=timeout)


def import_python_sources(
    sources: Sequence[tuple[str, str]],
    timeout: float = 90,
    *,
    pythonpath: Sequence[Path] = (),
) -> list[PythonImportResult]:
    """Import several ``(source, filename)`` files in one subprocess (each with its own
    DagBag, so files defining the same DAG do not collide); one result per file.
    ``pythonpath``: folders the files may import from (the dags folder they live in)."""
    if not sources:
        return []
    with tempfile.TemporaryDirectory(prefix="autoair-import-") as tmp:
        paths = []
        for i, (source, filename) in enumerate(sources):
            folder = Path(tmp) / str(i)
            folder.mkdir()
            path = folder / filename
            path.write_text(source, encoding="utf-8")
            paths.append(path)
        out = _subprocess_import(paths, timeout, pythonpath=pythonpath)
    if isinstance(out, PythonImportResult):
        return [out.model_copy(deep=True) for _ in sources]
    return out


def import_python_file(
    path: Path, timeout: float = 90, *, pythonpath: Sequence[Path] = ()
) -> PythonImportResult:
    """``pythonpath``: folders importable by the DAG file (e.g. the root of the repository it
    comes from, like Airflow puts the dags folder on ``sys.path``)."""
    out = _subprocess_import([path], timeout, pythonpath=pythonpath)
    return out if isinstance(out, PythonImportResult) else out[0]


def _subprocess_import(
    paths: Sequence[Path], timeout: float, *, pythonpath: Sequence[Path] = ()
) -> PythonImportResult | list[PythonImportResult]:
    """One result per path, or a single failed result (timeout, crash) for all of them."""
    code = "import sys; from autoair.importer.python_dag import _run; _run(*sys.argv[1:])"
    env = user_code_env()  # the DAG's code runs there: no AutoAir secrets
    if pythonpath:
        extra = os.pathsep.join(str(p) for p in pythonpath)
        env["PYTHONPATH"] = extra + (
            os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
        )
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code, *(str(p) for p in paths)],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return PythonImportResult(dags=[], import_errors=[f"import timed out after {timeout}s"])
    marker = proc.stdout.rfind("@@AUTOAIR@@")
    if marker < 0:
        tail = (proc.stderr or proc.stdout).strip().splitlines()[-25:]
        return PythonImportResult(dags=[], import_errors=["\n".join(tail) or "import failed"])
    payload = json.loads(proc.stdout[marker + len("@@AUTOAIR@@") :])
    return [_result(data) for data in payload["files"]]


def _result(data: dict[str, Any]) -> PythonImportResult:
    dags = []
    for d in data["dags"]:
        spec = None
        error = d.get("error")
        if d.get("spec") is not None:
            try:
                spec = DagSpec.model_validate(d["spec"])
            except Exception as exc:
                error = f"converted spec is invalid: {exc}"
        dags.append(ImportedDag(dag_id=d["dag_id"], spec=spec, warnings=d["warnings"], error=error))
    return PythonImportResult(dags=dags, import_errors=data["import_errors"])


# --------------------------------------------------------------------------- subprocess side


def _run(*dag_files: str) -> None:  # pragma: no cover - executed in a subprocess
    _record_cron_presets()
    out = {"files": [_import_one(f) for f in dag_files]}
    sys.stdout.write("\n@@AUTOAIR@@" + json.dumps(out, default=str))


def _import_one(dag_file: str) -> dict[str, Any]:  # pragma: no cover - executed in a subprocess
    import inspect as _inspect

    from autoair._compat import dagbag_class
    from autoair.importer.blueprints import record_renders

    record_renders()
    cls = dagbag_class()
    kwargs: dict[str, Any] = {"dag_folder": dag_file}
    if "include_examples" in _inspect.signature(cls.__init__).parameters:
        kwargs["include_examples"] = False
    bag = cls(**kwargs)
    out: dict[str, Any] = {
        "dags": [],
        "import_errors": [str(e) for e in bag.import_errors.values()],
    }
    for dag_id, dag in sorted(bag.dags.items()):
        warnings: list[str] = []
        try:
            spec = _convert_dag(dag, warnings)
            out["dags"].append({"dag_id": dag_id, "spec": spec, "warnings": warnings})
        except Exception as exc:
            out["dags"].append(
                {
                    "dag_id": dag_id,
                    "spec": None,
                    "warnings": warnings,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
    return out


# Cron presets as the spec stores them (``@annually`` is Airflow's alias of ``@yearly``).
_PRESETS = {
    "@hourly": "@hourly",
    "@daily": "@daily",
    "@weekly": "@weekly",
    "@monthly": "@monthly",
    "@quarterly": "@quarterly",
    "@yearly": "@yearly",
    "@annually": "@yearly",
}
# id(timetable) -> (timetable, expression as written): Airflow resolves presets in place.
_CRON_SOURCES: dict[int, tuple[Any, str]] = {}


def _record_cron_presets() -> None:  # pragma: no cover - executed in a subprocess
    """Remember the expression each cron timetable was created with, so that
    ``CronTriggerTimetable("@daily", ...)`` is imported as the ``@daily`` preset."""
    import importlib

    for module in ("airflow.sdk.definitions.timetables._cron", "airflow.timetables._cron"):
        try:
            mixin = importlib.import_module(module).CronMixin
        except (ImportError, AttributeError):
            continue
        hook = "__attrs_post_init__" if "__attrs_post_init__" in vars(mixin) else "__init__"
        original = vars(mixin).get(hook)
        if original is None or getattr(original, "__autoair__", False):
            continue

        def wrapped(self: Any, *args: Any, __orig: Any = original, **kw: Any) -> None:
            written = kw.get("cron", kw.get("expression"))
            if written is None:
                written = args[0] if args else getattr(self, "expression", None)
            if isinstance(written, str):
                _CRON_SOURCES[id(self)] = (self, written)
            __orig(self, *args, **kw)

        wrapped.__autoair__ = True  # type: ignore[attr-defined]
        setattr(mixin, hook, wrapped)


def _preset_of(dag: Any, tt: Any) -> str | None:
    """The cron preset ``dag``'s schedule was written as, if any."""
    written = dag.schedule if isinstance(getattr(dag, "schedule", None), str) else None
    if written is None:
        entry = _CRON_SOURCES.get(id(tt))
        if entry is not None and entry[0] is tt:
            written = entry[1]
    if written is None:
        # Airflow 3.2's Task SDK timetables are attrs classes that keep the preset as the
        # expression (no CronMixin hook runs to record it).
        expr = _expression(tt)
        written = expr if expr and expr.startswith("@") else None
    return _PRESETS.get(written.strip().lower()) if written else None


def _expression(tt: Any) -> str | None:
    value = getattr(tt, "_expression", None) or getattr(tt, "expression", None)
    return value if isinstance(value, str) else None


def _resolve_preset(expr: str | None) -> str | None:
    """``@daily`` -> ``0 0 * * *`` (a timetable that kept the preset unresolved)."""
    if not expr or not expr.startswith("@"):
        return expr
    try:
        from airflow.utils.dates import cron_presets
    except ImportError:  # pragma: no cover - present in every Airflow 3.x
        return expr
    resolved: str = cron_presets.get(_PRESETS.get(expr.strip().lower(), expr), expr)
    return resolved


# ``<run_with> <tmp>/airflow-scripts-<uid>/<dag_id>/<sha256[:16]>/<name> `` (see codegen's
# MATERIALIZE_TEMPLATE; ``airflow-scripts/`` before 0.2.3, ``airflow-scripts-<random>`` for
# a private fallback folder): a script written in the builder, materialised at parse time.
_SCRIPT_PATH = (
    r"^(?:(?P<run>[A-Za-z0-9_./-]{1,40}) )?"
    r"(?P<path>\S*[/\\]airflow-scripts(?:-[A-Za-z0-9_]+)?[/\\][^/\\]+[/\\]"
    r"(?P<digest>[0-9a-f]{16})[/\\]"
    r"(?P<name>[A-Za-z0-9][A-Za-z0-9_.-]{0,120}))(?P<tail> ?)$"
)


def _script_file(value: str) -> dict[str, Any] | None:
    """The embedded script a materialised path stands for (its content is read back from
    the file the DAG wrote while it was parsed, and checked against the path's digest)."""
    import hashlib
    import re
    import tempfile

    m = re.match(_SCRIPT_PATH, value)
    if m is None or bool(m.group("run")) != bool(m.group("tail")):
        return None
    path = Path(m.group("path"))
    tmp = Path(tempfile.gettempdir()).resolve()
    try:
        resolved = path.resolve()
        if not resolved.is_relative_to(tmp) or not resolved.is_file():
            return None
        top = resolved.relative_to(tmp).parts[0]
        if not (top == "airflow-scripts" or top.startswith("airflow-scripts-")):
            return None
        content = resolved.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    if hashlib.sha256(content.encode()).hexdigest()[:16] != m.group("digest"):
        return None
    out: dict[str, Any] = {"$type": "file", "name": m.group("name"), "content": content}
    if m.group("run"):
        out["run_with"] = m.group("run")
    return out


def _jsonable(value: Any) -> tuple[bool, Any]:
    import datetime

    if isinstance(value, str) and "airflow-scripts" in value:
        script = _script_file(value)
        if script is not None:
            return True, script
    if isinstance(value, datetime.timedelta):
        return True, {"$type": "timedelta", "seconds": value.total_seconds()}
    if isinstance(value, datetime.datetime):
        return True, {"$type": "datetime", "value": value.isoformat()}
    if isinstance(value, (list, tuple)):
        items = [_jsonable(v) for v in value]
        return all(ok for ok, _ in items), [v for _, v in items]
    if isinstance(value, dict):
        items2 = {k: _jsonable(v) for k, v in value.items()}
        ok = all(isinstance(k, str) for k in value) and all(ok for ok, _ in items2.values())
        return ok, {k: v for k, (_, v) in items2.items()}
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return False, None
    return True, value


def _asset(a: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    name, uri, group = (getattr(a, k, None) for k in ("name", "uri", "group"))
    if name:
        out["name"] = name
    if uri and uri != name:
        out["uri"] = uri
    if group and group != "asset":
        out["group"] = group
    return out


def _asset_expr(cond: Any, warnings: list[str]) -> dict[str, Any]:
    kind = type(cond).__name__
    if kind == "Asset":
        return {"op": "asset", "asset": _asset(cond)}
    if kind == "AssetAlias":
        return {"op": "alias", "name": cond.name}
    if kind in ("AssetAll", "AssetAny", "AssetBooleanCondition"):
        objects = getattr(cond, "objects", [])
        return {
            "op": "all" if kind == "AssetAll" else "any",
            "items": [_asset_expr(o, warnings) for o in objects],
        }
    if kind == "AssetRef" or kind.endswith("Ref"):
        name = getattr(cond, "name", None) or getattr(cond, "uri", None)
        return {
            "op": "asset",
            "asset": {"name": name} if getattr(cond, "name", None) else {"uri": name},
        }
    warnings.append(f"unsupported asset condition {kind}")
    return {"op": "asset", "asset": {"name": str(cond)}}


def _schedule(dag: Any, warnings: list[str]) -> dict[str, Any]:
    tt = dag.timetable
    kind = type(tt).__name__
    tz = getattr(tt, "_timezone", None) or getattr(tt, "timezone", None)
    tz_name = getattr(tz, "name", None) or (str(tz) if tz else None)
    if tz_name in ("UTC", "Timezone('UTC')"):
        tz_name = None
    if kind == "NullTimetable":
        return {"type": "none"}
    if kind == "OnceTimetable":
        return {"type": "preset", "preset": "@once"}
    if kind == "ContinuousTimetable":
        return {"type": "preset", "preset": "@continuous"}
    if kind in ("CronTriggerTimetable", "CronDataIntervalTimetable"):
        preset = _preset_of(dag, tt)
        dag_tz = getattr(getattr(dag, "timezone", None), "name", None)
        immediately = _run_immediately(tt, warnings)
        window = _interval_seconds(tt)
        plain = not immediately and not window
        utc = ("UTC", "Etc/UTC")
        same_tz = (tz_name or "UTC") == ("UTC" if dag_tz in utc else (dag_tz or "UTC"))
        # ``schedule="@daily"`` follows Airflow's timetable config either way; an explicit
        # CronDataIntervalTimetable("@daily") stays a cron schedule with data intervals.
        explicit = not isinstance(getattr(dag, "schedule", None), str)
        if explicit and kind == "CronDataIntervalTimetable":
            preset = None
        if preset and plain and same_tz:
            # The spec's preset is generated in the DAG timezone (see codegen), so the
            # preset round-trips as long as the timetable uses that timezone.
            return {"type": "preset", "preset": preset}
        out: dict[str, Any] = {
            "type": "cron",
            "expression": _resolve_preset(_expression(tt)),
            "data_interval": kind == "CronDataIntervalTimetable",
        }
        if tz_name:
            out["timezone"] = tz_name
        if window and kind == "CronTriggerTimetable":
            out["interval_seconds"] = window  # the run's data window (``interval=``)
        if immediately:
            out["run_immediately"] = True
        return out
    if kind in ("DeltaTriggerTimetable", "DeltaDataIntervalTimetable"):
        delta: Any = getattr(tt, "_delta", None)
        seconds = int(delta.total_seconds()) if hasattr(delta, "total_seconds") else 3600
        return {
            "type": "interval",
            "seconds": seconds,
            "data_interval": kind == "DeltaDataIntervalTimetable",
        }
    if kind == "MultipleCronTriggerTimetable":
        exprs = [getattr(t, "_expression", None) for t in getattr(tt, "_timetables", [])]
        return {"type": "multi_cron", "expressions": [e for e in exprs if e]}
    if kind == "AssetTriggeredTimetable":
        return {"type": "assets", "expression": _asset_expr(tt.asset_condition, warnings)}
    warnings.append(f"timetable {kind} is not supported; imported without schedule")
    return {"type": "none"}


def _run_immediately(tt: Any, warnings: list[str]) -> bool:
    """``CronTriggerTimetable(run_immediately=...)``; the Task SDK keeps it as
    ``_run_immediately`` in some versions."""
    value: Any = getattr(tt, "run_immediately", None)
    if value is None:
        value = getattr(tt, "_run_immediately", False)
    if hasattr(value, "total_seconds"):
        warnings.append(
            "schedule: run_immediately with a time window is imported as run_immediately=True"
        )
        return True
    return bool(value)


def _interval_seconds(tt: Any) -> float:
    interval: Any = getattr(tt, "interval", None) or getattr(tt, "_interval", None)
    return float(interval.total_seconds()) if hasattr(interval, "total_seconds") else 0.0


def _group_chain(task: Any) -> list[Any]:
    chain = []
    tg = getattr(task, "task_group", None)
    while tg is not None and getattr(tg, "group_id", None):
        chain.append(tg)
        tg = getattr(tg, "parent_group", None)
    return chain


_DECORATORS = {
    "_PythonDecoratedOperator": "task",
    "_BranchPythonDecoratedOperator": "branch",
    "_ShortCircuitDecoratedOperator": "short_circuit",
    "_PythonVirtualenvDecoratedOperator": "virtualenv",
    "_PythonExternalDecoratedOperator": "external_python",
    "_BranchPythonVirtualenvDecoratedOperator": "branch_virtualenv",
    "_BranchExternalPythonDecoratedOperator": "branch_external_python",
    "DecoratedSensorOperator": "sensor",
    "_BashDecoratedOperator": "bash",
    "_DockerDecoratedOperator": "docker",
    "_KubernetesDecoratedOperator": "kubernetes",
}
_COMMON = (
    "retries",
    "pool",
    "pool_slots",
    "queue",
    "owner",
    "doc_md",
    "priority_weight",
    "weight_rule",
    "task_display_name",
    "max_active_tis_per_dagrun",
    "depends_on_past",
)


def _function_body(fn: Any, warnings: list[str], task_id: str) -> tuple[str, list[str]]:
    import ast as _ast
    import inspect as _inspect
    import textwrap

    try:
        src = textwrap.dedent(_inspect.getsource(fn))
    except (OSError, TypeError):
        warnings.append(f"{task_id}: source of the python callable is unavailable")
        return "pass", []
    tree = _ast.parse(src)
    func = next(n for n in tree.body if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef)))
    args = func.args
    positional = [*args.posonlyargs, *args.args]
    # The generated function takes every parameter by keyword (its call binds them by
    # name), so positional-only and keyword-only parameters become plain ones.
    params = [a.arg for a in positional] + [a.arg for a in args.kwonlyargs]
    fn.__autoair_positional__ = [a.arg for a in positional]
    # remember literal defaults so they can become bound arguments
    fn.__autoair_defaults__ = {}
    pairs = list(
        zip(positional[len(positional) - len(args.defaults) :], args.defaults, strict=True)
    )
    pairs += [
        (a, d) for a, d in zip(args.kwonlyargs, args.kw_defaults, strict=True) if d is not None
    ]
    for arg, default in pairs:
        try:
            fn.__autoair_defaults__[arg.arg] = _ast.literal_eval(default)
        except ValueError:
            warnings.append(
                f"{task_id}: default of parameter {arg.arg!r} is not a literal and is not "
                "carried over; set the argument on the task"
            )
    for star, kind in ((args.vararg, "*"), (args.kwarg, "**")):
        if star is not None:
            warnings.append(
                f"{task_id}: parameter {kind}{star.arg} is not supported; values passed "
                "through it are not imported"
            )
    lines = src.splitlines()
    first = func.body[0]
    has_doc = isinstance(first, _ast.Expr) and isinstance(
        getattr(first, "value", None), _ast.Constant
    )
    start = first.lineno - 1  # the docstring, when there is one, stays first
    body = textwrap.dedent("\n".join(lines[start : func.end_lineno]))
    # Names the body reads from the DAG file's imports (``import math`` at the top) are
    # imported inside the function: the generated module does not have the file's imports.
    try:
        source_file = _inspect.getsourcefile(fn)
    except TypeError:
        source_file = None
    imports, unresolved = _module_imports_for(func, source_file)
    if imports:
        if has_doc:
            doc_end = first.end_lineno - first.lineno + 1  # type: ignore[operator]
            doc, rest = body.splitlines()[:doc_end], body.splitlines()[doc_end:]
            body = "\n".join([*doc, *imports, *rest])
        else:
            body = "\n".join([*imports, body])
    if unresolved:
        warnings.append(
            f"{task_id}: the function uses names defined elsewhere in the DAG file "
            f"({', '.join(unresolved)}); they are not carried into the imported task"
        )
    return body, params


def _same_file(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    try:
        return Path(a).resolve() == Path(b).resolve()
    except (OSError, ValueError):
        return False


def _module_imports_for(func: Any, source_file: str | None) -> tuple[list[str], list[str]]:
    """``(imports, unresolved)`` for the names ``func`` (an ``ast`` function) reads from its
    module: the file's import statements that bind them (only the names used), and the
    free names no import binds (constants, helpers, enclosing-function variables)."""
    import ast as _ast
    import builtins

    # The body and the signature only: decorators (``@task``) are not part of the
    # imported function.
    scope = [n for top in [*func.body, func.args] for n in _ast.walk(top)]
    read = {n.id for n in scope if isinstance(n, _ast.Name)}
    bound: set[str] = set()
    for n in scope:
        if isinstance(n, _ast.arg):
            bound.add(n.arg)
        elif isinstance(n, _ast.Name) and isinstance(n.ctx, _ast.Store):
            bound.add(n.id)
        elif isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef, _ast.ClassDef)):
            bound.add(n.name)
        elif isinstance(n, (_ast.Import, _ast.ImportFrom)):
            bound |= {(a.asname or a.name).split(".")[0] for a in n.names}
    free = read - bound - set(dir(builtins)) - {func.name}
    imports: list[str] = []
    try:
        module_tree = _ast.parse(Path(source_file or "").read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError):
        module_tree = _ast.Module(body=[], type_ignores=[])
    imported: set[str] = set()
    for stmt in module_tree.body:
        if isinstance(stmt, _ast.ImportFrom) and stmt.module == "__future__":
            continue
        if isinstance(stmt, (_ast.Import, _ast.ImportFrom)):
            aliases = [a for a in stmt.names if (a.asname or a.name).split(".")[0] in free]
            if not aliases:
                continue
            imported |= {(a.asname or a.name).split(".")[0] for a in aliases}
            kept: _ast.stmt
            if isinstance(stmt, _ast.Import):
                kept = _ast.Import(names=aliases)
            else:
                kept = _ast.ImportFrom(module=stmt.module, names=aliases, level=stmt.level)
            imports.append(_ast.unparse(kept))
    # Sorted: the value must not depend on the order the imports have in the file (generated
    # code orders them differently from the hand-written file).
    return sorted(set(imports)), sorted(free - imported)


def _callable_value(
    fn: Any, dag_file: str | None, warnings: list[str], task_id: str, name: str
) -> dict[str, Any] | None:
    """A callable operator argument (``python_callable=...``) as a spec value: a function
    defined in the DAG file becomes an inline function (its source, decorators removed, plus
    the file's imports it uses), one imported from another module an import reference.
    None (with a warning) when neither can represent it."""
    import ast as _ast
    import inspect as _inspect
    import re
    import textwrap

    from autoair.spec.models import PYTHON_PATH_PATTERN

    what = f"{task_id}: {name}"
    fn_name = getattr(fn, "__name__", None)
    qualname = getattr(fn, "__qualname__", None) or ""
    if not _inspect.isfunction(fn):
        warnings.append(
            f"{what} is a {type(fn).__name__}, not a plain function; it was not imported "
            "(set it again in the builder)"
        )
        return None
    if fn_name == "<lambda>":
        warnings.append(
            f"{what} is a lambda and was not imported; define it with `def` in the DAG file "
            "or set it again in the builder"
        )
        return None
    try:
        source_file = _inspect.getsourcefile(fn)
    except TypeError:
        source_file = None
    local = _same_file(source_file, dag_file)
    if not local:
        module = getattr(fn, "__module__", None) or ""
        path = f"{module}.{qualname}"
        if (
            "<" not in qualname
            and re.match(PYTHON_PATH_PATTERN, path)
            and (
                not module.startswith("unusual_prefix_")  # a DAG file loaded by the DagBag
            )
        ):
            return {"$type": "import", "path": path}
        warnings.append(
            f"{what} ({qualname or fn_name}) cannot be imported: it is neither defined in the "
            "DAG file nor importable by name (set it again in the builder)"
        )
        return None
    try:
        src = textwrap.dedent(_inspect.getsource(fn))
        tree = _ast.parse(src)
    except (OSError, TypeError, SyntaxError):
        warnings.append(f"{what}: the source of {fn_name} is unavailable; it was not imported")
        return None
    func = next(
        (n for n in tree.body if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef))), None
    )
    if func is None:
        warnings.append(f"{what}: the source of {fn_name} is unavailable; it was not imported")
        return None
    # ``func.lineno`` is the ``def`` line (decorators start above it): decorators are dropped,
    # the function is passed as written.
    lines = src.splitlines()
    body = "\n".join(lines[func.lineno - 1 : func.end_lineno])
    imports, unresolved = _module_imports_for(func, source_file)
    if unresolved:
        warnings.append(
            f"{what}: {fn_name} uses names defined elsewhere in the DAG file "
            f"({', '.join(unresolved)}); they are not carried into the imported function"
        )
    if getattr(fn, "__closure__", None):
        warnings.append(
            f"{what}: {fn_name} uses variables of the function it is defined in; the "
            "imported function is defined at module level"
        )
    source = "\n".join(imports) + ("\n\n\n" if imports else "") + body + "\n"
    return {"$type": "function", "source": source}


def _with_xcoms(value: Any, ids: dict[str, str]) -> tuple[bool, Any]:
    """``_jsonable`` that also converts XCom references nested in lists and dicts
    (``env={"ROWS": t.output}``)."""
    if type(value).__name__ in ("PlainXComArg", "XComArg"):
        op: Any = getattr(value, "operator", None)
        key = getattr(value, "key", None)
        ref: dict[str, Any] = {"$type": "xcom", "node": ids.get(op.task_id, op.task_id)}
        if key and key != "return_value":
            ref["key"] = key
        return True, ref
    if isinstance(value, (list, tuple)):
        items = [_with_xcoms(v, ids) for v in value]
        return all(ok for ok, _ in items), [v for _, v in items]
    if isinstance(value, dict) and all(isinstance(k, str) for k in value):
        pairs = {k: _with_xcoms(v, ids) for k, v in value.items()}
        return all(ok for ok, _ in pairs.values()), {k: v for k, (_, v) in pairs.items()}
    return _jsonable(value)


def _xcom_or_value(value: Any, ids: dict[str, str], warnings: list[str], task_id: str) -> Any:
    ok, converted = _with_xcoms(value, ids)
    if not ok:
        warnings.append(f"{task_id}: value of type {type(value).__name__} dropped")
        return None
    return converted


def _task_class_name(t: Any) -> str:
    """Class name of the operator behind ``t`` (also for a ``MappedOperator``)."""
    op_cls = getattr(t, "operator_class", None)
    if isinstance(op_cls, type):
        return op_cls.__name__
    return getattr(t, "_task_type", None) or getattr(t, "task_type", None) or type(t).__name__


def _mapping(t: Any, ids: dict[str, str], warnings: list[str]) -> dict[str, Any] | None:
    """The spec ``mapping`` of a mapped task (``.partial(...).expand(...)`` /
    ``.expand_kwargs(...)``), or None with a warning when it cannot be represented."""
    expand_input = getattr(t, "op_kwargs_expand_input", None) or getattr(t, "expand_input", None)
    kind = type(expand_input).__name__
    value = getattr(expand_input, "value", None)
    out: dict[str, Any] = {}
    if kind == "DictOfListsExpandInput" and isinstance(value, dict):
        expand: dict[str, Any] = {}
        for name, arg in value.items():
            converted = _xcom_or_value(arg, ids, warnings, t.task_id)
            if converted is None:
                warnings.append(
                    f"{t.task_id}: mapped argument {name!r} cannot be imported; the task is "
                    "imported without mapping (re-create the mapping in the builder)"
                )
                return None
            expand[name] = converted
        if not expand:
            return None
        out["expand"] = expand
    elif kind == "ListOfDictsExpandInput":
        converted = _xcom_or_value(value, ids, warnings, t.task_id)
        if converted is None:
            warnings.append(
                f"{t.task_id}: expand_kwargs input cannot be imported; the task is imported "
                "without mapping (re-create the mapping in the builder)"
            )
            return None
        out["expand_kwargs"] = converted
    else:
        warnings.append(
            f"{t.task_id}: mapped task imported without mapping ({kind or 'unknown'} "
            "expand input is not supported; re-create the mapping in the builder)"
        )
        return None
    template = (getattr(t, "partial_kwargs", {}) or {}).get("map_index_template")
    if isinstance(template, str) and template:
        out["map_index_template"] = template
    return out


def _same(a: Any, b: Any) -> bool:
    try:
        return bool(a is b or a == b)
    except Exception:
        return False


def _edge_label(dag: Any, upstream: Any, downstream: Any) -> str | None:
    """The ``Label`` of an edge (``a >> Label("x") >> b``), kept in ``dag.edge_info``. An
    edge from or to a task group is recorded under the group's join id
    (``<group>.downstream_join_id`` / ``<group>.upstream_join_id``)."""
    sources = [upstream.task_id] + [
        getattr(g, "downstream_join_id", None) for g in _group_chain(upstream)
    ]
    targets = [downstream.task_id] + [
        getattr(g, "upstream_join_id", None) for g in _group_chain(downstream)
    ]
    for src in sources:
        for dst in targets:
            if not src or not dst:
                continue
            try:
                info = dag.get_edge_info(src, dst)
            except (AttributeError, KeyError, TypeError):
                return None
            label = info.get("label") if isinstance(info, dict) else None
            if isinstance(label, str) and label:
                return label[:200]
    return None


def _passed_kwargs(t: Any, is_mapped: bool) -> dict[str, Any] | None:
    """The constructor arguments ``t`` was created with (``partial()`` kwargs for a mapped
    task): what the code passes plus values merged in from ``default_args``. None when the
    operator does not record them."""
    if is_mapped:
        return dict(getattr(t, "partial_kwargs", {}) or {})
    passed = getattr(t, "_BaseOperator__init_kwargs", None)
    return dict(passed) if isinstance(passed, dict) else None


def _seconds_of(value: Any, what: str, warnings: list[str], *, positive: bool) -> float | None:
    """A duration argument (``timedelta`` or seconds) as seconds for the spec, or None with a
    warning when the generated ``datetime.timedelta(seconds=...)`` could not hold it."""
    import math

    from autoair.codegen.context import MAX_TIMEDELTA_SECONDS

    if hasattr(value, "total_seconds"):
        seconds = float(value.total_seconds())
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value)
    else:
        warnings.append(f"{what}: value of type {type(value).__name__} dropped")
        return None
    if not math.isfinite(seconds) or seconds < 0 or (positive and seconds == 0):
        warnings.append(f"{what}: duration {value!r} is not a valid timeout/delay; not imported")
        return None
    if seconds > MAX_TIMEDELTA_SECONDS:
        # timedelta.max as a float rounds up past the limit: keep whole seconds.
        seconds = float(min(math.floor(seconds), MAX_TIMEDELTA_SECONDS))
    return seconds


# BaseOperator arguments the spec has no field for (anything else is imported or structural).
_UNSUPPORTED_TASK_ARGS = (
    "email",
    "email_on_retry",
    "email_on_failure",
    "max_retry_delay",
    "do_xcom_push",
    "wait_for_downstream",
    "wait_for_past_depends_before_skipping",
    "ignore_first_depends_on_past",
    "start_date",
    "end_date",
    "run_as_user",
    "resources",
    "sla",
    "max_active_tis_per_dag",
    "params",
    "doc",
    "doc_json",
    "doc_yaml",
    "doc_rst",
    "pre_execute",
    "post_execute",
    "render_template_as_native_obj",
    "logger_name",
    "allow_nested_operators",
    "retry_policy",
)
_TASK_CALLBACKS = (
    "on_execute_callback",
    "on_failure_callback",
    "on_retry_callback",
    "on_skipped_callback",
    "on_success_callback",
)


def _task_settings(
    t: Any, dag: Any, passed: dict[str, Any] | None, warnings: list[str]
) -> dict[str, Any]:
    """Task-level BaseOperator settings of ``t`` as spec node fields.

    With recorded constructor arguments an argument counts as written when it was passed and
    differs from the DAG's ``default_args`` (``retries=0`` against ``default_args`` retries=3
    is an override); without them, an attribute differing from the default."""
    import datetime

    from airflow.configuration import conf as _conf

    da = dict(getattr(dag, "default_args", None) or {})
    missing = object()
    retry_default = _conf.getfloat("core", "default_task_retry_delay", fallback=300.0)
    # Airflow's own defaults: a value equal to one is not an argument the code wrote, unless
    # it overrides default_args. (A mapped task's partial() kwargs list every default.)
    airflow_defaults: dict[str, Any] = {
        "pool": "default_pool",
        "queue": "default",
        "owner": "airflow",
        "priority_weight": 1,
        "retries": 0,
        "pool_slots": 1,
        "depends_on_past": False,
        "task_display_name": t.task_id,
        "retry_delay": datetime.timedelta(seconds=retry_default),
        "do_xcom_push": True,
        "email_on_retry": True,
        "email_on_failure": True,
        "wait_for_downstream": False,
        "wait_for_past_depends_before_skipping": False,
        "ignore_first_depends_on_past": False,
        "allow_nested_operators": True,
    }
    dag_params = getattr(dag, "params", None)
    with contextlib.suppress(KeyError, TypeError, ValueError):
        airflow_defaults["params"] = {k: dag_params[k] for k in dag_params} if dag_params else {}

    def written(attr: str) -> tuple[bool, Any]:
        if passed is not None:
            if attr not in passed:
                return False, None
            val = passed[attr]
        else:
            val = getattr(t, attr, None)
        if attr in da:
            return not _same(val, da[attr]), val
        return val is not None and not _same(val, airflow_defaults.get(attr, missing)), val

    out: dict[str, Any] = {}
    for attr in _COMMON:
        val = getattr(t, attr, None)
        if attr in da:
            # An explicit value equal to Airflow's default still overrides default_args.
            if val is None or _same(val, da[attr]):
                continue
        else:
            if val is None or _same(val, airflow_defaults.get(attr, missing)):
                continue
            if attr == "weight_rule" and str(val).lower().endswith("downstream"):
                continue
        ok, conv = _jsonable(val)
        if ok:
            out[attr] = str(conv) if attr == "weight_rule" else conv
    for attr, field in (
        ("retry_delay", "retry_delay_seconds"),
        ("execution_timeout", "execution_timeout_seconds"),
    ):
        is_written, val = written(attr)
        if not is_written or val is None:
            continue
        seconds = _seconds_of(
            val, f"{t.task_id}: {attr}", warnings, positive=attr == "execution_timeout"
        )
        if seconds is not None:
            out[field] = seconds
    is_written, val = written("executor")
    if is_written and isinstance(val, str) and val:
        out["executor"] = val
    is_written, val = written("executor_config")
    if is_written and val:
        ok, conv = _jsonable(val)
        if ok and isinstance(conv, dict):
            out["executor_config"] = conv
        else:
            warnings.append(f"{t.task_id}: executor_config is not plain data; not imported")
    is_written, val = written("retry_exponential_backoff")
    if is_written and val:
        if isinstance(val, bool):
            # Before Airflow 3.2 the flag means backoff with base 2; from 3.2 the argument
            # is the multiplier itself (True == 1).
            from autoair._compat import airflow_version

            out["retry_exponential_backoff"] = (
                2.0 if (airflow_version() or (3, 2, 0)) < (3, 2, 0) else 1.0
            )
        elif isinstance(val, (int, float)):
            out["retry_exponential_backoff"] = float(val)
    inlets = [_asset(i) for i in getattr(t, "inlets", []) or [] if type(i).__name__ == "Asset"]
    if inlets:
        out["inlets"] = inlets
    if passed is not None:
        for attr in _UNSUPPORTED_TASK_ARGS:
            is_written, val = written(attr)
            if is_written and val not in (None, {}, [], ""):
                warnings.append(f"{t.task_id}: argument {attr} is not imported")
        if any(written(cb)[0] and written(cb)[1] for cb in _TASK_CALLBACKS):
            warnings.append(f"{t.task_id}: task callbacks are not imported")
    return out


def _operator_params(
    t: Any,
    op_cls: type | None,
    dag: Any,
    base_params: set[str],
    ids: dict[str, str],
    warnings: list[str],
) -> dict[str, Any]:
    """Operator-specific constructor arguments of ``t``, as written in the code.

    Only arguments the code passes are imported: the operator's recorded ``__init__``
    kwargs (``partial()`` kwargs for a mapped task), so normalised attributes and
    defaults (``skip_on_exit_code=99`` stored as ``[99]``) never become explicit
    parameters. Values that only come from ``default_args`` are skipped too.
    """
    import inspect as _inspect

    from airflow.sdk import BaseOperator

    is_mapped = op_cls is not type(t)
    expanded: set[str] = set()
    if is_mapped:
        value = getattr(getattr(t, "expand_input", None), "value", None)
        if isinstance(value, dict):
            expanded = set(value)
    passed = _passed_kwargs(t, is_mapped)
    dag_defaults = dict(getattr(dag, "default_args", None) or {})
    names: dict[str, Any] = {}  # name -> signature default
    for klass in op_cls.__mro__ if op_cls is not None else ():
        if klass is BaseOperator or not isinstance(klass, type):
            break
        if not issubclass(klass, BaseOperator):
            # A mixin listed before the operator base (``class Op(Mixin, BashOperator)``):
            # the operator classes after it still define constructor arguments.
            continue
        init = klass.__dict__.get("__init__")
        if init is None:
            continue
        for name, p in _inspect.signature(init).parameters.items():
            if name in base_params or name in names or name in ("self", "kwargs", "args"):
                continue
            names[name] = p.default
    if op_cls is None and passed is not None:
        names = {k: _inspect.Parameter.empty for k in passed if k not in base_params}
    op_params: dict[str, Any] = {}
    for name, default in names.items():
        if name in expanded:
            continue
        if passed is not None:
            if name not in passed:
                continue
            val = passed[name]
            if name in dag_defaults and _same(val, dag_defaults[name]):
                continue  # merged in from default_args, not written on the task
        else:  # no recorded kwargs (older Airflow): attribute differing from the default
            if not hasattr(t, name):
                continue
            val = getattr(t, name)
            if default is not _inspect.Parameter.empty and val == default:
                continue
        if callable(val) and not isinstance(val, type):
            ref = _callable_value(val, getattr(dag, "fileloc", None), warnings, t.task_id, name)
            if ref is not None:
                op_params[name] = ref
            continue
        converted = _xcom_or_value(val, ids, warnings, t.task_id)
        if converted is not None:
            op_params[name] = converted
    return op_params


# JSON Schema keyword of a Param -> DagParam field.
_PARAM_SCHEMA_KEYS = {
    "type": "type",
    "title": "title",
    "description": "description",
    "description_md": "description_md",
    "section": "section",
    "format": "format",
    "enum": "enum",
    "values_display": "values_display",
    "examples": "examples",
    "minimum": "minimum",
    "maximum": "maximum",
    "minLength": "min_length",
    "maxLength": "max_length",
    "pattern": "pattern",
    "items": "items",
    "const": "const",
}
# DAG arguments imported only when they differ from Airflow's default.
_DAG_DEFAULTS: dict[str, Any] = {
    "fail_fast": False,
    "render_template_as_native_obj": False,
}


def _convert_dag(dag: Any, warnings: list[str]) -> dict[str, Any]:
    import inspect as _inspect

    from airflow.sdk import BaseOperator

    base_params = set(_inspect.signature(BaseOperator.__init__).parameters)
    ids: dict[str, str] = {}
    nodes: list[dict[str, Any]] = []
    groups: dict[str, str] = {}

    import hashlib

    def node_id(prefix: str, key: str) -> str:
        return f"{prefix}_{hashlib.sha1(key.encode()).hexdigest()[:8]}"  # noqa: S324 - ids only

    from autoair.importer.blueprints import steps_of

    # Astronomer Blueprint steps: their tasks and groups fold into one node each.
    steps = steps_of(dag, warnings)
    folded: dict[str, str] = {}
    step_groups: set[str] = set()
    for step in steps:
        step.node["id"] = node_id("b", min(step.tasks))
        folded.update(dict.fromkeys(step.tasks, step.node["id"]))
        step_groups |= step.groups

    tasks = list(dag.tasks)
    for t in tasks:
        ids[t.task_id] = folded.get(t.task_id) or node_id("t", t.task_id)

    def group_chain(start: Any) -> list[Any]:
        chain = []
        while start is not None and getattr(start, "group_id", None):
            chain.append(start)
            start = getattr(start, "parent_group", None)
        return chain

    chains = [_group_chain(t) for t in tasks if t.task_id not in folded]
    chains += [group_chain(step.parent) for step in steps]
    for chain in chains:
        for tg in reversed(chain):
            if tg.group_id in groups or tg.group_id in step_groups:
                continue
            gid = node_id("g", tg.group_id)
            groups[tg.group_id] = gid
            parent = getattr(tg, "parent_group", None)
            entry: dict[str, Any] = {
                "kind": "task_group",
                "id": gid,
                "group_id": tg.group_id.rsplit(".", 1)[-1] if tg.prefix_group_id else tg.group_id,
                "prefix_group_id": bool(tg.prefix_group_id),
            }
            if tg.tooltip:
                entry["tooltip"] = tg.tooltip
            if parent is not None and getattr(parent, "group_id", None):
                entry["parent_group"] = groups.get(parent.group_id)
            nodes.append(entry)

    for step in steps:
        parent_id = getattr(step.parent, "group_id", None)
        if parent_id and parent_id in groups:
            step.node["parent_group"] = groups[parent_id]
        nodes.append(step.node)

    for t in tasks:
        if t.task_id in folded:
            continue
        cls = type(t)
        chain = _group_chain(t)
        own_id = t.task_id
        if chain and chain[0].prefix_group_id and own_id.startswith(chain[0].group_id + "."):
            own_id = own_id[len(chain[0].group_id) + 1 :]
        node: dict[str, Any] = {"id": ids[t.task_id], "task_id": own_id}
        if chain:
            node["parent_group"] = groups[chain[0].group_id]
        is_mapped = bool(getattr(t, "is_mapped", False)) or type(t).__name__.endswith(
            "MappedOperator"
        )
        real_cls_name = _task_class_name(t)
        decorator = _DECORATORS.get(real_cls_name)
        mapping = _mapping(t, ids, warnings) if is_mapped else None
        if decorator:
            body, params = _function_body(t.python_callable, warnings, t.task_id)
            if is_mapped:
                partial = getattr(t, "partial_kwargs", {}) or {}
                op_kwargs = dict(partial.get("op_kwargs") or {})
                op_args = list(partial.get("op_args") or ())
            else:
                op_kwargs = dict(getattr(t, "op_kwargs", {}) or {})
                op_args = list(getattr(t, "op_args", ()) or ())
            # Positional call arguments (``multiply(7)``) bind to the parameters in order;
            # the spec stores every argument by name.
            positional = getattr(t.python_callable, "__autoair_positional__", [])
            for name, value in zip(positional, op_args, strict=False):
                if name in op_kwargs:
                    warnings.append(f"{t.task_id}: argument {name!r} is passed twice")
                op_kwargs.setdefault(name, value)
            if len(op_args) > len(positional):
                warnings.append(
                    f"{t.task_id}: {len(op_args) - len(positional)} positional argument(s) "
                    "beyond the named parameters (*args) are not imported"
                )
            expanded = set(mapping["expand"]) if mapping and "expand" in mapping else set()
            for k, v in getattr(t.python_callable, "__autoair_defaults__", {}).items():
                if k not in expanded:
                    op_kwargs.setdefault(k, v)
            node.update(
                {
                    "kind": "python_task",
                    "decorator": decorator,
                    "function_name": t.python_callable.__name__,
                    "code": body,
                    "arguments": {
                        k: _xcom_or_value(v, ids, warnings, t.task_id) for k, v in op_kwargs.items()
                    },
                    "extra_parameters": [
                        p for p in params if p not in op_kwargs and p not in expanded
                    ],
                    "multiple_outputs": bool(getattr(t, "multiple_outputs", False)),
                }
            )
        else:
            module = getattr(t, "_task_module", None) or cls.__module__
            op_cls = getattr(t, "operator_class", None) if is_mapped else cls
            if not isinstance(op_cls, type):
                op_cls = None  # deserialized mapped operator: no class to inspect
            node["kind"] = "operator"
            node["operator"] = f"{module}.{real_cls_name}"
            if module.startswith("unusual_prefix_"):  # the DagBag's name for the DAG file
                warnings.append(
                    f"{t.task_id}: operator class {real_cls_name} is defined in the DAG file; "
                    "move it to an importable module (the generated DAG cannot import it)"
                )
            node["params"] = _operator_params(t, op_cls, dag, base_params, ids, warnings)
        if mapping is not None:
            node["mapping"] = mapping
        # A MappedOperator exposes the same task attributes (from partial()).
        node.update(_task_settings(t, dag, _passed_kwargs(t, is_mapped), warnings))
        tr = str(getattr(t, "trigger_rule", "all_success")).split(".")[-1].lower()
        teardown_rule = getattr(t, "is_teardown", False) and tr == "all_done_setup_success"
        if tr != "all_success" and not teardown_rule:  # as_teardown() sets the latter
            node["trigger_rule"] = tr
        if getattr(t, "is_setup", False):
            node["is_setup"] = True
        if getattr(t, "is_teardown", False):
            node["is_teardown"] = True
        outlets = [
            _asset(o) for o in getattr(t, "outlets", []) or [] if type(o).__name__ == "Asset"
        ]
        if outlets:
            node["outlets"] = outlets
        nodes.append(node)

    edges = []
    seen_edges: set[tuple[str, str]] = set()
    for t in tasks:
        for up in sorted(t.upstream_task_ids):
            pair = (ids[up], ids[t.task_id])
            if pair[0] == pair[1] or pair in seen_edges:
                continue  # inside one blueprint step, or a step's many roots/leaves
            seen_edges.add(pair)
            edge: dict[str, Any] = {"source": pair[0], "target": pair[1]}
            label = _edge_label(dag, dag.get_task(up), t)
            if label:
                edge["label"] = label
            edges.append(edge)

    spec: dict[str, Any] = {
        "dag_id": dag.dag_id,
        "output_mode": "python",
        "schedule": _schedule(dag, warnings),
        "catchup": bool(dag.catchup),
        "tags": sorted(dag.tags or []),
        "nodes": nodes,
        "edges": edges,
    }
    from airflow.configuration import conf as _conf

    conf_defaults = {
        "max_active_runs": _conf.getint("core", "max_active_runs_per_dag", fallback=16),
        "max_active_tasks": _conf.getint("core", "max_active_tasks_per_dag", fallback=16),
    }
    for attr in ("description", "doc_md", "max_active_runs", "max_active_tasks"):
        val = getattr(dag, attr, None)
        if val not in (None, "") and val != conf_defaults.get(attr):
            spec[attr] = val
    for attr in (
        "is_paused_upon_creation",
        "fail_fast",
        "max_consecutive_failed_dag_runs",
        "render_template_as_native_obj",
    ):
        val = getattr(dag, attr, None)
        if attr == "max_consecutive_failed_dag_runs":
            default: Any = _conf.getint(
                "core", "max_consecutive_failed_dag_runs_per_dag", fallback=0
            )
        else:
            default = _DAG_DEFAULTS.get(attr)
        if isinstance(val, (bool, int)) and val != default:
            spec[attr] = val
    # Airflow 3.3+: None means "follow [core] rerun_with_latest_version"; only an explicit
    # choice is imported (older Airflow has no such attribute).
    rerun = getattr(dag, "rerun_with_latest_version", None)
    if isinstance(rerun, bool):
        spec["rerun_with_latest_version"] = rerun
    owner_links = getattr(dag, "owner_links", None)
    if isinstance(owner_links, dict) and owner_links:
        spec["owner_links"] = {str(k): str(v) for k, v in owner_links.items()}
    if getattr(dag, "dagrun_timeout", None) is not None:
        seconds = _seconds_of(dag.dagrun_timeout, "dagrun_timeout", warnings, positive=True)
        if seconds is not None:
            spec["dagrun_timeout_seconds"] = seconds
    # DAG timezone: from start_date's tzinfo (Airflow's own rule); dates become wall clock.
    tz = getattr(getattr(dag, "timezone", None), "name", None)
    if tz and tz not in ("UTC", "Etc/UTC"):
        spec["timezone"] = tz
    for attr in ("start_date", "end_date"):
        value = getattr(dag, attr, None)
        if value is None:
            continue
        # Airflow keeps dates in UTC: back to the DAG timezone's wall clock.
        if spec.get("timezone"):
            value = value.astimezone(dag.timezone).replace(tzinfo=None)
        spec[attr] = value.isoformat()
    if getattr(dag, "dag_display_name", dag.dag_id) != dag.dag_id:
        spec["display_name"] = dag.dag_display_name
    dag_params: dict[str, Any] = {}
    for name, param in (dag.params or {}).items() if hasattr(dag.params, "items") else []:
        raw = dag.params.get_param(name) if hasattr(dag.params, "get_param") else param
        default = getattr(raw, "value", raw)
        ok, default = _jsonable(default)
        if not ok:
            warnings.append(f"param {name}: default of type {type(default).__name__} dropped")
        schema = dict(getattr(raw, "schema", {}) or {})
        # Param(description=...) is kept on the Param, not in its schema.
        if getattr(raw, "description", None) and "description" not in schema:
            schema["description"] = raw.description
        param_entry: dict[str, Any] = {"default": default if ok else None}
        for key, field in _PARAM_SCHEMA_KEYS.items():
            if key in schema:
                param_entry[field] = schema[key]
        dag_params[name] = param_entry
    if dag_params:
        spec["params"] = dag_params
    da = dag.default_args or {}
    default_args: dict[str, Any] = {}
    for key in (
        "owner",
        "retries",
        "depends_on_past",
        "email",
        "email_on_failure",
        "pool",
        "queue",
    ):
        if key in da:
            default_args[key] = da[key]
    for key, field in (
        ("retry_delay", "retry_delay_seconds"),
        ("execution_timeout", "execution_timeout_seconds"),
    ):
        if da.get(key) is not None:
            seconds = _seconds_of(
                da[key], f"default_args {key}", warnings, positive=key == "execution_timeout"
            )
            if seconds is not None:
                default_args[field] = seconds
    if default_args:
        spec["default_args"] = default_args
    if getattr(dag, "deadline", None):
        warnings.append("deadline alerts are not imported")
    if getattr(dag, "on_failure_callback", None) or getattr(dag, "on_success_callback", None):
        warnings.append("DAG callbacks are not imported")
    return spec
