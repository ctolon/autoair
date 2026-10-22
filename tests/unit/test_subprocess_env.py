"""Processes running DAG code never see AutoAir's, Airflow's or the cloud's secrets."""

from __future__ import annotations

import os
import textwrap

import pytest

from autoair import _subprocess, sections
from autoair.sections import SandboxSettings, ValidatorSettings

SECRETS = {
    "AIRFLOW__DATABASE__SQL_ALCHEMY_CONN": "postgresql://airflow:pw@db/airflow",
    "AIRFLOW__DATABASE__SQL_ALCHEMY_CONN_CMD": "cat /run/secrets/db",
    "AIRFLOW__DATABASE__SQL_ALCHEMY_CONN_SECRET": "airflow/db",
    "AIRFLOW__CORE__FERNET_KEY": "fernet",
    "AIRFLOW__CORE__FERNET_KEY_CMD": "cat /run/secrets/fernet",
    "AIRFLOW__API_AUTH__JWT_SECRET": "jwt",
    "AIRFLOW__API__SECRET_KEY": "flask",
    "AIRFLOW__CELERY__BROKER_URL": "redis://:pw@redis:6379/0",
    "AIRFLOW__CELERY__RESULT_BACKEND": "db+postgresql://airflow:pw@db/airflow",
    "AIRFLOW__SMTP__SMTP_PASSWORD": "smtp",
    "AIRFLOW__SECRETS__BACKEND_KWARGS": '{"token": "vault"}',
    "AIRFLOW_CONN_MY_DB": "postgresql://u:pw@h/db",
    "AIRFLOW_VAR_API_SECRET": "v",
    "AWS_ACCESS_KEY_ID": "AKIA",
    "AWS_SECRET_ACCESS_KEY": "aws",
    "AWS_SESSION_TOKEN": "aws-session",
    "GOOGLE_APPLICATION_CREDENTIALS": "/keys/gcp.json",
    "AZURE_CLIENT_SECRET": "azure",
    "AZURE_STORAGE_CONNECTION_STRING": "DefaultEndpointsProtocol=https;AccountKey=k",
    "VAULT_TOKEN": "vault",
    "GITHUB_TOKEN": "gh",
    "PGPASSWORD": "pg",
    "AUTOAIR_STORAGE__GIT__TOKEN": "t",
}
KEPT = {
    "PATH": "/bin",
    "HOME": "/home/airflow",
    "AIRFLOW_HOME": "/opt/airflow",
    "AIRFLOW__CORE__DAGS_FOLDER": "/opt/airflow/dags",
    "AIRFLOW__CORE__LOAD_EXAMPLES": "False",
    "AIRFLOW__CORE__EXECUTOR": "LocalExecutor",
    "AIRFLOW__DATABASE__SQL_ALCHEMY_POOL_SIZE": "5",
    "AIRFLOW__LOGGING__REMOTE_LOG_CONN_ID": "logs",
    "AWS_DEFAULT_REGION": "eu-west-1",
    "AZURE_TENANT_ID": "tenant",
    "TOKENIZERS_PARALLELISM": "false",
    "PYTHONPATH": "/opt/airflow/plugins",
}


def test_airflow_and_cloud_secrets_are_removed() -> None:
    env = _subprocess.user_code_env(base={**SECRETS, **KEPT})
    assert env == KEPT


def _use(*values: SandboxSettings | ValidatorSettings) -> None:
    for value in values:
        sections._ACTIVE[type(value)] = value  # conftest resets the sections after each test


def test_passthrough_keeps_named_credentials_but_never_autoair_secrets() -> None:
    passthrough = ["AIRFLOW_CONN_MY_*", "airflow_var_api_secret", "AUTOAIR_STORAGE__GIT__TOKEN"]
    _use(SandboxSettings(env_passthrough=passthrough))
    env = _subprocess.user_code_env(base={**SECRETS, **KEPT})
    assert env["AIRFLOW_CONN_MY_DB"] == SECRETS["AIRFLOW_CONN_MY_DB"]
    assert env["AIRFLOW_VAR_API_SECRET"] == "v"
    assert "AUTOAIR_STORAGE__GIT__TOKEN" not in env
    assert "AIRFLOW__CORE__FERNET_KEY" not in env


def test_validator_key_connection_is_never_passed_through() -> None:
    _use(
        SandboxSettings(env_passthrough=["AIRFLOW_CONN_*"]),
        ValidatorSettings(secret_conn_id="autoair_validator"),
    )
    env = _subprocess.user_code_env(
        base={"AIRFLOW_CONN_AUTOAIR_VALIDATOR": "k", "AIRFLOW_CONN_OTHER": "x"}
    )
    assert env == {"AIRFLOW_CONN_OTHER": "x"}


