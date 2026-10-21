"""Import a DAG file with Airflow's own ``DagBag`` in an isolated process.

Running outside the API server process keeps user code (python_task bodies, operator
imports) away from it and gives us a hard timeout. By default checks run on a pool of
warm worker processes (:mod:`autoair.validation.pool`); set ``AUTOAIR_DAGBAG_POOL=0``
or pass ``use_pool=False`` to use a fresh one-shot subprocess per check instead.

With ``validator.url`` set, checks go to the separate validator service
(:mod:`autoair.validation.remote`) and no user code runs in this process; when the service
cannot be asked, ``validator.fail_mode`` decides: ``open`` (default) and ``closed`` raise
:class:`ImportCheckUnavailable` (a warning / an error for the caller), ``local`` uses the
local pool.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from autoair._subprocess import user_code_env

log = logging.getLogger(__name__)


class ImportCheckResult(BaseModel):
    ok: bool = False
    dag_ids: list[str] = []
    task_ids: dict[str, list[str]] = {}
    import_errors: list[str] = []


def _runner(dag_file: str) -> None:  # pragma: no cover - executed in subprocess
    from autoair.validation.pool import bag_summary

    sys.stdout.write("\n@@AUTOAIR@@" + json.dumps(bag_summary(dag_file)))


class ImportCheckUnavailable(Exception):
    """The validator service could not be asked (``validator.fail_mode`` closed / open)."""

    def __init__(self, reason: str, fail_mode: str) -> None:
        self.reason = reason
        self.fail_mode = fail_mode
        if fail_mode == "open":
            tail = "the DAG was not imported (fail open)"
        else:
            tail = "deploys are blocked (fail closed)"
        super().__init__(f"the import check service is unavailable ({reason}); {tail}")

    @property
    def severity(self) -> Literal["error", "warning"]:
        return "warning" if self.fail_mode == "open" else "error"


def _count(backend: str, result: str) -> None:
    try:
        from autoair.api.routers.ops import IMPORT_CHECKS
    except ImportError:  # pragma: no cover - the API is part of the core install
        return
    IMPORT_CHECKS.labels(backend, result).inc()


def _remote(source: str, filename: str, timeout: float) -> ImportCheckResult | None:
    """The validator's result; ``None`` when there is no validator or it is down and
    ``fail_mode`` is ``local``."""
    from autoair.sections import ValidatorSettings, active

    cfg = active(ValidatorSettings)
    if not cfg.url:
        return None
    from autoair.validation.remote import ValidatorUnavailable, check_remote

    try:
        result = check_remote(source, filename, timeout, cfg)
    except ValidatorUnavailable as exc:
        _count("remote", "unavailable")
        if cfg.fail_mode == "local":
            log.warning("validator unavailable (%s); import check runs locally", exc)
            return None
        raise ImportCheckUnavailable(str(exc), cfg.fail_mode) from exc
    _count("remote", "ok" if result.ok else "failed")
    return result


def _use_pool(use_pool: bool | None) -> bool:
    if use_pool is not None:
        return use_pool
    from autoair.validation.pool import pool_enabled

    return pool_enabled()


def check_source(
    source: str, filename: str = "dag.py", timeout: float = 60, *, use_pool: bool | None = None
) -> ImportCheckResult:
    """Import ``source`` (written to a temp dir as ``filename``) via DagBag: on the
    validator service when ``validator.url`` is set (``use_pool`` given explicitly forces
    a local check), else locally. Raises :class:`ImportCheckUnavailable` when the
    validator cannot be asked and ``validator.fail_mode`` is not ``local``."""
    if use_pool is None:
        remote = _remote(source, filename, timeout)
        if remote is not None:
            return remote
    result = _check_source_local(source, filename, timeout, use_pool)
    _count("local", "ok" if result.ok else "failed")
    return result


def _check_source_local(
    source: str, filename: str, timeout: float, use_pool: bool | None
) -> ImportCheckResult:
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
    if use_pool is None:
        path = Path(path)
        remote = _remote(path.read_text(encoding="utf-8"), path.name, timeout)
        if remote is not None:
            return remote
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
