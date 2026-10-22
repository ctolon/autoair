"""Security / robustness regressions that need no Airflow: validators, regexes, caches."""

from __future__ import annotations

import asyncio
import time

import httpx2
import pytest

from autoair import _subprocess
from autoair.catalog import service
from autoair.codegen.yaml_dag import load_yaml
from autoair.gateway import AirflowError, AirflowGateway
from autoair.storage.base import StorageError, check_dag_id, check_files, check_version
from autoair.storage.git.provider import check_branch
from autoair.templates import TemplateError, TemplateStore
from autoair.validation import _SECRET_ASSIGN


# --------------------------------------------------------------------------- regex DoS
@pytest.mark.parametrize(
    "text",
    ["\n" * 50_000, " \n" * 50_000, "password" * 20_000, "x = '" + "a" * 100_000],
)
def test_secret_assign_regex_is_linear(text):
    start = time.perf_counter()
    _SECRET_ASSIGN.search(text)
    assert time.perf_counter() - start < 0.5


@pytest.mark.parametrize(
    "line",
    [
        'export API_TOKEN="0123456789abc"',
        "    token = 'abcdef123456'",
        "self.config['db_password'] = 'hunter2hunter2'",
    ],
)
def test_secret_assign_regex_still_detects(line):
    assert _SECRET_ASSIGN.search(f"import os\n{line}\n")


@pytest.mark.parametrize(
    "line",
    [
        'conn = Connection(conn_id="db", password="hunter2hunter2")',
        'hook.run(sql, api_key="sk-0123456789")',
        "x = 1; token = 'abcdef123456'",
        "cmd = 'mysql --password=\"hunter2hunter2\"'",
    ],
)
def test_secret_assign_regex_detects_inline_assignments(line):
    assert _SECRET_ASSIGN.search(f"import os\n{line}\n")


@pytest.mark.parametrize(
    "line",
    [
        'conn = BaseHook.get_connection("db"); pw = conn.password',
        'password = os.environ["DB_PASSWORD"]',
        'hook = Hook(password=Variable.get("db_password"))',
        'if token == "abcdef123456":',
        'if token != "abcdef123456":',
        'token = f"Bearer {conn.password}"',
        'run(password="{{ conn.db.password }}")',
        'call(token="")',
        "secret_ref = SECRET",
        'label = "Enter your password here"',
        'mypasswordless_flag = "yes"',
    ],
)
def test_secret_assign_regex_no_false_positives_on_common_code(line):
    assert not _SECRET_ASSIGN.search(f"import os\n{line}\n")


def test_secret_assign_regex_ignores_templates_and_short_values():
    assert not _SECRET_ASSIGN.search("token = '{{ var.value.t }}'")
    assert not _SECRET_ASSIGN.search("token = 'abc'")


# --------------------------------------------------------------------------- validators
@pytest.mark.parametrize("bad", ["abc\n", "..", "a/b", ""])
def test_dag_id_validator_rejects_trailing_newline_and_traversal(bad):
    with pytest.raises(StorageError):
        check_dag_id(bad)


def test_version_and_file_validators_use_fullmatch():
    with pytest.raises(StorageError):
        check_version("abc\n")
    with pytest.raises(StorageError):
        check_files("d", {"d.py\n": "x"})


def test_branch_validator_rejects_trailing_newline():
    assert check_branch("main") == "main"
    with pytest.raises(StorageError):
        check_branch("main\n")


def test_template_id_rejects_trailing_newline(tmp_path):
    store = TemplateStore(str(tmp_path))
    with pytest.raises(TemplateError):
        store.get("abc\n")


# --------------------------------------------------------------------------- YAML
def test_yaml_alias_bomb_is_rejected():
    bomb = "a: &a [x, x, x, x, x, x, x, x, x, x]\n"
    prev = "a"
    for name in "bcdefghij":
        bomb += f"{name}: &{name} [" + ", ".join([f"*{prev}"] * 10) + "]\n"
        prev = name
    with pytest.raises(ValueError, match="expands to more than"):
        load_yaml(bomb)


def test_yaml_recursive_alias_is_rejected():
    with pytest.raises(ValueError, match="recursive aliases"):
        load_yaml("a: &a [*a]\n")


def test_yaml_normal_document_loads():
    assert load_yaml("dag_id: x\ntasks: [{id: a}]\n") == {"dag_id": "x", "tasks": [{"id": "a"}]}


# --------------------------------------------------------------------------- subprocess env
def test_user_code_env_drops_autoair_secrets():
    env = _subprocess.user_code_env(
        {"EXTRA": "1"},
        base={
            "PATH": "/bin",
            "AIRFLOW_HOME": "/af",
            "AUTOAIR_STORAGE__GIT__TOKEN": "t",
            "AUTOAIR_COLLAB_SECRET": "s",
            "AUTOAIR_ANTHROPIC_API_KEY": "k",
            "AUTOAIR_AI__API_KEY": "k",
            "AUTOAIR_STORAGE__GIT__GITHUB_PRIVATE_KEY": "pem",
            "ANTHROPIC_API_KEY": "k",
            "OPENAI_API_KEY": "k",
            "AUTOAIR_ENVIRONMENT": "prod",
            "AUTOAIR_DAGBAG_POOL": "1",
        },
    )
    assert env == {
        "PATH": "/bin",
        "AIRFLOW_HOME": "/af",
        "AUTOAIR_ENVIRONMENT": "prod",
        "AUTOAIR_DAGBAG_POOL": "1",
        "EXTRA": "1",
    }


