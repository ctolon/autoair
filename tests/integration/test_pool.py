"""Warm DagBag worker pool."""

from __future__ import annotations

import time

import pytest

from autoair.validation import dagbag_check
from autoair.validation.pool import DagBagPool, get_pool, shutdown_pool

pytestmark = pytest.mark.airflow

DAG = """
from airflow.sdk import DAG
from airflow.providers.standard.operators.bash import BashOperator

with DAG("{dag_id}", schedule=None):
    BashOperator(task_id="hello", bash_command="echo hi")
"""
# DagBag's safe mode skips files that do not mention both "airflow" and "dag"
MARK = "# airflow dag\n"


@pytest.fixture(scope="module")
def pool():
    p = DagBagPool(1, max_tasks_per_child=50)
    yield p
    p.shutdown()


def test_warm_check_is_fast(pool):
    first = pool.submit(DAG.format(dag_id="warm1"), "warm1.py", 60)
    assert first.ok
    assert first.dag_ids == ["warm1"]
    assert first.task_ids == {"warm1": ["hello"]}
    start = time.perf_counter()
    second = pool.submit(DAG.format(dag_id="warm2"), "warm2.py", 60)
    elapsed = time.perf_counter() - start
    assert second.ok
    assert second.dag_ids == ["warm2"]
    assert elapsed < 0.5, elapsed


def test_import_error_reported(pool):
    result = pool.submit(MARK + "raise ValueError('boom')\n", "bad.py", 30)
    assert not result.ok
    assert "boom" in result.import_errors[0]


def test_timeout_kills_and_respawns(pool):
    started = pool.started
    t0 = time.perf_counter()
    result = pool.submit(MARK + "import time\ntime.sleep(60)\n", "slow.py", 1.0)
    assert time.perf_counter() - t0 < 10
    assert not result.ok
    assert "timed out" in result.import_errors[0]
    assert pool.started == started + 1  # replacement already spawned
    again = pool.submit(DAG.format(dag_id="after_timeout"), "after.py", 60)
    assert again.ok


def test_crash_is_isolated(pool):
    result = pool.submit(MARK + "import os\nos._exit(7)\n", "crash.py", 30)
    assert not result.ok
    assert "crashed" in result.import_errors[0]
    assert pool.submit(DAG.format(dag_id="after_crash"), "after.py", 60).ok


def test_worker_recycling():
    with DagBagPool(1, max_tasks_per_child=2) as p:
        for i in range(5):
            assert p.submit(DAG.format(dag_id=f"r{i}"), f"r{i}.py", 60).ok
        assert p.started == 3  # 2 checks per worker


def test_concurrent_submissions():
    from concurrent.futures import ThreadPoolExecutor

    with DagBagPool(2) as p, ThreadPoolExecutor(4) as ex:
        results = list(
            ex.map(lambda i: p.submit(DAG.format(dag_id=f"c{i}"), f"c{i}.py", 60), range(6))
        )
    assert [r.dag_ids for r in results] == [[f"c{i}"] for i in range(6)]
    assert p.started <= 2


def test_check_source_uses_singleton_pool_and_escape_hatch(monkeypatch):
    shutdown_pool()
    try:
        result = dagbag_check.check_source(DAG.format(dag_id="single"), "single.py")
        assert result.ok
        assert get_pool().started >= 1
        oneshot = dagbag_check.check_source(
            DAG.format(dag_id="oneshot"), "oneshot.py", use_pool=False
        )
        assert oneshot.ok
        assert oneshot.dag_ids == ["oneshot"]
        monkeypatch.setenv("AUTOAIR_DAGBAG_POOL", "0")
        assert dagbag_check._use_pool(None) is False
    finally:
        shutdown_pool()