def test_keep_hands_named_credentials_to_one_caller_only() -> None:
    base = {**SECRETS, **KEPT}
    keep = frozenset({"AIRFLOW__DATABASE__SQL_ALCHEMY_CONN", "AUTOAIR_STORAGE__GIT__TOKEN"})
    env = _subprocess.user_code_env(base=base, keep=keep)
    assert (
        env["AIRFLOW__DATABASE__SQL_ALCHEMY_CONN"] == SECRETS["AIRFLOW__DATABASE__SQL_ALCHEMY_CONN"]
    )
    assert "AUTOAIR_STORAGE__GIT__TOKEN" not in env  # AutoAir's own secrets: never


def test_empty_pattern_disables_the_credential_rule() -> None:
    _use(SandboxSettings(credential_env_pattern=""))
    env = _subprocess.user_code_env(base={"AIRFLOW_CONN_X": "x", "AUTOAIR_AI__API_KEY": "k"})
    assert env == {"AIRFLOW_CONN_X": "x"}


def test_validator_worker_env_has_no_airflow_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    from autoair.validation.service import worker_env

    for name, value in SECRETS.items():
        monkeypatch.setenv(name, value)
    env = worker_env(ValidatorSettings())
    assert not set(SECRETS) & set(env)
    assert env["PATH"] == os.environ["PATH"]


# --------------------------------------------------------------------------- real children

_ENV_DAG = textwrap.dedent(
    """
    import os
    from airflow.sdk import DAG, task

    with DAG("env_probe", schedule=None):
        @task
        def probe():
            return sorted(k for k in os.environ if k in {names!r})

        probe()
    """
)


@pytest.mark.airflow
def test_task_test_child_does_not_see_airflow_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    from autoair.runner import run_task_test

    names = [
        "AIRFLOW__CORE__FERNET_KEY",
        "AIRFLOW__API_AUTH__JWT_SECRET",
        "AIRFLOW__DATABASE__SQL_ALCHEMY_CONN_CMD",
        "AIRFLOW_CONN_MY_DB",
        "AWS_SECRET_ACCESS_KEY",
        "AIRFLOW__CORE__LOAD_EXAMPLES",
    ]
    for name in names[:-1]:
        monkeypatch.setenv(name, SECRETS.get(name, "x"))
    monkeypatch.setenv("AIRFLOW__CORE__LOAD_EXAMPLES", "False")
    result = run_task_test(_ENV_DAG.format(names=set(names)), "env_probe", "probe", timeout=120)
    assert result.state == "success", result.log
    assert result.return_value == ["AIRFLOW__CORE__LOAD_EXAMPLES"]


@pytest.mark.airflow
def test_dagbag_import_works_without_the_db_secrets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: os.PathLike[str]
) -> None:
    """The scrubbed child still imports a DAG although the server's environment points
    Airflow at a PostgreSQL metadata DB it may no longer see."""
    from autoair.validation.dagbag_check import check_source

    monkeypatch.setenv("AIRFLOW__DATABASE__SQL_ALCHEMY_CONN", "postgresql://u:pw@nowhere/db")
    monkeypatch.setenv("AIRFLOW__CORE__EXECUTOR", "LocalExecutor")
    monkeypatch.setenv("AIRFLOW__CORE__FERNET_KEY", "x" * 44)
    result = check_source(_ENV_DAG.format(names={"AIRFLOW__CORE__FERNET_KEY"}), use_pool=False)
    assert result.ok, result.import_errors
    assert result.dag_ids == ["env_probe"]


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("AIRFLOW__CELERY__FLOWER_BASIC_AUTH", "u:p"),
        ("AIRFLOW__SENTRY__SENTRY_DSN", "https://k@sentry/1"),
        ("AIRFLOW__DATABASE__SQL_ALCHEMY_ENGINE_ARGS", "{}"),
        ("DB_PASS", "p"),
        ("SNOWFLAKE_PWD", "p"),
        ("SLACK_WEBHOOK_URL", "https://hooks/x"),
        ("CELERY_BROKER_URL", "redis://r"),
        ("REDIS_URL", "redis://:pw@redis:6379/0"),
        ("PIP_INDEX_URL", "https://user:pw@pypi.example/simple"),
    ],
)
def test_more_credential_shapes_are_removed(name: str, value: str) -> None:
    assert _subprocess.user_code_env(base={name: value, "PWD": "/w"}) == {"PWD": "/w"}


def test_git_env_drops_credential_like_git_names() -> None:
    from autoair.storage.git.local_git import git_base_env

    base = {
        "GIT_TOKEN": "t",
        "GIT_PASSWORD": "p",
        "GIT_SSL_CAINFO": "/ca",
        "HTTPS_PROXY": "http://u:pw@proxy:3128",
    }
    assert git_base_env(base=base) == {
        "GIT_SSL_CAINFO": "/ca",
        "HTTPS_PROXY": "http://u:pw@proxy:3128",
    }
    assert git_base_env(["GIT_TOKEN"], base=base)["GIT_TOKEN"] == "t"
