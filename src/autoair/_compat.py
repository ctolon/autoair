"""Airflow version detection and import shims (Airflow >= 3.0)."""

from __future__ import annotations

from functools import lru_cache
from typing import Any


@lru_cache(maxsize=1)
def airflow_version() -> tuple[int, int, int] | None:
    """Installed Airflow version, or ``None`` when Airflow is not importable."""
    try:
        from importlib.metadata import version

        raw = version("apache-airflow-core")
    except Exception:
        try:
            from importlib.metadata import version

            raw = version("apache-airflow")
        except Exception:
            return None
    parts: list[int] = []
    for piece in raw.split(".")[:3]:
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits or 0))
    while len(parts) < 3:
        parts.append(0)
    return parts[0], parts[1], parts[2]


def airflow_version_str() -> str | None:
    v = airflow_version()
    return ".".join(map(str, v)) if v else None


def supports_react_apps() -> bool:
    """``react_apps`` / ``external_views`` plugin attributes were added in Airflow 3.1."""
    v = airflow_version()
    return v is not None and v >= (3, 1, 0)


def dagbag_class() -> Any:
    """``DagBag`` moved to ``airflow.dag_processing.dagbag`` in Airflow 3.2."""
    try:
        from airflow.dag_processing.dagbag import DagBag
    except ImportError:
        from airflow.models.dagbag import DagBag
    return DagBag


def base_operator_classes() -> tuple[type, ...]:
    """Operator base classes (in Airflow 3 every operator derives from the Task SDK one)."""
    try:
        from airflow.sdk import BaseOperator
    except ImportError:
        return ()
    return (BaseOperator,)


# Where Task SDK symbols live per Airflow version. Timetables moved into ``airflow.sdk``
# in 3.2; deadline classes appeared in 3.1 under ``airflow.sdk.definitions.deadline``.
# Entries: symbol -> [(min_version, module)], newest first.
_SYMBOL_LOCATIONS: dict[str, list[tuple[tuple[int, int, int], str]]] = {
    **{
        name: [((3, 2, 0), "airflow.sdk"), ((3, 0, 0), module)]
        for name, module in {
            "CronTriggerTimetable": "airflow.timetables.trigger",
            "DeltaTriggerTimetable": "airflow.timetables.trigger",
            "MultipleCronTriggerTimetable": "airflow.timetables.trigger",
            "CronDataIntervalTimetable": "airflow.timetables.interval",
            "DeltaDataIntervalTimetable": "airflow.timetables.interval",
            "EventsTimetable": "airflow.timetables.events",
            "AssetOrTimeSchedule": "airflow.timetables.assets",
        }.items()
    },
    **{
        name: [((3, 2, 0), "airflow.sdk"), ((3, 1, 0), "airflow.sdk.definitions.deadline")]
        for name in ("DeadlineAlert", "DeadlineReference", "AsyncCallback")
    },
    "AssetTriggeredTimetable": [
        ((3, 4, 0), "airflow.sdk"),
        ((3, 0, 0), "airflow.timetables.simple"),
    ],
}

LATEST_KNOWN: tuple[int, int, int] = (3, 4, 0)


def sdk_symbol_module(name: str, version: tuple[int, int, int] | None = None) -> str:
    """Module to import an authoring symbol from for the given (or installed) Airflow."""
    version = version or airflow_version() or LATEST_KNOWN
    for min_version, module in _SYMBOL_LOCATIONS.get(name, []):
        if version >= min_version:
            return module
    return "airflow.sdk"
