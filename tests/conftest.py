from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).parent
GOLDEN_SPECS = sorted((ROOT / "golden" / "specs").glob("*.autoair.json"))

# A private AIRFLOW_HOME per test session: never the shared demo's .airflow-dev.
os.environ.setdefault("AIRFLOW_HOME", tempfile.mkdtemp(prefix="autoair-tests-airflow-"))


def airflow_installed() -> bool:
    try:
        import airflow.sdk  # noqa: F401
    except ImportError:
        return False
    return True


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if airflow_installed():
        return
    skip = pytest.mark.skip(reason="apache-airflow not installed")
    for item in items:
        if "airflow" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _stop_kb_schedulers():
    """End the knowledge-base schedulers a test's apps started, so none keeps running (and
    logging into captured CLI output) during later tests."""
    yield
    from autoair.kb.scheduler import stop_all

    stop_all()


@pytest.fixture
def golden_spec_data() -> dict[str, object]:
    return json.loads((ROOT / "golden" / "specs" / "etl_full.autoair.json").read_text())


@pytest.fixture
def simple_spec_data() -> dict[str, object]:
    return {
        "dag_id": "simple",
        "nodes": [
            {
                "kind": "operator",
                "id": "a",
                "task_id": "a",
                "operator": "airflow.providers.standard.operators.empty.EmptyOperator",
            },
            {
                "kind": "operator",
                "id": "b",
                "task_id": "b",
                "operator": "airflow.providers.standard.operators.bash.BashOperator",
                "params": {"bash_command": "echo hi"},
            },
        ],
        "edges": [{"source": "a", "target": "b"}],
    }


@pytest.fixture(autouse=True)
def _reset_process_config() -> object:
    """Every test starts and ends with the default process-wide configuration (activated
    sections, discovery filters, active AI section): no leaks between tests."""
    from autoair import sections

    sections.reset_all()
    yield
    sections.reset_all()


@pytest.fixture(autouse=True)
def _anonymous_callers_edit() -> Iterator[None]:
    """Most API tests build apps with ``AuthBackend()`` (no authentication) and act as an
    editor. ``iam.anonymous_roles`` defaults to Viewer, so tests opt back into Admin here;
    tests about the default pass ``iam`` explicitly (an explicit value beats the env).

    A private MonkeyPatch, not the ``monkeypatch`` fixture: requesting that here would set it
    up before ``_reset_process_config``, so a test's own patches (``importlib``) would still
    be active while that fixture's teardown runs."""
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("AUTOAIR_IAM__ANONYMOUS_ROLES", '["Admin"]')
        yield
