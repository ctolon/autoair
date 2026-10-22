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


def supports_plugin_applies_to() -> bool:
    """Airflow 3.4 added ``applies_to`` (UI-side scoping) to ``react_apps`` / ``external_views``.

    Detected on the plugin dict type rather than the version, so a backport is picked up too.
    """
    try:
        from airflow.plugins_manager import ReactAppDict
    except ImportError:
        return False
    keys = getattr(ReactAppDict, "__required_keys__", frozenset()) | getattr(
        ReactAppDict, "__optional_keys__", frozenset()
    )
    return "applies_to" in keys


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


class AirflowConnectionError(LookupError):
    """An Airflow connection could not be read. The message names the connection id and
    the exception types of the failed lookups only, never anything the connection holds."""


def _connection_loaders() -> list[Any]:
    loaders: list[Any] = []
    # Server-side lookup first: environment variables, the configured secrets backend and
    # the metadata database (MetastoreBackend). The Task SDK's BaseHook.get_connection
    # outside a task searches env + secrets backend + the Execution API only, so a
    # connection kept in the metadata DB is "not defined" there (Airflow 3.3).
    try:
        from airflow.models.connection import Connection

        loaders.append(Connection.get_connection_from_secrets)
    except ImportError:
        pass  # no core models in this process (Task SDK only): the SDK loaders below apply
    # Inside a task (or without the core models) the Task SDK is the supported path;
    # airflow.sdk exports BaseHook from 3.1, Airflow 3.0 has it in airflow.hooks.base.
    try:
        from airflow.sdk import BaseHook

        loaders.append(BaseHook.get_connection)
    except ImportError:
        try:
            from airflow.hooks.base import BaseHook as LegacyBaseHook

            loaders.append(LegacyBaseHook.get_connection)
        except ImportError:
            pass  # neither hook location exists: callers get only the loaders found above
    return loaders


def get_airflow_connection(conn_id: str) -> Any:
    """The Airflow connection ``conn_id`` from env vars (``AIRFLOW_CONN_*``), the configured
    secrets backend, the metadata database when this process can reach it, or the Task SDK.

    Raises :class:`AirflowConnectionError`; an authoritative deny of a secrets backend
    (``AirflowSecretsBackendAccessDenied``, Airflow 3.3+) is re-raised as that error
    without trying the remaining lookups."""
    try:
        from airflow import exceptions as airflow_exceptions

        denied: tuple[type[BaseException], ...] = tuple(
            cls
            for cls in (getattr(airflow_exceptions, "AirflowSecretsBackendAccessDenied", None),)
            if isinstance(cls, type)
        )
    except ImportError:
        denied = ()
    errors: list[str] = []
    for load in _connection_loaders():
        try:
            return load(conn_id)
        except denied as exc:
            raise AirflowConnectionError(
                f"access to the Airflow connection {conn_id!r} was denied"
            ) from exc
        except Exception as exc:  # Airflow raises its own not-found / configuration errors
            errors.append(type(exc).__name__)
    if not errors:
        errors.append("Airflow is not installed")
    raise AirflowConnectionError(
        f"the Airflow connection {conn_id!r} is not defined or not readable ({', '.join(errors)})"
    )
