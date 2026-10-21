"""Configuration edge cases: precedence per source, Python config files, module
references, credential masking, feature names and CLI choices."""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from autoair import config_sources
from autoair.config_reference import MASK, mask_url_credentials, masked
from autoair.config_sources import CONFIG_FILE_ENV, ConfigError, load_config_file, load_settings
from autoair.deploy_setup import (
    DeployTargetStore,
    restore_secrets,
    scrub_secrets,
    unreferenced_secrets,
)
from autoair.settings import Settings
from autoair.storage.config import StorageSettings


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in (
        CONFIG_FILE_ENV,
        "AIRFLOW__AUTOAIR__CONFIG_FILE",
        "AUTOAIR_AI__MODEL",
        "AUTOAIR_AI__PROVIDER",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AIRFLOW_HOME", str(tmp_path / "airflow-home"))
    monkeypatch.delenv("AIRFLOW_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)  # no stray .env
    config_sources.clear_python_cache()


def _settings(*files: Path, **init: object) -> Settings:
    s: Settings = load_settings(Settings, [str(f) for f in files], **init)
    return s


def _yaml(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "c.yaml"
    path.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")
    return path


# ------------------------------------------------------------- 1. precedence per source


def test_env_overrides_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    f = _yaml(tmp_path, "ai:\n  model: file-model\n")
    monkeypatch.setenv("AUTOAIR_AI__MODEL", "env-model")
    s = _settings(f)
    assert s.ai.model == "env-model"
    assert s.origin_of("ai.model") == "env"
    monkeypatch.delenv("AUTOAIR_AI__MODEL")
    s = _settings(f)
    assert s.ai.model == "file-model"
    assert s.origin_of("ai.model") == f"file:{f}"


def test_init_beats_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOAIR_AI__MODEL", "env-model")
    s = _settings(ai={"model": "init-model"})
    assert s.ai.model == "init-model"
    assert s.origin_of("ai.model") == "init"


def test_dotenv(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("AUTOAIR_AI__MODEL=from-dotenv\n", encoding="utf-8")
    s = _settings()
    assert s.ai.model == "from-dotenv"
    assert s.origin_of("ai.model") == "dotenv"


# ------------------------------------------------------------- 2. Python config files


def test_python_config_with_dataclass_and_postponed_annotations(tmp_path: Path) -> None:
    f = tmp_path / "dc_conf.py"
    f.write_text(
        "from __future__ import annotations\n"
        "from dataclasses import dataclass\n"
        "@dataclass\n"
        "class X:\n"
        "    a: int = 1\n"
        "AUTOAIR_CONFIG = {'subfolder': 'fromdc', 'n': X().a}\n",
        encoding="utf-8",
    )
    assert load_config_file(str(f)) == {"subfolder": "fromdc", "n": 1}
    assert not [m for m in sys.modules if m.startswith("_autoair_config_")]


def test_python_config_file_is_cached_until_it_changes(tmp_path: Path) -> None:
    counter = tmp_path / "count.txt"
    f = tmp_path / "conf.py"
    f.write_text(
        "from pathlib import Path\n"
        f"p = Path({str(counter)!r})\n"
        "p.write_text(str(int(p.read_text()) + 1) if p.exists() else '1')\n"
        "AUTOAIR_CONFIG = {'subfolder': 'x', 'nested': {'a': 1}}\n",
        encoding="utf-8",
    )
    first = load_config_file(str(f))
    first["nested"]["a"] = 99  # callers cannot corrupt the cached module's values
    assert load_config_file(str(f)) == {"subfolder": "x", "nested": {"a": 1}}
    assert counter.read_text() == "1"
    f.write_text(f.read_text() + "AUTOAIR_CONFIG['subfolder'] = 'y'\n", encoding="utf-8")
    assert load_config_file(str(f))["subfolder"] == "y"
    assert counter.read_text() == "2"


# ------------------------------------------------------------- 3. module references


def test_module_reference_with_suffix_like_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pkg = tmp_path / "pkgroot" / "acmecfgpkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "cfg.py").write_text("AUTOAIR_CONFIG = {'subfolder': 'mod'}\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path / "pkgroot"))
    assert load_config_file("acmecfgpkg.cfg") == {"subfolder": "mod"}
    assert load_config_file("acmecfgpkg.cfg:AUTOAIR_CONFIG") == {"subfolder": "mod"}
    with pytest.raises(ConfigError, match="file not found"):
        load_config_file("nosuchpkg_xyz.cfg")


# ------------------------------------------------------------- 4. credential masking


def test_url_userinfo_is_masked() -> None:
    assert mask_url_credentials("postgresql://u:p@db/x") == "postgresql://u:***@db/x"
    assert mask_url_credentials("https://ghp_abc@github.com/o/r") == "https://***@github.com/o/r"
    assert mask_url_credentials("ssh://git@github.com/o/r") == "ssh://git@github.com/o/r"
    assert mask_url_credentials("no url here") == "no url here"


def test_config_masking_hides_url_credentials_and_option_keys() -> None:
    storage = StorageSettings(
        mode="object",
        object_url="s3://AKIA:topsecret@bucket/dags",
        storage_options={"key": "AKIA", "secret": "s3cr3t", "client_kwargs": {"region": "eu"}},
    )
    out = masked(storage)
    assert "topsecret" not in str(out)
    assert out["object_url"] == "s3://AKIA:***@bucket/dags"
    s = Settings(
        storage=storage,
        team_storage={"t": storage},
        ai={"memory_url": "postgresql://u:pw@h/db"},  # type: ignore[arg-type]
    )
    shown = masked(s)
    text = str(shown)
    assert "topsecret" not in text
    assert "s3cr3t" not in text
    assert ":pw@" not in text


def test_api_config_masks_dsn_in_both_modes(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from autoair.api.app import build_services, create_app
    from autoair.auth import AuthBackend

    for mode in ("true", "non-sensitive-only"):
        s = Settings(
            dags_folder=tmp_path,
            expose_config=mode,  # type: ignore[arg-type]
            ai={"memory_url": "postgresql://u:pw@h/db"},  # type: ignore[arg-type]
            storage={"mode": "object", "object_url": "memory://u:secretpw@b/dags"},  # type: ignore[arg-type]
            subfolder="",
        )
        client = TestClient(create_app(build_services(s, AuthBackend())))
        res = client.get("/api/config")
        assert res.status_code == 200, res.text
        assert "secretpw" not in res.text
        assert ":pw@" not in res.text


def test_scrub_secrets_masks_storage_options_recursively() -> None:
    storage = StorageSettings(
        mode="object",
        object_url="s3://bucket/dags",
        storage_options={
            "key": "AKIA",
            "secret": "s3cr3t",
            "token": "t",
            "aws_secret_access_key": "x",
            "account_key": "k",
            "sas_token": "sas",
            "connection_string": "cs",
            "endpoint_url": "https://minio",
            "client_kwargs": {"region_name": "eu", "client_secret": "cs2"},
        },
    )
    safe = scrub_secrets(storage).storage_options
    for k in ("key", "secret", "token", "aws_secret_access_key", "account_key", "sas_token"):
        assert safe[k] == MASK
    assert safe["connection_string"] == MASK
    assert safe["endpoint_url"] == "https://minio"
    assert safe["client_kwargs"] == {"region_name": "eu", "client_secret": MASK}


def test_deploy_target_store_never_persists_secrets(tmp_path: Path) -> None:
    configured = StorageSettings(
        mode="object", object_url="memory://b/dags", storage_options={"secret": "s3cr3t"}
    )
    store = DeployTargetStore(tmp_path / "deploy-target.json")
    store.save(configured.model_copy(update={"storage_options": {"secret": "s3cr3t", "a": 1}}))
    raw = store.path.read_text()
    assert "s3cr3t" not in raw
    assert MASK in raw
    loaded = store.load(configured)
    assert loaded is not None
    assert loaded.storage_options == {"secret": "s3cr3t", "a": 1}  # reference resolved
    unresolved = store.load(None)
    assert unresolved is not None
    assert unresolved.storage_options == {"a": 1}


def test_restore_and_unreferenced_secrets() -> None:
    configured = StorageSettings(storage_options={"key": "k1", "endpoint_url": "e"})
    roundtrip = scrub_secrets(configured)
    assert unreferenced_secrets(roundtrip, configured) == []
    assert restore_secrets(roundtrip, configured).storage_options == configured.storage_options
    new = StorageSettings(storage_options={"key": "other"})
    assert unreferenced_secrets(new, configured) == ["key"]


def test_deploy_target_api_does_not_leak_storage_options(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from autoair.api.app import build_services, create_app
    from autoair.auth import AuthBackend

    s = Settings(
        dags_folder=tmp_path,
        storage={  # type: ignore[arg-type]
            "mode": "object",
            "object_url": "memory://bucket/dags",
            "storage_options": {"secret": "s3cr3t-value"},
        },
        subfolder="",
    )
    client = TestClient(create_app(build_services(s, AuthBackend())))
    res = client.get("/api/deploy-target")
    assert res.status_code == 200, res.text
    assert "s3cr3t-value" not in res.text
    assert res.json()["settings"]["storage_options"]["secret"] == MASK
    body = res.json()["settings"]
    # Round trip of the masked target keeps the configured credential (not persisted).
    put = client.put("/api/deploy-target", json=body)
    assert put.status_code == 200, put.text
    saved = (s.state_dir / "deploy-target.json").read_text()
    assert "s3cr3t-value" not in saved
    body["storage_options"] = {"secret": "new-clear-secret"}
    assert client.put("/api/deploy-target", json=body).status_code == 400


# ------------------------------------------------------------- 5. feature names


def test_unknown_feature_name_raises() -> None:
    s = Settings()
    assert s.feature("deploy") is True
    with pytest.raises(ValueError, match="unknown feature"):
        s.feature("deploi")


# ------------------------------------------------------------- 6. CLI strategy choice


def test_catalog_build_strategy_is_a_choice(tmp_path: Path) -> None:
    from autoair.cli import app

    res = CliRunner().invoke(
        app, ["catalog", "build", "--strategy", "bogus", "-o", str(tmp_path / "c.json")]
    )
    assert res.exit_code != 0
    assert "bogus" in res.output
