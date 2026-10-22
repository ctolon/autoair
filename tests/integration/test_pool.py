"""Warm DagBag worker pool."""

from __future__ import annotations

import os
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


def test_worker_cannot_make_the_parent_unpickle(pool, tmp_path):
    """DAG code that writes a pickle to the worker's pipe must not run code in the parent
    (the pipe carries JSON only)."""
    marker = tmp_path / "parent-executed"
    code = (
        MARK
        + "import os, pickle, re, struct, time\n"
        + "cmd = open('/proc/self/cmdline', 'rb').read().decode()\n"
        + "fd = int(re.search(r'_worker_entry\\((\\d+)', cmd).group(1))\n"
        + "class Evil:\n"
        + "    def __reduce__(self):\n"
        + f"        return (open, ({str(marker)!r}, 'w'))\n"
        + "data = pickle.dumps(('ok', Evil()))\n"
        + "os.write(fd, struct.pack('!i', len(data)) + data)\n"
        + "time.sleep(5)\n"
    )
    result = pool.submit(code, "evil.py", 30)
    assert not result.ok
    assert "crashed" in result.import_errors[0] or "invalid" in result.import_errors[0]
    assert not marker.exists()
    assert pool.submit(DAG.format(dag_id="after_evil"), "after.py", 60).ok


def test_stopping_a_worker_kills_its_process_group(tmp_path):
    """A process the DAG code started dies with the worker (own process group)."""
    pid_file = tmp_path / "child.pid"
    code = (
        MARK
        + "import subprocess, sys\n"
        + "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        + f"open({str(pid_file)!r}, 'w').write(str(p.pid))\n"
        + "raise ValueError('started')\n"
    )
    with DagBagPool(1, max_tasks_per_child=1) as p:
        assert "started" in p.submit(code, "child.py", 30).import_errors[0]
        child = int(pid_file.read_text())
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            os.kill(child, 9)
            raise AssertionError("the child of the recycled worker survived")


def test_a_partial_reply_cannot_hang_the_pool(tmp_path):
    """DAG code that writes a length header to the worker's pipe and stops must not block the
    parent past the check's timeout (the deadline covers the whole reply)."""
    code = (
        MARK
        + "import os, re, struct, time\n"
        + "cmd = open('/proc/self/cmdline', 'rb').read().decode()\n"
        + "fd = int(re.search(r'_worker_entry\\((\\d+)', cmd).group(1))\n"
        + "os.write(fd, struct.pack('!i', 1000))\n"
        + "time.sleep(120)\n"
    )
    with DagBagPool(1, max_tasks_per_child=50) as p:
        p.warmup()
        started = p.started
        t0 = time.perf_counter()
        result = p.submit(code, "hang.py", 2.0)
        assert time.perf_counter() - t0 < 15
        assert not result.ok
        assert "timed out" in result.import_errors[0]
        assert p.started == started + 1  # the worker was replaced, its slot released
        assert p.submit(DAG.format(dag_id="after_hang"), "after.py", 60).ok


def test_a_reply_for_another_check_is_refused(tmp_path):
    """A complete forged reply without this check's nonce (as code left running from an
    earlier check would write) is not taken as the result."""
    code = (
        MARK
        + "import json, os, re, struct, time\n"
        + "cmd = open('/proc/self/cmdline', 'rb').read().decode()\n"
        + "fd = int(re.search(r'_worker_entry\\((\\d+)', cmd).group(1))\n"
        + "fake = {'tag': 'ok', 'result': {'dag_ids': ['forged'], 'task_ids': {},\n"
        + "        'import_errors': []}, 'nonce': 'guessed'}\n"
        + "data = json.dumps(fake).encode()\n"
        + "os.write(fd, struct.pack('!i', len(data)) + data)\n"
        + "time.sleep(5)\n"
    )
    with DagBagPool(1, max_tasks_per_child=50) as p:
        result = p.submit(code, "forged.py", 30)
        assert not result.ok
        assert result.dag_ids == []
        assert "invalid reply" in result.import_errors[0]
        assert p.submit(DAG.format(dag_id="after_forged"), "after.py", 60).ok


# --------------------------------------------------------------------------- local sandbox

PROC_PROBE = """
import os
from airflow.sdk import DAG

with open(f"/proc/{os.getppid()}/environ", "rb") as fh:
    fh.read()

with DAG("proc_probe", schedule=None):
    pass
"""

CFG_PROBE = """
from airflow.configuration import conf
from airflow.sdk import DAG

fernet = conf.get("core", "fernet_key", fallback="")
with DAG("cfg_" + ("fernet" if fernet else "nofernet"), schedule=None):
    pass
"""


@pytest.fixture
def local_pool(tmp_path, monkeypatch):
    """The local pool's sandbox (no validator) over an AIRFLOW_HOME whose airflow.cfg holds
    a Fernet key."""
    from autoair.validation.pool import local_pool_sandbox

    home = tmp_path / "airflow"
    (home / "dags").mkdir(parents=True)
    (home / "airflow.cfg").write_text(
        f"[core]\nfernet_key = {'k' * 43}=\ndags_folder = {home / 'dags'}\n", encoding="utf-8"
    )
    monkeypatch.setenv("AIRFLOW_HOME", str(home))
    monkeypatch.setenv("AIRFLOW_CONFIG", str(home / "airflow.cfg"))
    monkeypatch.delenv("AIRFLOW__CORE__FERNET_KEY", raising=False)
    sandbox = local_pool_sandbox()
    assert sandbox.profile == "run"
    assert sandbox.sanitize_airflow_config
    assert sandbox.process_isolation == "off"
    p = DagBagPool(1, sandbox=sandbox)
    yield p, home
    p.shutdown()


def test_local_pool_denies_reading_the_parents_proc_entries(local_pool):
    pool, _ = local_pool
    probe = pool.submit(PROC_PROBE, "proc_probe.py", 60)
    assert not probe.ok
    assert "not allowed during an AutoAir test run" in probe.import_errors[0]
    normal = pool.submit(DAG.format(dag_id="normal_after_probe"), "normal.py", 60)
    assert normal.ok, normal.import_errors
    assert normal.dag_ids == ["normal_after_probe"]


def test_local_pool_reads_a_secret_free_airflow_cfg(local_pool):
    pool, home = local_pool
    result = pool.submit(CFG_PROBE, "cfg_probe.py", 60)
    assert result.ok, result.import_errors
    assert result.dag_ids == ["cfg_nofernet"]
    # the DAG folder is read-only: a check cannot plant a file Airflow would parse
    plant = pool.submit(
        MARK + f"open({str(home / 'dags' / 'planted.py')!r}, 'w').write('x = 1')\n",
        "plant.py",
        60,
    )
    assert not plant.ok
    assert not (home / "dags" / "planted.py").exists()
    # files elsewhere still import (a deployed DAG checked by path)
    deployed = home / "dags" / "deployed.py"
    deployed.write_text(DAG.format(dag_id="deployed"), encoding="utf-8")
    assert pool.submit_file(deployed, 60).ok


def test_singleton_pool_is_sandboxed():
    shutdown_pool()
    try:
        pool = get_pool()
        assert pool.sandbox is not None
        assert pool.sandbox.profile == "run"
        assert dagbag_check.check_source(DAG.format(dag_id="boxed"), "boxed.py").ok
    finally:
        shutdown_pool()
