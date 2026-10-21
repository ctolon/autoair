"""``autoair db upgrade`` / ``autoair db current``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

pytest.importorskip("sqlalchemy")

from autoair.cli import app
from autoair.persistence import migrations
from autoair.persistence.db import sqlalchemy_major


def test_db_upgrade_and_current(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOAIR_DATABASE__BACKEND", "sqlite")
    monkeypatch.setenv("AUTOAIR_DATABASE__URL", f"sqlite:///{tmp_path / 'cli.db'}")
    runner = CliRunner()
    before = runner.invoke(app, ["db", "current", "--json"])
    assert before.exit_code == 0, before.output
    assert json.loads(before.output)["current"] is None
    assert json.loads(before.output)["pending"] == [m.version for m in migrations.MIGRATIONS]
    up = runner.invoke(app, ["db", "upgrade"])
    assert up.exit_code == 0, up.output
    assert "applied: [1]" in up.output
    again = runner.invoke(app, ["db", "upgrade"])
    assert "nothing (up to date)" in again.output
    after = json.loads(runner.invoke(app, ["db", "current", "--json"]).output)
    assert after == {
        "url": f"sqlite:///{tmp_path / 'cli.db'}",
        "current": migrations.HEAD,
        "head": migrations.HEAD,
        "pending": [],
    }
    text = runner.invoke(app, ["db", "current"])
    assert f"version {migrations.HEAD}" in text.output
    bad = runner.invoke(app, ["db", "upgrade", "--revision", "999"])
    assert bad.exit_code == 2


def test_db_commands_refuse_the_file_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOAIR_DATABASE__BACKEND", "file")
    result = CliRunner().invoke(app, ["db", "current"])
    assert result.exit_code == 2


def test_db_credentials_are_masked(monkeypatch: pytest.MonkeyPatch) -> None:
    # The driver follows the installed SQLAlchemy (psycopg2 on 1.4); without it the command
    # exits 2 with an install hint before it ever connects.
    pytest.importorskip("psycopg" if sqlalchemy_major() >= 2 else "psycopg2")
    monkeypatch.setenv("AUTOAIR_DATABASE__BACKEND", "postgresql")
    monkeypatch.setenv("AUTOAIR_DATABASE__URL", "postgresql://u:topsecret@127.0.0.1:1/x")
    monkeypatch.setenv("AUTOAIR_DATABASE__POOL_TIMEOUT", "1")
    monkeypatch.setenv("AUTOAIR_DATABASE__CONNECT_ARGS", '{"connect_timeout": 1}')
    result = CliRunner().invoke(app, ["db", "current"])
    assert result.exit_code == 1
    assert "topsecret" not in result.output
    assert "u:***@" in result.output
