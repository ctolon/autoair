"""Recognise Astronomer Blueprint steps while importing a Python DAG.

The importer runs the DAG file in a child process. Before that, :func:`record_renders`
wraps ``render()`` of every ``Blueprint`` subclass defined afterwards, so each call is
remembered with its instance's ``step_id``, its config and what it returned. The tasks a
step created are then folded into one ``blueprint`` node instead of being imported as
plain operators and task groups.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Any

# Calls of Blueprint.render() in this (child) process, outermost calls only.
RENDERS: list[dict[str, Any]] = []
_DEPTH = [0]
_DAGBAG_PREFIX = "unusual_prefix_"


def record_renders() -> bool:  # pragma: no cover - executed in a subprocess
    """Install the ``render()`` recorder; False when airflow-blueprint is not installed."""
    try:
        from blueprint import Blueprint
    except ImportError:
        return False
    original = Blueprint.__dict__["__init_subclass__"]
    if getattr(original, "__autoair__", False):
        return True
    func = original.__func__ if isinstance(original, classmethod) else original

    def init_subclass(cls: Any, /, **kwargs: Any) -> None:
        func(cls, **kwargs)
        render = cls.__dict__.get("render")
        if callable(render) and not getattr(render, "__autoair__", False):
            cls.render = _recording(render)

    hook = classmethod(init_subclass)
    hook.__autoair__ = True  # type: ignore[attr-defined]
    Blueprint.__init_subclass__ = hook  # type: ignore[assignment,method-assign]
    return True


def _recording(render: Any) -> Any:
    def wrapped(self: Any, config: Any, *args: Any, **kwargs: Any) -> Any:
        _DEPTH[0] += 1
        try:
            result = render(self, config, *args, **kwargs)
        finally:
            _DEPTH[0] -= 1
        if _DEPTH[0] == 0:  # a render calling super().render() is one step
            RENDERS.append(
                {
                    "cls": type(self),
                    "step_id": getattr(self, "step_id", None),
                    "config": config,
                    "result": result,
                }
            )
        return result

    wrapped.__autoair__ = True  # type: ignore[attr-defined]
    wrapped.__wrapped__ = render  # type: ignore[attr-defined]
    return wrapped


@dataclass
class Step:
    """One recorded blueprint step inside the imported DAG."""

    node: dict[str, Any]
    tasks: set[str] = field(default_factory=set)  # full task ids folded into the step
    groups: set[str] = field(default_factory=set)  # full group ids inside the step
    parent: Any = None  # TaskGroup the step was rendered in (None: the DAG root)


def _group_ids(group: Any) -> set[str]:
    out = {group.group_id}
    for child in getattr(group, "children", {}).values():
        if hasattr(child, "children"):
            out |= _group_ids(child)
    return out


def _importable(cls: Any) -> bool:
    module = sys.modules.get(cls.__module__)
    return (
        module is not None
        # DagBag loads DAG files as unusual_prefix_<hash>_<name> (no importable module).
        and not cls.__module__.startswith(_DAGBAG_PREFIX)
        and "." not in cls.__qualname__
        and getattr(module, cls.__qualname__, None) is cls
        and getattr(getattr(module, "__spec__", None), "name", None) == cls.__module__
    )


def _config_class(cls: Any, config_type: Any) -> str | None:
    module = sys.modules.get(cls.__module__)
    name = getattr(config_type, "__name__", None)
    return name if module is not None and getattr(module, name or "", None) is config_type else None


def steps_of(dag: Any, warnings: list[str]) -> list[Step]:
    """The recorded steps that belong to ``dag``."""
    steps: list[Step] = []
    for rec in RENDERS:
        result, cls = rec["result"], rec["cls"]
        if hasattr(result, "children"):  # TaskGroup
            members = {
                t.task_id
                for t in dag.tasks
                if any(g is result for g in _chain(t))  # tasks of the group or its subgroups
            }
            if not members:
                continue
            groups, parent = _group_ids(result), getattr(result, "parent_group", None)
        elif dag.task_dict.get(getattr(result, "task_id", None)) is result:
            members, groups, parent = {result.task_id}, set(), getattr(result, "task_group", None)
        else:
            continue
        step_id = rec["step_id"]
        if not _importable(cls):
            # Defined in the DAG file itself (DagBag gives it a mangled module name) or nested:
            # a generated DAG could not import it, so the tasks stay as they are.
            warnings.append(
                f"blueprint step {step_id!r} kept as tasks: {cls.__qualname__} is not "
                "importable from a module (define the template outside the DAG file)"
            )
            continue
        try:
            name, version = cls.parse_name_and_version()
            config_type = cls.get_config_type()
            config = rec["config"].model_dump(mode="json", exclude_unset=True)
        except Exception as exc:
            warnings.append(f"blueprint step {step_id!r} kept as tasks: {exc}")
            continue
        node = {
            "kind": "blueprint",
            "step_id": step_id,
            "blueprint": name,
            "version": version,
            "class_path": f"{cls.__module__}.{cls.__qualname__}",
            "config": config,
        }
        config_class = _config_class(cls, config_type)
        if config_class:
            node["config_class"] = config_class
        steps.append(Step(node=node, tasks=members, groups=groups, parent=parent))
    return steps


def _chain(task: Any) -> list[Any]:
    chain = []
    tg = getattr(task, "task_group", None)
    while tg is not None and getattr(tg, "group_id", None):
        chain.append(tg)
        tg = getattr(tg, "parent_group", None)
    return chain


__all__ = ["RENDERS", "Step", "record_renders", "steps_of"]
