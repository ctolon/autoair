"""The logical-date context variables: Airflow 3 semantics and the frontend's shared copy."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from autoair.validation.jinja import (
    DAG_RUN_LOGICAL_DATE_ATTRS,
    LOGICAL_DATE_NAMES,
    TIME_NAMES,
    check_template,
)

ROOT = Path(__file__).resolve().parents[2]

# Set by Airflow 3 for every run, with or without a logical date.
ALWAYS_SET = ("task_instance_key_str", "prev_start_date_success", "prev_end_date_success", "run_id")


def _warnings(source: str, *, time_available: bool) -> list[str]:
    return [
        i.message
        for i in check_template(source, time_available=time_available)
        if "logical date" in i.message
    ]


@pytest.mark.parametrize("name", LOGICAL_DATE_NAMES)
def test_logical_date_name_warns_without_logical_date(name: str) -> None:
    messages = _warnings(f"{{{{ {name} }}}}", time_available=False)
    assert len(messages) == 1
    assert f"'{name}'" in messages[0]
    assert _warnings(f"{{{{ {name} }}}}", time_available=True) == []


@pytest.mark.parametrize("attr", DAG_RUN_LOGICAL_DATE_ATTRS)
def test_dag_run_attribute_warns_without_logical_date(attr: str) -> None:
    messages = _warnings(f"{{{{ dag_run.{attr} }}}}", time_available=False)
    assert messages == [
        f"'dag_run.{attr}' is None for runs without a logical date "
        "(asset-triggered or manual runs in Airflow 3)"
    ]


@pytest.mark.parametrize("name", ALWAYS_SET)
def test_always_set_names_do_not_warn(name: str) -> None:
    assert _warnings(f"{{{{ {name} }}}}", time_available=False) == []


def test_locally_assigned_name_does_not_warn() -> None:
    assert _warnings("{% set ds = 'x' %}{{ ds }}", time_available=False) == []
    assert _warnings("{{ dag_run.run_id }} {{ params.ds }}", time_available=False) == []


def test_time_names_match_the_list() -> None:
    assert frozenset(LOGICAL_DATE_NAMES) == TIME_NAMES
    assert len(set(LOGICAL_DATE_NAMES)) == len(LOGICAL_DATE_NAMES)


@pytest.mark.airflow
def test_names_match_airflow_task_runner() -> None:
    """Every listed name is a key Airflow adds only inside the logical-date block."""
    import inspect

    pytest.importorskip("airflow.sdk.execution_time.task_runner")
    from airflow.sdk.execution_time import task_runner

    source = inspect.getsource(task_runner.RuntimeTaskInstance.get_template_context)
    start = source.index("if logical_date := ")
    block = source[start:]
    before = source[:start]
    for name in LOGICAL_DATE_NAMES:
        assert f'"{name}":' in block, name
        assert f'"{name}":' not in before, name
    for name in ("task_instance_key_str", "prev_start_date_success", "prev_end_date_success"):
        assert f'"{name}":' in before, name


def test_frontend_constant_is_current() -> None:
    spec = importlib.util.spec_from_file_location(
        "gen_shared_constants", ROOT / "scripts" / "gen_shared_constants.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("gen_shared_constants", module)
    spec.loader.exec_module(module)
    assert module.main(["--check"]) == 0, "run: uv run python scripts/gen_shared_constants.py"
