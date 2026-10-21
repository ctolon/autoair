"""``autoair`` top-level options and help output."""

from __future__ import annotations

from typer.testing import CliRunner

from autoair import __version__
from autoair.cli import app


def test_version_option() -> None:
    result = CliRunner().invoke(app, ["--version"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == f"autoair {__version__}"


def test_help_keeps_angle_bracket_placeholders() -> None:
    # Markdown help rendering would drop bare <placeholders> as HTML tags.
    result = CliRunner().invoke(app, ["new-node", "--help"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    assert "builtin:<logo>" in result.output


def _without_uvicorn(monkeypatch, airflow: bool) -> None:
    import importlib.util

    from autoair import server

    real = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name, *a, **kw: None if name == "uvicorn" else real(name, *a, **kw),
    )
    monkeypatch.setattr(server, "airflow_installed", lambda: airflow)


def test_local_without_uvicorn_names_the_extras_to_install(monkeypatch, tmp_path) -> None:
    _without_uvicorn(monkeypatch, airflow=False)
    result = CliRunner().invoke(app, ["local", "--dags-folder", str(tmp_path)])
    assert result.exit_code == 1
    assert "'standalone' extra" in result.output
    assert "uvx --from 'autoair[standalone,airflow]' autoair local --open" in result.output
    assert "Traceback" not in result.output


def test_local_without_uvicorn_but_with_airflow_only_needs_standalone(monkeypatch) -> None:
    _without_uvicorn(monkeypatch, airflow=True)
    result = CliRunner().invoke(app, ["local"])
    assert result.exit_code == 1
    assert "pip install 'autoair[standalone]'" in result.output


def test_serve_without_uvicorn_names_the_standalone_extra(monkeypatch) -> None:
    _without_uvicorn(monkeypatch, airflow=True)
    result = CliRunner().invoke(app, ["serve"])
    assert result.exit_code == 1
    assert "pip install 'autoair[standalone]'" in result.output
