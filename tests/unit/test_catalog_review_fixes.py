"""Regression tests: catalog review findings (discovery pre-filter, per-catalog filters, cache
key inputs, subprocess environment, probe memoisation, editable installs, cache files)."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from autoair import sections
from autoair.catalog import discovery, importer, service
from autoair.catalog.discovery import DiscoveryFilters, maybe_task_file
from autoair.catalog.service import (
    CatalogData,
    OperatorCatalog,
    ProvidersSource,
    _prune,
    _write_data,
    probe_imports,
)
from autoair.sections import DiscoverySettings

PLAIN = "class Thing(Base):\n    pass\n"


# ------------------------------------------------------------- 2. empty class hint


def test_empty_class_hint_pattern_disables_the_prefilter(tmp_path: Path) -> None:
    f = tmp_path / "x.py"
    f.write_text(PLAIN, encoding="utf-8")
    assert not maybe_task_file(f)  # default hint: needs Operator/Sensor/… in the text
    discovery.configure(DiscoverySettings(class_hint_pattern=""))
    assert maybe_task_file(f)
    no_class = tmp_path / "y.py"
    no_class.write_text("x = 1\n", encoding="utf-8")
    assert not maybe_task_file(no_class)  # still needs a class definition


# ------------------------------------------------------------- 6. reset_all / own filters


def test_reset_all_restores_defaults() -> None:
    from autoair.ai import config as ai_config

    discovery.configure(DiscoverySettings(skip_dirs_extra=["zzz"]))
    sections.active(sections.SandboxSettings)
    ai_config.activate_ai(None)
    sections.reset_all()
    assert discovery.filters().settings == DiscoverySettings()
    assert not sections._ACTIVE
    assert not ai_config._ACTIVE


def test_catalog_keeps_its_own_discovery_filters() -> None:
    own = DiscoveryFilters.from_settings(DiscoverySettings(skip_dirs_extra=["mine"]))
    catalog = OperatorCatalog(sources=[], discovery=own)
    key = catalog.cache_key_parts()
    discovery.configure(DiscoverySettings(skip_dirs_extra=["other"]))
    assert catalog.cache_key_parts() == key
    assert key["discovery"] == own.settings.fingerprint()


# ------------------------------------------------------------- 9. scan_packages in the key


def test_cache_key_includes_scan_packages() -> None:
    a = OperatorCatalog([ProvidersSource(scan_packages=True)], use_cache=False)
    b = OperatorCatalog([ProvidersSource(scan_packages=False)], use_cache=False)
    assert a._key(a.cache_key_parts()) != b._key(b.cache_key_parts())


# ------------------------------------------------------------- 13. stable key on errors


class _Broken:
    def modules(self) -> Any:
        raise RuntimeError(f"boom at {time.time()} {object()!r}")


def test_cache_key_is_stable_when_a_source_fails() -> None:
    catalog = OperatorCatalog([_Broken()], use_cache=False)
    first = catalog._key(catalog.cache_key_parts())
    assert first == catalog._key(catalog.cache_key_parts())
    assert ["error", "RuntimeError"] in catalog.cache_key_parts()["sources"][0]


def test_write_failure_leaves_no_tmp_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_a: object, **_k: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(service.os, "replace", fail)
    data = CatalogData(key="k", key_parts={}, strategy="static", built_at=0, build_seconds=0)
    with pytest.raises(OSError, match="disk full"):
        _write_data(tmp_path / "catalog-static-x.json", data)
    assert not list(tmp_path.glob(".catalog-*.tmp"))


def test_prune_removes_old_tmp_files(tmp_path: Path) -> None:
    keep = tmp_path / "catalog-static-a.json"
    keep.write_text("{}", encoding="utf-8")
    old = tmp_path / ".catalog-static-b.json.1.2.tmp"
    fresh = tmp_path / ".catalog-static-c.json.1.3.tmp"
    old.write_text("x", encoding="utf-8")
    fresh.write_text("x", encoding="utf-8")
    past = time.time() - 2 * service._TMP_MAX_AGE
    os.utime(old, (past, past))
    _prune(tmp_path, keep)
    assert not old.exists()
    assert fresh.exists()  # may belong to a running writer


# ------------------------------------------------------------- 10/11. probes


class _Proc:
    def __init__(self, stdout: str, returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = "Segmentation fault"
        self.returncode = returncode


def test_probe_runs_with_user_code_env_and_is_not_memoised_on_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AUTOAIR_STORAGE__GIT__TOKEN", "ghp_secret")
    seen: list[dict[str, str]] = []
    outputs = [_Proc("", returncode=-11), _Proc('@@PROBE@@{"zz_mod": "ImportError: x"}')]

    def fake_run(*_a: object, **kw: Any) -> _Proc:
        seen.append(kw["env"])
        return outputs.pop(0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(service, "_PROBE_CACHE", {})
    assert probe_imports(["zz_mod"]) == {}  # crash: unknown, kept for now
    assert "zz_mod" not in service._PROBE_CACHE
    assert probe_imports(["zz_mod"]) == {"zz_mod": "ImportError: x"}  # probed again
    assert all("AUTOAIR_STORAGE__GIT__TOKEN" not in env for env in seen)


def test_probe_timeout_is_not_memoised(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*_a: object, **_kw: Any) -> _Proc:
        raise subprocess.TimeoutExpired("python", 1)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(service, "_PROBE_CACHE", {})
    assert probe_imports(["slow_mod"], timeout=1) == {}
    assert service._PROBE_CACHE == {}


def test_introspection_subprocess_gets_user_code_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOAIR_AI__API_KEY", "sk-secret")
    seen: dict[str, Any] = {}

    def fake_run(*_a: object, **kw: Any) -> _Proc:
        seen.update(kw)
        return _Proc('@@AUTOAIR-INTROSPECT@@{"results": {}, "errors": {}}')

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(importer, "_MARKER", "@@AUTOAIR-INTROSPECT@@")
    importer.introspect_in_subprocess([{"module": "m"}])
    assert "env" in seen
    assert "AUTOAIR_AI__API_KEY" not in seen["env"]


# ------------------------------------------------------------- 12. editable installs


class _FakeFile:
    def __init__(self, name: str) -> None:
        self.name = name

    def __str__(self) -> str:
        return self.name


class _FakeDist:
    """An editable install: RECORD lists only a .pth file pointing at the source folder."""

    def __init__(self, src: Path) -> None:
        self.name = "acme-editable-ops"
        self.version = "1.0"
        self._src = src
        self.files = [_FakeFile("__editable__.acme_editable_ops-1.0.pth")]

    def read_text(self, name: str) -> str | None:
        return None

    def locate_file(self, f: object) -> Path:
        pth = self._src.parent / "site" / str(f)
        pth.parent.mkdir(parents=True, exist_ok=True)
        pth.write_text(str(self._src) + "\n", encoding="utf-8")
        return pth


def test_distributions_source_falls_back_for_editable_installs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    src = tmp_path / "src"
    pkg = src / "acme_editable_ops"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "jira.py").write_text(
        "from airflow.sdk import BaseOperator\nclass JiraOperator(BaseOperator):\n    pass\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(src))
    dist = _FakeDist(src)
    monkeypatch.setattr(discovery, "distribution", lambda name: dist)
    discovery.clear_locator_cache()
    caplog.set_level("INFO", logger="autoair.catalog.discovery")
    refs = list(discovery.DistributionsSource(["acme-editable-ops"]).modules())
    assert [r.module for r in refs] == ["acme_editable_ops.jira"]
    assert "editable install" in caplog.text
    assert discovery.distribution_packages("acme-editable-ops") == ["acme_editable_ops"]