# --------------------------------------------------------------------------- catalog cache
def test_catalog_cache_key_tracks_catalog_logic(monkeypatch):
    service.logic_fingerprint.cache_clear()
    try:
        names = {name for name, _ in service._logic_files()}
        assert "autoair/catalog/static.py" in names
        assert "autoair/catalog/service.py" in names
        assert "autoair/icons.py" in names
        assert "autoair/resources/logos/manifest.json" in names
        first = service.logic_fingerprint()
        assert service.logic_fingerprint() == first  # memoised per process

        files = service._logic_files()
        changed = [(n, (lambda: b"changed") if n.endswith("static.py") else r) for n, r in files]
        monkeypatch.setattr(service, "_logic_files", lambda: changed)
        service.logic_fingerprint.cache_clear()
        assert service.logic_fingerprint() != first
    finally:
        service.logic_fingerprint.cache_clear()


def test_stale_logic_cache_is_not_served(tmp_path, monkeypatch):
    cat = service.OperatorCatalog([], cache_dir=tmp_path, use_cache=True)
    service.logic_fingerprint.cache_clear()
    monkeypatch.setattr(service, "logic_fingerprint", lambda: "old-logic")
    old_parts = cat.cache_key_parts()
    assert old_parts["logic"] == "old-logic"
    monkeypatch.setattr(service, "logic_fingerprint", lambda: "new-logic")
    assert cat._key(cat.cache_key_parts()) != cat._key(old_parts)
    assert not service._compatible(old_parts, cat.cache_key_parts())


# --------------------------------------------------------------------------- gateway
def _recording_gateway(seen: list[httpx2.Request]) -> AirflowGateway:
    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        path = request.url.raw_path.decode()
        if "/xcomEntries/" in path:
            key = path.rsplit("/", 1)[-1].split("?")[0]
            return httpx2.Response(200, json={"value": key})
        if path.endswith("/xcomEntries") or "/xcomEntries?" in path:
            entries = [{"key": f"k{i}", "map_index": -1} for i in range(20)]
            return httpx2.Response(200, json={"xcom_entries": entries})
        if "/dagRuns/" in path:
            return httpx2.Response(200, json={"dag_run_id": "r", "state": "success"})
        return httpx2.Response(404, json={"detail": "nope"})

    return AirflowGateway("http://af", transport=httpx2.MockTransport(handler))


def test_gateway_encodes_path_segments():
    asyncio.run(_test_gateway_encodes_path_segments())


async def _test_gateway_encodes_path_segments():
    seen: list[httpx2.Request] = []
    async with _recording_gateway(seen) as gw:
        await gw.dag_run("my/dag", "manual__2024-01-01T00:00:00+00:00?x=1#f")
        with pytest.raises(AirflowError):
            await gw.dag_status("..")
    path = seen[0].url.raw_path.decode()
    assert (
        path
        == "/api/v2/dags/my%2Fdag/dagRuns/manual__2024-01-01T00%3A00%3A00%2B00%3A00%3Fx%3D1%23f"
    )


def test_gateway_xcom_values_fetched_concurrently_in_order():
    asyncio.run(_test_gateway_xcom_values_fetched_concurrently_in_order())


async def _test_gateway_xcom_values_fetched_concurrently_in_order():
    seen: list[httpx2.Request] = []
    async with _recording_gateway(seen) as gw:
        values = await gw.xcom_entries("d", "r", "t")
    assert [v.key for v in values] == [f"k{i}" for i in range(20)]
    assert [v.value for v in values] == [f"k{i}" for i in range(20)]


def test_gateway_dag_run_missing_is_none():
    asyncio.run(_test_gateway_dag_run_missing_is_none())


async def _test_gateway_dag_run_missing_is_none():
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(404, json={"detail": "not found"})

    async with AirflowGateway("http://af", transport=httpx2.MockTransport(handler)) as gw:
        assert await gw.dag_run("d", "r") is None


# --------------------------------------------------------------------------- dagbag pool
def test_pool_settings_from_configure_and_env(monkeypatch):
    from autoair.validation import pool

    monkeypatch.setattr(pool, "_config", {})
    monkeypatch.delenv("AUTOAIR_DAGBAG_POOL", raising=False)
    monkeypatch.delenv("AUTOAIR_DAGBAG_POOL_SIZE", raising=False)
    pool.configure_pool(enabled=False, size=5)
    assert pool.pool_enabled() is False
    assert pool._int_setting("size", "AUTOAIR_DAGBAG_POOL_SIZE", 2) == 5
    monkeypatch.setenv("AUTOAIR_DAGBAG_POOL", "1")  # explicit variable wins
    assert pool.pool_enabled() is True
    monkeypatch.setenv("AUTOAIR_DAGBAG_POOL_SIZE", "nonsense")
    assert pool._int_setting("size", "AUTOAIR_DAGBAG_POOL_SIZE", 2) == 2
