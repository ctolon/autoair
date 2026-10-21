from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from autoair.kb.components import resolve_versions
from autoair.kb.config import KbSettings
from autoair.settings import Settings


def test_defaults_are_air_gapped_and_everything_sensible() -> None:
    kb = KbSettings()
    assert kb.online is False
    assert kb.store == "local"
    assert kb.providers == "installed"
    assert kb.components == [
        "core",
        "task_sdk",
        "providers",
        "helm_chart",
        "airflowctl",
        "astronomer_airflow",
    ]
    assert [s.id for s in kb.sources] == ["official"]
    assert kb.fetch.include_api_reference is False
    assert Settings().feature("kb") is True


def test_environment_configures_sources(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AUTOAIR_KB__ONLINE", "true")
    monkeypatch.setenv("AUTOAIR_KB__STORE", "pgvector")
    monkeypatch.setenv(
        "AUTOAIR_KB__SOURCES",
        json.dumps(
            [
                {
                    "id": "mirror",
                    "kind": "git",
                    "url": "https://git.corp/a.git",
                    "conn_id": "kb_git",
                    "ref": "3.1.0",
                    "layout": "airflow_repo",
                }
            ]
        ),
    )
    monkeypatch.setenv("AUTOAIR_FEATURES__KB", "false")
    s = Settings(dags_folder=tmp_path)
    assert s.kb.online
    assert s.kb.store == "pgvector"
    assert s.kb.sources[0].conn_id == "kb_git"
    assert s.feature("kb") is False


@pytest.mark.parametrize(
    "bad",
    [
        {"sources": [{"id": "a", "kind": "official"}, {"id": "a", "kind": "official"}]},
        {"sources": [{"id": "s", "kind": "site"}]},
        {"sources": [{"id": "s", "kind": "git"}]},
        {"sources": [{"id": "s", "kind": "site", "url": "https://u:p@mirror/docs/"}]},
        {"sources": [{"id": "Bad Id", "kind": "official"}]},
        {"pgvector": {"url": "postgresql://user:secret@db/kb"}},
        {"elasticsearch": {"url": "https://elastic:pw@es:9200"}},
        {"chunk": {"size": 400, "overlap": 300}},
        {"components": ["nope"]},
    ],
)
def test_invalid_settings_are_rejected(bad: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        KbSettings.model_validate(bad)


def test_version_keywords() -> None:
    assert resolve_versions(["installed"], "3.1.0") == [("3.1.0", True)]
    assert resolve_versions(["installed"], None) == [("stable", False)]
    assert resolve_versions(["installed", "3.1.0", "stable"], "3.1.0") == [
        ("3.1.0", True),
        ("stable", False),
    ]
