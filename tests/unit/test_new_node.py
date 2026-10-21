"""``autoair new-node`` scaffolds a package whose node kind is discovered through the
``autoair.node_kinds`` entry point and whose generated tests pass."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from autoair.cli import app
from autoair.registry import HandlerRegistry
from autoair.scaffold import ScaffoldError, new_node


def _install_metadata(project: Path, site: Path) -> None:
    """What ``pip install -e`` provides: the package importable and its entry point."""
    dist = site / "autoair_node_http_get-0.1.0.dist-info"
    dist.mkdir(parents=True)
    (dist / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: autoair-node-http-get\nVersion: 0.1.0\n"
    )
    (dist / "entry_points.txt").write_text(
        "[autoair.node_kinds]\nhttp_get = autoair_node_http_get.handler:HttpGetHandler\n"
    )
    (site / "autoair-node-http-get.pth").write_text(str(project / "src") + "\n")


def test_cli_scaffolds_a_compilable_package(tmp_path):
    result = CliRunner().invoke(
        app,
        ["new-node", "http_get", "--title", "HTTP GET", "--category", "web", "-o", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    project = tmp_path / "autoair-node-http-get"
    files = sorted(p.relative_to(project).as_posix() for p in project.rglob("*") if p.is_file())
    assert files == [
        "README.md",
        "pyproject.toml",
        "src/autoair_node_http_get/__init__.py",
        "src/autoair_node_http_get/handler.py",
        "tests/test_handler.py",
    ]
    for py in project.rglob("*.py"):
        ast.parse(py.read_text(), filename=str(py))
    handler = (project / "src/autoair_node_http_get/handler.py").read_text()
    assert 'name: ClassVar[str] = "http_get"' in handler
    assert 'title: ClassVar[str] = "HTTP GET"' in handler
    assert 'category: ClassVar[str] = "web"' in handler
    assert "{{" not in handler
    assert '"autoair.node_kinds"' in (project / "pyproject.toml").read_text()
    again = CliRunner().invoke(app, ["new-node", "http_get", "-o", str(tmp_path)])
    assert again.exit_code == 2
    assert "already exists" in again.output


@pytest.mark.parametrize("bad", ["HttpGet", "1x", "http-get", "a" * 70, "../x"])
def test_invalid_names_are_rejected(tmp_path, bad):
    with pytest.raises(ScaffoldError):
        new_node(bad, tmp_path)


def test_scaffolded_node_is_discovered_and_its_tests_pass(tmp_path, monkeypatch):
    project = new_node("http_get", tmp_path / "out")
    site = tmp_path / "site"
    _install_metadata(project, site)
    monkeypatch.syspath_prepend(str(project / "src"))
    monkeypatch.syspath_prepend(str(site))
    registry = HandlerRegistry.from_entry_points()
    assert "http_get" in registry

    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(site), str(project / "src")])}
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(project / "tests")],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "4 passed" in proc.stdout
