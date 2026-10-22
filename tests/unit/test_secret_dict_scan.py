"""Hard-coded secrets in dict literals and ``dict()`` calls (``{"token": "..."}``): the
assignment patterns over the text miss them because a dict entry has no ``=``."""

from __future__ import annotations

import pytest

from autoair.spec import DagSpec
from autoair.validation import DEFAULT_POLICY, Policy, _hardcoded_secrets, secret_dict_entries
from autoair.validation.raw import _secrets

MUST_FIND = [
    'headers = {"token": "ghp_0123456789abcdef"}',
    "conf = {'password': 'hunter2hunter2'}",
    'payload = {"api_key": "sk-0123456789"}',
    'payload = {"API-KEY": "sk-0123456789"}',
    'auth = {"user": "svc", "client_secret": "s3cr3t-value"}',
    'opts = dict(password="hunter2hunter2")',
    'opts = dict(access_key="AKIAIOSFODNN7EXAMPLE")',
    'cfg = {"nested": {"private_key": "MIIEvQIBADANBgkq"}}',
    'call(extra={"token": "abcdef123456"})',
    'x = {"token": "abc" "def123"}',  # implicit concatenation is one literal
    'aws = {"aws_secret_access_key": "AKIAIOSFODNN7EXAMPLE"}',
]

MUST_NOT_FIND = [
    'headers = {"token": "{{ var.value.api_token }}"}',
    'conf = {"password": "{{ conn.db.password }}"}',
    'conf = {"password": os.environ["DB_PASSWORD"]}',
    'conf = {"password": os.environ.get("DB_PASSWORD", "")}',
    'conf = {"password": Variable.get("db_password")}',
    'conf = {"password": conn.password}',
    'conf = {"password": password}',
    'conf = {"token": ""}',
    'conf = {"token": None}',
    'conf = {"token": "abc"}',  # too short to be a credential
    'conf = {"token": f"Bearer {token}"}',
    'conf = {"token": "Bearer " + token}',
    'conf = {"token": "<your-token>"}',
    'conf = {"password": "changeme"}',
    'conf = {"password": "********"}',
    'conf = {"password": "xxxxxxxx"}',
    'conf = {"api_key": "YOUR_API_KEY"}',
    'conf = {"token_env": "GITHUB_TOKEN"}',
    'conf = {"token": "GITHUB_TOKEN"}',  # names the environment variable
    'conf = {"token": "${GITHUB_TOKEN}"}',
    'conf = {"password": "%(password)s"}',
    'conf = {"password": "placeholder"}',
    'conf = {"password_conn_id": "postgres_default"}',
    'conf = {"token_url": "https://login.example.com/oauth2/token"}',
    'conf = {"secret_name": "prod/db/password"}',
    'conf = {"label": "Enter your password here"}',
    'conf = {"description": "the API token of the service"}',
    'conf = {"retries": 3, "owner": "data"}',
    'conf = {1: "abcdef123456"}',
    'conf = {**other, "name": "abcdef123456"}',
    "opts = dict(password=password)",
    'opts = dict(token=os.getenv("TOKEN"))',
    'opts = dict(token="{{ var.value.t }}")',
    'opts = make(password="")',
    'if conf["token"] == "abcdef123456": pass',
    "this is not python {",
]


@pytest.mark.parametrize("line", MUST_FIND)
def test_finds_secrets_in_dicts(line: str) -> None:
    found = secret_dict_entries(f"import os\n{line}\n")
    assert found, line
    assert all(n == 2 for n, _ in found)


@pytest.mark.parametrize("line", MUST_NOT_FIND)
def test_no_false_positives_on_common_code(line: str) -> None:
    assert secret_dict_entries(f"import os\n{line}\n") == [], line


def test_multi_line_dicts_report_the_key_line() -> None:
    code = 'cfg = {\n    "user": "svc",\n    "token": "ghp_0123456789abcdef",\n}\n'
    assert secret_dict_entries(code) == [(3, "token")]


def test_policy_patterns_apply() -> None:
    policy = Policy(secret_param_patterns=("^pin$",))
    assert secret_dict_entries('x = {"pin": "12345678"}', policy) == [(1, "pin")]
    assert secret_dict_entries('x = {"token": "ghp_0123456789abcdef"}', policy) == []
    off = Policy(secret_param_patterns=())
    assert secret_dict_entries('x = {"token": "ghp_0123456789abcdef"}', off) == []


def test_raw_code_and_preserved_files_report_dict_secrets_as_warnings() -> None:
    code = 'import os\nHEADERS = {"token": "ghp_0123456789abcdef"}\n'
    problems = _secrets(code, DEFAULT_POLICY)
    assert [(p.code, p.severity, p.line) for p in problems] == [("secret", "warning", 2)]
    assert "'token'" in problems[0].message
    # one finding per line, also when the assignment pattern matches it too
    both = 'opts = dict(password="hunter2hunter2")\n'
    assert len(_secrets(both, DEFAULT_POLICY)) == 1


def test_builder_python_task_code_reports_dict_secrets() -> None:
    spec = DagSpec.model_validate(
        {
            "dag_id": "d",
            "nodes": [
                {
                    "kind": "python_task",
                    "id": "t",
                    "task_id": "t",
                    "function_name": "call",
                    "code": 'headers = {"token": "ghp_0123456789abcdef"}\nreturn headers',
                }
            ],
        }
    )
    problems = _hardcoded_secrets(spec)
    assert [(p.code, p.severity, p.node_id, p.field) for p in problems] == [
        ("secret", "warning", "t", "code")
    ]
    clean = spec.model_copy(deep=True)
    clean.nodes[0].code = 'headers = {"token": Variable.get("t")}\nreturn headers'  # type: ignore[union-attr]
    assert _hardcoded_secrets(clean) == []
