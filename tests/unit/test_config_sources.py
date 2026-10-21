"""Configuration sources: Python/JSON/YAML/INI files, precedence, nested sections, errors."""

from __future__ import annotations

import json
import logging
import sys
import textwrap
from pathlib import Path

import pytest

from autoair import config_sources
from autoair.config_sources import (
    CONFIG_FILE_ENV,
    ConfigError,
    add_config_files,
    configured_files,
    load_config_file,
    load_settings,
    parse_file_list,
)
from autoair.settings import Settings


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in (CONFIG_FILE_ENV, "AIRFLOW__AUTOAIR__CONFIG_FILE", "AUTOAIR_SUBFOLDER"):
        monkeypatch.delenv(name, raising=False)
    # No airflow.cfg pointer from the developer's machine.
    monkeypatch.setenv("AIRFLOW_HOME", str(tmp_path / "airflow-home"))
    monkeypatch.delenv("AIRFLOW_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)  # no stray .env


def settings(*files: Path | str, **init: object) -> Settings:
    s: Settings = load_settings(Settings, [str(f) for f in files], dags_folder=Path("/x"), **init)
    return s


def write(path: Path, text: str) -> Path:
    path.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- formats


def test_yaml_file_with_nested_sections(tmp_path: Path) -> None:
    f = write(
        tmp_path / "c.yaml",
        """
        subfolder: from_yaml
        required_tags: [team, tier]
        features:
          marketplace: false
        storage:
          mode: git
          git:
            mode: push
            pr_labels: [autoair]
        discovery:
          skip_dirs_extra: [build]
        """,
    )
    s = settings(f)
    assert s.subfolder == "from_yaml"
    assert s.required_tags == ["team", "tier"]
    assert s.features.marketplace is False
    assert (s.storage.mode, s.storage.git.mode, s.storage.git.pr_labels) == (
        "git",
        "push",
        ["autoair"],
    )
    assert "build" in s.discovery.skip_dir_names()
    assert s.origin_of("storage.git.mode") == f"file:{f}"
    assert s.origin_of("subfolder") == f"file:{f}"
    assert s.origin_of("team") == "default"


def test_json_file_and_autoair_wrapper_key(tmp_path: Path) -> None:
    f = tmp_path / "c.json"
    f.write_text(json.dumps({"autoair": {"team": "data", "policy": {"dag_id_pattern": "[a-z_]+"}}}))
    s = settings(f)
    assert s.team == "data"
    assert s.policy.dag_id_pattern == "[a-z_]+"


def test_ini_file_sections_dotted_keys_and_json_values(tmp_path: Path) -> None:
    f = write(
        tmp_path / "airflow.cfg",
        """
        [core]
        dags_folder = /ignored

        [autoair]
        subfolder = from_ini
        required_tags = ["a", "b"]
        allow_python_code = false
        git.mode = ignored-here

        [autoair.storage]
        git.branch_prefix = team/

        [autoair.storage.git]
        mode = push
        environments = {"prod": "main"}

        [autoair.features]
        templates = false
        """,
    )
    s = settings(f)
    assert s.subfolder == "from_ini"
    assert s.required_tags == ["a", "b"]
    assert s.allow_python_code is False
    assert s.storage.git.branch_prefix == "team/"
    assert s.storage.git.mode == "push"
    assert s.storage.git.environments == {"prod": "main"}
    assert s.features.templates is False
    assert s.dags_folder == Path("/x")  # [core] is not ours


def test_python_file_dict_callable_and_settings_object(tmp_path: Path) -> None:
    f = write(
        tmp_path / "conf.py",
        """
        from autoair.settings import Settings

        config = {"team": "py-dict"}

        def make():
            return {"team": "py-callable", "codegen": {"line_length": 88}}

        AS_SETTINGS = Settings(team="py-settings", dags_folder="/y")
        """,
    )
    assert settings(f).team == "py-dict"
    s = settings(f"{f}:make")
    assert (s.team, s.codegen.line_length) == ("py-callable", 88)
    s = settings(f"{f}:AS_SETTINGS")
    assert s.team == "py-settings"
    assert s.dags_folder == Path("/x")  # init arguments still win


def test_python_module_reference(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pkg = tmp_path / "acme_conf"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    write(pkg / "autoair_settings.py", "AUTOAIR_CONFIG = {'team': 'module'}\nother = {'team': 'x'}")
    monkeypatch.syspath_prepend(str(tmp_path))
    assert settings("acme_conf.autoair_settings").team == "module"
    assert settings("acme_conf.autoair_settings:other").team == "x"
    sys.modules.pop("acme_conf.autoair_settings", None)


def test_unknown_format_missing_file_and_bad_syntax(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="unknown configuration format"):
        load_config_file(str(write(tmp_path / "c.toml", "a = 1")))
    with pytest.raises(ConfigError, match="not found"):
        load_config_file(str(tmp_path / "missing.yaml"))
    bad = write(tmp_path / "bad.yaml", "a: [1, 2\n")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_config_file(str(bad))
    with pytest.raises(ConfigError, match="top level must be a mapping"):
        load_config_file(str(write(tmp_path / "list.json", "[1, 2]")))
    with pytest.raises(ConfigError, match="no attribute"):
        load_config_file(f"{write(tmp_path / 'c.py', 'x = 1')}:nope")


def test_missing_pyyaml_has_a_clear_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    f = write(tmp_path / "c.yaml", "team: a\n")
    monkeypatch.setitem(sys.modules, "yaml", None)  # import yaml -> ImportError
    with pytest.raises(ConfigError, match=r"autoair\[yaml\]"):
        load_config_file(str(f))


# --------------------------------------------------------------------------- precedence


def test_precedence_init_env_files_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = write(tmp_path / "a.yaml", "subfolder: a\nteam: a\ncors_origins: [a]\n")
    second = write(tmp_path / "b.json", '{"subfolder": "b", "team": "b"}')
    monkeypatch.setenv("AUTOAIR_TEAM", "env")
    s = settings(first, second)
    assert s.subfolder == "b"  # later file wins
    assert s.origin_of("subfolder") == f"file:{second}"
    assert s.cors_origins == ["a"]  # earlier file still contributes
    assert s.team == "env"  # environment beats files
    assert s.origin_of("team") == "env"
    s = settings(first, second, team="init")
    assert s.team == "init"  # init arguments beat everything
    assert s.origin_of("team") == "init"
    assert s.origin_of("expose_config") == "default"


def test_nested_env_overrides_one_key_of_a_file_section(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = write(tmp_path / "c.yaml", "storage:\n  git:\n    mode: push\n    branch_prefix: f/\n")
    monkeypatch.setenv("AUTOAIR_STORAGE__GIT__MODE", "pr")
    s = settings(f)
    assert (s.storage.git.mode, s.storage.git.branch_prefix) == ("pr", "f/")
    assert s.origin_of("storage.git.mode") == "env"
    assert s.origin_of("storage.git.branch_prefix") == f"file:{f}"


def test_config_file_env_comma_and_json_lists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = write(tmp_path / "a.yaml", "team: a\n")
    b = write(tmp_path / "b.yaml", "team: b\n")
    monkeypatch.setenv(CONFIG_FILE_ENV, f"{a}, {b}")
    assert Settings(dags_folder="/x").team == "b"
    assert Settings(dags_folder="/x").config_file == [str(a), str(b)]
    monkeypatch.setenv(CONFIG_FILE_ENV, json.dumps([str(b), str(a)]))
    assert Settings(dags_folder="/x").team == "a"
    assert parse_file_list(" ") == []
    with pytest.raises(ConfigError):
        parse_file_list("[not json")


def test_airflow_cfg_config_file_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "airflow-home"
    home.mkdir()
    conf = write(tmp_path / "autoair.yaml", "team: from-airflow-cfg\n")
    write(home / "airflow.cfg", f"[autoair]\nconfig_file = {conf}\n")
    monkeypatch.setitem(sys.modules, "airflow.configuration", None)  # read the file itself
    assert configured_files() == [str(conf)]
    assert Settings(dags_folder="/x").team == "from-airflow-cfg"
    later = write(tmp_path / "later.yaml", "team: env-file\n")
    monkeypatch.setenv(CONFIG_FILE_ENV, str(later))
    assert configured_files() == [str(conf), str(later)]  # AUTOAIR_CONFIG_FILE comes later
    assert Settings(dags_folder="/x").team == "env-file"
    monkeypatch.setenv("AIRFLOW__AUTOAIR__CONFIG_FILE", str(later))
    assert configured_files() == [str(later)]


def test_add_config_files_appends_to_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CONFIG_FILE_ENV, "a.yaml")
    add_config_files(["b.yaml", "a.yaml"])
    assert parse_file_list(config_sources.os.environ[CONFIG_FILE_ENV]) == ["a.yaml", "b.yaml"]


# --------------------------------------------------------------------------- errors


def test_validation_errors_name_the_file_and_key(tmp_path: Path) -> None:
    f = write(tmp_path / "c.yaml", "storage:\n  git:\n    mode: sideways\n")
    with pytest.raises(ConfigError) as exc:
        settings(f)
    message = str(exc.value)
    assert "storage.git.mode" in message
    assert str(f) in message
    bad_ini = write(tmp_path / "c.ini", "[autoair.features]\nnot_a_feature = true\n")
    with pytest.raises(ConfigError, match=rf"features.not_a_feature.*{bad_ini.name}"):
        settings(bad_ini)
    regex = write(tmp_path / "r.json", '{"discovery": {"skip_module_patterns_extra": ["("]}}')
    with pytest.raises(ConfigError, match=r"discovery\.skip_module_patterns_extra"):
        settings(regex)


def test_env_errors_name_the_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOAIR_STORAGE__GIT__MODE", "sideways")
    with pytest.raises(ConfigError, match="AUTOAIR_STORAGE__GIT__MODE"):
        Settings(dags_folder="/x")


def test_unknown_keys_are_logged_and_ignored(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    f = write(tmp_path / "c.yaml", "no_such_setting: 1\nfeatures:\n  collab: false\n")
    with caplog.at_level(logging.WARNING, logger="autoair.config_sources"):
        s = settings(f)
    assert "no_such_setting" in caplog.text
    assert s.effective_features()["collab"] is False


def test_expose_config_accepts_booleans(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOAIR_EXPOSE_CONFIG", "True")
    assert Settings(dags_folder="/x").expose_config == "true"
    monkeypatch.setenv("AUTOAIR_EXPOSE_CONFIG", "non_sensitive_only")
    assert Settings(dags_folder="/x").expose_config == "non-sensitive-only"
    assert Settings(dags_folder="/x", expose_config=False).expose_config == "false"


def test_catalog_source_settings_read_config_files(tmp_path: Path) -> None:
    from autoair.catalog.discovery import CatalogSourceSettings

    f = write(
        tmp_path / "c.yaml",
        "extra_operator_distributions: [acme-airflow-ops]\n"
        "discovery:\n  skip_dirs_extra: [build]\n",
    )
    s = load_settings(CatalogSourceSettings, [str(f)])
    assert s.extra_operator_distributions == ["acme-airflow-ops"]
    assert "build" in s.discovery.skip_dirs_extra


def test_empty_env_and_dotenv_values_count_as_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autoair.catalog.discovery import CatalogSourceSettings

    f = write(tmp_path / "c.yaml", "team: from-file\nstorage:\n  git:\n    mode: push\n")
    # Enum, path, nested and plain values set to "" in the environment (docker-compose
    # `AUTOAIR_X: ${UNSET:-}` style) and in .env: defaults / file values apply.
    monkeypatch.setenv("AUTOAIR_AI__PROVIDER", "")
    monkeypatch.setenv("AUTOAIR_OUTPUT_DIR", "")
    monkeypatch.setenv("AUTOAIR_TEAM", "")
    monkeypatch.setenv("AUTOAIR_STORAGE__GIT__MODE", "")
    (tmp_path / ".env").write_text(
        "AUTOAIR_STATE_DIR=\nAUTOAIR_STORAGE__MODE=\nAUTOAIR_EXTRA_OPERATOR_PATHS=\n",
        encoding="utf-8",
    )
    s = settings(f)
    default = settings()
    assert s.ai.provider == default.ai.provider
    assert s.output_dir == default.output_dir != Path(".")
    assert s.state_dir == default.state_dir != Path(".")
    assert s.storage.mode == "local"
    assert s.team == "from-file"  # an empty variable does not shadow the file
    assert s.origin_of("team") == f"file:{f}"
    assert s.storage.git.mode == "push"
    assert load_settings(CatalogSourceSettings, []).extra_operator_paths == []


def test_non_empty_env_values_still_apply(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOAIR_STORAGE__GIT__MODE", "push")
    monkeypatch.setenv("AUTOAIR_AI__PROVIDER", "ollama")
    s = settings()
    assert (s.storage.git.mode, s.ai.provider) == ("push", "ollama")
