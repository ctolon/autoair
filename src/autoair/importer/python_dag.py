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


def import_python_file(
    path: Path, timeout: float = 90, *, pythonpath: Sequence[Path] = ()
) -> PythonImportResult:
    """``pythonpath``: folders importable by the DAG file (e.g. the root of the repository it
    comes from, like Airflow puts the dags folder on ``sys.path``)."""
    code = "import sys; from autoair.importer.python_dag import _run; _run(sys.argv[1])"
    env = user_code_env()  # the DAG's code runs there: no AutoAir secrets
    if pythonpath:
        extra = os.pathsep.join(str(p) for p in pythonpath)
        env["PYTHONPATH"] = extra + (
            os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
        )
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code, str(path)],
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
    data = json.loads(proc.stdout[marker + len("@@AUTOAIR@@") :])
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


def _run(dag_file: str) -> None:  # pragma: no cover - executed in a subprocess
    import inspect as _inspect

    from autoair._compat import dagbag_class

    _record_cron_presets()
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
    sys.stdout.write("\n@@AUTOAIR@@" + json.dumps(out, default=str))


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


# ``<run_with> <tmp>/airflow-scripts/<dag_id>/<sha256[:16]>/<name> `` (see codegen's
# MATERIALIZE_TEMPLATE): a script written in the builder, materialised at parse time.
_SCRIPT_PATH = (
    r"^(?:(?P<run>[A-Za-z0-9_./-]{1,40}) )?"
    r"(?P<path>\S*[/\\]airflow-scripts[/\\][^/\\]+[/\\](?P<digest>[0-9a-f]{16})[/\\]"
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
    root = Path(tempfile.gettempdir()).resolve() / "airflow-scripts"
    try:
        resolved = path.resolve()
        if not resolved.is_relative_to(root) or not resolved.is_file():
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
        plain = not getattr(tt, "run_immediately", False) and not _interval_seconds(tt)
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
        out = {
            "type": "cron",
            "expression": _resolve_preset(_expression(tt)),
            "data_interval": kind == "CronDataIntervalTimetable",
        }
        if tz_name:
            out["timezone"] = tz_name
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
    params = [a.arg for a in func.args.args]
    # remember literal defaults so they can become bound arguments
    defaults = func.args.defaults
    fn.__autoair_defaults__ = {}
    for arg, default in zip(
        func.args.args[len(func.args.args) - len(defaults) :], defaults, strict=True
    ):
        with contextlib.suppress(ValueError):
            fn.__autoair_defaults__[arg.arg] = _ast.literal_eval(default)
    lines = src.splitlines()
    start = func.body[0].lineno - 1
    if isinstance(func.body[0], _ast.Expr) and isinstance(
        getattr(func.body[0], "value", None), _ast.Constant
    ):
        start = func.body[0].lineno - 1  # keep docstring
    body = textwrap.dedent("\n".join(lines[start : func.end_lineno]))
    return body, params


def _xcom_or_value(value: Any, ids: dict[str, str], warnings: list[str], task_id: str) -> Any:
    kind = type(value).__name__
    if kind in ("PlainXComArg", "XComArg"):
        op: Any = getattr(value, "operator", None)
        key = getattr(value, "key", None)
        ref: dict[str, Any] = {"$type": "xcom", "node": ids.get(op.task_id, op.task_id)}
        if key and key != "return_value":
            ref["key"] = key
        return ref
    ok, converted = _jsonable(value)
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
        passed: dict[str, Any] | None = dict(getattr(t, "partial_kwargs", {}) or {})
        value = getattr(getattr(t, "expand_input", None), "value", None)
        if isinstance(value, dict):
            expanded = set(value)
    else:
        passed = getattr(t, "_BaseOperator__init_kwargs", None)
        passed = dict(passed) if isinstance(passed, dict) else None
    dag_defaults = dict(getattr(dag, "default_args", None) or {})
    names: dict[str, Any] = {}  # name -> signature default
    for klass in op_cls.__mro__ if op_cls is not None else ():
        if klass is BaseOperator or not isinstance(klass, type):
            break
        if not issubclass(klass, BaseOperator):
            break
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
            warnings.append(f"{t.task_id}: callable parameter {name!r} dropped")
            continue
        converted = _xcom_or_value(val, ids, warnings, t.task_id)
        if converted is not None:
            op_params[name] = converted
    return op_params


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

    tasks = list(dag.tasks)
    for t in tasks:
        ids[t.task_id] = node_id("t", t.task_id)
    for t in tasks:
        for tg in reversed(_group_chain(t)):
            if tg.group_id in groups:
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

    for t in tasks:
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
                op_kwargs = dict((getattr(t, "partial_kwargs", {}) or {}).get("op_kwargs") or {})
            else:
                op_kwargs = dict(getattr(t, "op_kwargs", {}) or {})
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
            node["params"] = _operator_params(t, op_cls, dag, base_params, ids, warnings)
        if mapping is not None:
            node["mapping"] = mapping
        # A MappedOperator exposes the same task attributes (from partial()).
        for attr in _COMMON:
            val = getattr(t, attr, None)
            default = dag.default_args.get(attr) if dag.default_args else None
            if val is None or val == default:
                continue
            if attr == "pool" and val == "default_pool":
                continue
            if attr == "queue" and val == "default":
                continue
            if attr == "owner" and val == "airflow":
                continue
            if attr in ("priority_weight",) and val == 1:
                continue
            if attr == "weight_rule" and str(val).lower().endswith("downstream"):
                continue
            if attr == "retries" and val == 0:
                continue
            if attr == "pool_slots" and val == 1:
                continue
            if attr == "depends_on_past" and val is False:
                continue
            if attr == "task_display_name" and val == t.task_id:
                continue
            ok, conv = _jsonable(val)
            if ok:
                node[attr] = str(conv) if attr == "weight_rule" else conv
        tr = str(getattr(t, "trigger_rule", "all_success")).split(".")[-1].lower()
        if tr != "all_success":
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
    for t in tasks:
        for up in sorted(t.upstream_task_ids):
            edges.append({"source": ids[up], "target": ids[t.task_id]})

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
        schema = dict(getattr(raw, "schema", {}) or {})
        param_entry: dict[str, Any] = {"default": default if ok else None}
        for key in (
            "type",
            "title",
            "description",
            "format",
            "enum",
            "minimum",
            "maximum",
            "examples",
            "section",
            "description_md",
            "values_display",
        ):
            if key in schema:
                param_entry[key] = schema[key]
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
        if key in da and hasattr(da[key], "total_seconds"):
            default_args[field] = da[key].total_seconds()
    if default_args:
        spec["default_args"] = default_args
    if getattr(dag, "deadline", None):
        warnings.append("deadline alerts are not imported")
    if getattr(dag, "on_failure_callback", None) or getattr(dag, "on_success_callback", None):
        warnings.append("DAG callbacks are not imported")
    return spec
