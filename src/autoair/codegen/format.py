"""Optional ``ruff format`` pass over generated code."""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys


def _passes() -> tuple[list[str], list[str], float]:
    """(isort pass, format pass, timeout) from AUTOAIR_CODEGEN__* settings."""
    from autoair.sections import CodegenSettings, active

    cfg = active(CodegenSettings)
    common = [
        "--line-length",
        str(cfg.line_length),
        "--stdin-filename",
        "dag.py",
        "--no-cache",
        "--isolated",
    ]
    sort_imports = ["check", "--select", "I", "--fix-only", "--exit-zero", *common, "-"]
    return sort_imports, ["format", *common, "-"], cfg.format_timeout


def _ruff_command() -> list[str] | None:
    exe = shutil.which("ruff")
    if exe:
        return [exe]
    if importlib.util.find_spec("ruff") is None:
        return None
    return [sys.executable, "-m", "ruff"]


def format_source(source: str) -> str:
    """Format with ruff when available; otherwise return ``source`` unchanged."""
    cmd = _ruff_command()
    if cmd is None:
        return source
    sort_imports, fmt, timeout = _passes()
    for args in (sort_imports, fmt):
        try:
            proc = subprocess.run(
                [*cmd, *args],
                input=source,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return source
        if proc.returncode == 0 and proc.stdout:
            source = proc.stdout
    return source
