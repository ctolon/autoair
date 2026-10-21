"""Import a DAG file with Airflow's own ``DagBag`` in an isolated process.

Running outside the API server process keeps user code (python_task bodies, operator
imports) away from it and gives us a hard timeout. By default checks run on a pool of
warm worker processes (:mod:`autoair.validation.pool`); set ``AUTOAIR_DAGBAG_POOL=0``
or pass ``use_pool=False`` to use a fresh one-shot subprocess per check instead.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

from pydantic import BaseModel

from autoair._subprocess import user_code_env


class ImportCheckResult(BaseModel):
    ok: bool = False
    dag_ids: list[str] = []
    task_ids: dict[str, list[str]] = {}
    import_errors: list[str] = []


def _runner(dag_file: str) -> None:  # pragma: no cover - executed in subprocess
    from autoair.validation.pool import bag_summary

    sys.stdout.write("\n@@AUTOAIR@@" + json.dumps(bag_summary(dag_file)))


def _use_pool(use_pool: bool | None) -> bool:
    if use_pool is not None:
        return use_pool
    from autoair.validation.pool import pool_enabled

    return pool_enabled()


def check_source(
    source: str, filename: str = "dag.py", timeout: float = 60, *, use_pool: bool | None = None
) -> ImportCheckResult:
    """Import ``source`` (written to a temp dir as ``filename``) via DagBag."""
    if _use_pool(use_pool):
        from autoair.validation.pool import get_pool

        return get_pool().submit(source, filename, timeout)
    with tempfile.TemporaryDirectory(prefix="autoair-check-") as tmp:
        path = Path(tmp) / filename
        path.write_text(source, encoding="utf-8")
        return _check_file_oneshot(path, timeout)


def check_file(
    path: Path, timeout: float = 60, *, use_pool: bool | None = None
) -> ImportCheckResult:
    if _use_pool(use_pool):
        from autoair.validation.pool import get_pool

        return get_pool().submit_file(Path(path), timeout)
    return _check_file_oneshot(Path(path), timeout)


def _check_file_oneshot(path: Path, timeout: float) -> ImportCheckResult:
    code = "import sys; from autoair.validation.dagbag_check import _runner; _runner(sys.argv[1])"
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code, str(path)],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=user_code_env(),
        )
    except subprocess.TimeoutExpired:
        return ImportCheckResult(ok=False, import_errors=[f"import timed out after {timeout}s"])
    marker = proc.stdout.rfind("@@AUTOAIR@@")
    if proc.returncode != 0 or marker < 0:
        tail = (proc.stderr or proc.stdout).strip().splitlines()[-20:]
        return ImportCheckResult(ok=False, import_errors=["\n".join(tail) or "import failed"])
    data = json.loads(proc.stdout[marker + len("@@AUTOAIR@@") :])
    result = ImportCheckResult.model_validate(data)
    result.ok = not result.import_errors and bool(result.dag_ids)
    return result
