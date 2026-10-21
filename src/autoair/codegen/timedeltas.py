"""Operator parameters that Airflow only accepts as ``datetime.timedelta``.

The builder stores durations as plain seconds (the HITL editor's *Response timeout
(s)*, raw spec JSON, imported Dags). Most Airflow duration arguments accept a number
too (``retry_delay``, ``max_retry_delay``, sensors' ``poke_interval``/``timeout``/
``max_wait``, ``TimeDeltaSensor.delta``), but the ones listed here are used in date
arithmetic or type-checked and fail at run time with a number. Codegen turns a
numeric value of these parameters into a ``{"$type": "timedelta"}`` value, which is
generated as ``datetime.timedelta(seconds=...)``.
"""

from __future__ import annotations

from typing import Any

# Parameters every ``BaseOperator`` types as ``timedelta | None`` (``sla`` is accepted
# and ignored by Airflow 3, but still type-checked when it is set).
_ANY_OPERATOR = frozenset({"execution_timeout", "sla"})

# (FQN substring, parameter) pairs for operator-specific timedelta-only parameters.
_BY_OPERATOR: tuple[tuple[str, str], ...] = (
    # HITLOperator and subclasses: ``utcnow() + response_timeout``.
    (".operators.hitl.", "response_timeout"),
    # ExternalTaskSensor / ExternalTaskMarker: ``logical_date - execution_delta``.
    (".sensors.external_task.", "execution_delta"),
)


def requires_timedelta(operator: str, name: str) -> bool:
    """Whether ``operator``'s ``name`` argument must be a ``datetime.timedelta``."""
    if name in _ANY_OPERATOR:
        return True
    return any(fragment in operator and name == param for fragment, param in _BY_OPERATOR)


def coerce_timedelta(operator: str, name: str, value: Any) -> Any:
    """``value`` as a ``{"$type": "timedelta"}`` value when the parameter needs one."""
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and requires_timedelta(operator, name)
    ):
        return {"$type": "timedelta", "seconds": value}
    return value
