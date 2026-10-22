"""Airflow connections kept in the metadata database are found outside a task.

On Airflow 3.3 the Task SDK's ``BaseHook.get_connection`` searches only env vars, the
secrets backend and the Execution API when it runs outside a task, so ``ai.llm_conn_id``
pointing at a connection created in the UI failed with "conn_id isn't defined" in the
plugin, ``autoair serve`` and ``autoair ai-serve``. Each case runs in a child process with
its own ``AIRFLOW_HOME`` because Airflow reads its configuration once per process.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.airflow

_SETUP = """
from airflow.models.connection import Connection
from airflow.utils.session import create_session

with create_session() as session:
    session.add(Connection(conn_id="db_llm", conn_type="pydanticai", password="sk-from-db",
                           extra='{"model": "anthropic:claude-haiku-5-5"}'))
    session.add(Connection(conn_id="db_git", conn_type="git", login="bot",
                           password="git-token-from-db", host="https://git.example.com/a/b.git"))
"""

_PROBE = """
import json, sys
try:
    from airflow.sdk import BaseHook
except ImportError:  # Airflow 3.0: the core BaseHook, which still reads the metadata DB
    BaseHook = None
from autoair.ai.llm import LLM, LLMError
from autoair.storage.git.config import credentials_from_connection

out = {}
if BaseHook is not None:
    try:
        BaseHook.get_connection(sys.argv[1])
        out["sdk"] = "found"
    except Exception as exc:
        out["sdk"] = type(exc).__name__
try:
    cfg = LLM(conn_id=sys.argv[1]).resolve()
    out["llm"] = {"model": cfg.model, "key_ok": cfg.api_key.get_secret_value() == sys.argv[2]}
except LLMError as exc:
    out["llm"] = {"error": str(exc)}
creds = credentials_from_connection("db_git")
out["git"] = None if creds is None else {"token_ok": creds.token == "git-token-from-db"}
print(json.dumps(out))
"""


def _env(home: Path, **extra: str) -> dict[str, str]:
    # No AIRFLOW_CONN_* and no secrets-backend override (the old workaround) leak in.
    env = {k: v for k, v in os.environ.items() if not k.startswith("AIRFLOW")}
    return {**env, "AIRFLOW_HOME": str(home), **extra}


def _run(env: dict[str, str], *args: str) -> str:
    proc = subprocess.run(
        [sys.executable, *args], env=env, capture_output=True, text=True, timeout=300
    )
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-4000:]
    return proc.stdout


def _probe(env: dict[str, str], conn_id: str, key: str) -> dict[str, Any]:
    lines = _run(env, "-c", _PROBE, conn_id, key).strip().splitlines()
    result: dict[str, Any] = json.loads(lines[-1])
    return result


def test_connection_in_the_metadata_db_is_found(tmp_path: Path) -> None:
    pytest.importorskip("pydantic_ai")
    env = _env(tmp_path)
    _run(env, "-m", "airflow", "db", "migrate")
    _run(env, "-c", _SETUP)

    result = _probe(env, "db_llm", "sk-from-db")

    # The Task SDK alone (what ai/llm.py used to call) does not see the metadata DB.
    assert result.get("sdk", "AirflowNotFoundException") == "AirflowNotFoundException"
    assert result["llm"] == {"model": "claude-haiku-5-5", "key_ok": True}
    assert result["git"] == {"token_ok": True}


def test_env_connection_without_a_metadata_db(tmp_path: Path) -> None:
    pytest.importorskip("pydantic_ai")
    conn = json.dumps(
        {"conn_type": "pydanticai", "password": "sk-env", "extra": {"model": "openai:gpt-5"}}
    )
    # A database URL nobody listens on: env connections must not depend on it.
    env = _env(
        tmp_path,
        AIRFLOW_CONN_ENV_LLM=conn,
        AIRFLOW__DATABASE__SQL_ALCHEMY_CONN=f"sqlite:///{tmp_path / 'missing' / 'x.db'}",
    )

    result = _probe(env, "env_llm", "sk-env")

    assert result["llm"] == {"model": "gpt-5", "key_ok": True}
    assert result["git"] is None


def test_missing_connection_error_names_no_secret(tmp_path: Path) -> None:
    pytest.importorskip("pydantic_ai")
    env = _env(tmp_path)
    _run(env, "-m", "airflow", "db", "migrate")
    _run(env, "-c", _SETUP)

    result = _probe(env, "nope", "-")

    error = result["llm"]["error"]
    assert "'nope'" in error
    assert "not defined" in error
    assert "sk-from-db" not in error
