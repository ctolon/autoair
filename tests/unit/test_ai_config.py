"""The ``ai`` settings section: descriptions, nested env vars, active limits."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from autoair.ai.config import AiSettings, ai_settings_of
from autoair.ai.llm import build_llm, provider_config
from autoair.settings import Settings

AI_ENV = (
    "AUTOAIR_AI__PROVIDER",
    "AUTOAIR_AI__MODEL",
    "AUTOAIR_AI__RETRIEVAL__TOP_K",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in AI_ENV:
        monkeypatch.delenv(name, raising=False)


def test_defaults_have_descriptions_and_secrets_are_masked() -> None:
    def walk(model: type) -> None:
        for name, info in model.model_fields.items():  # type: ignore[attr-defined]
            assert info.description, f"{model.__name__}.{name} has no description"
            annotation = info.annotation
            if isinstance(annotation, type) and issubclass(annotation, AiSettings.__base__):  # type: ignore[arg-type]
                walk(annotation)

    walk(AiSettings)
    ai = AiSettings(api_key=SecretStr("sk-test-123456"))
    assert "sk-test" not in repr(ai)
    assert ai.retrieval.top_k == 8
    assert ai.research.online is False
    assert ai.llm_mode == "central"


def test_nested_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOAIR_AI__PROVIDER", "openai")
    monkeypatch.setenv("AUTOAIR_AI__RETRIEVAL__TOP_K", "12")
    settings = Settings()
    assert settings.ai.provider == "openai"
    assert settings.ai.retrieval.top_k == 12


def test_ai_settings_of_foreign_objects_and_provider_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Foreign:
        ai = "not a section"

    assert ai_settings_of(Foreign()) == AiSettings()
    assert ai_settings_of(None) == AiSettings()
    cfg = provider_config(Settings(ai=AiSettings(provider="ollama", model="llama3")))
    assert cfg is not None
    assert (cfg.provider, cfg.model) == ("ollama", "llama3")
    assert build_llm(Settings()) is None
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a-key")
    llm = build_llm(Settings(ai=AiSettings(max_output_tokens=2048)))
    assert llm is not None
    assert llm.model == "claude-sonnet-5-5"


def test_active_ai_limits_reach_rules_and_assist() -> None:
    from autoair.ai.config import AssistSettings, activate_ai, active_ai
    from autoair.ai.rules import resource_findings
    from autoair.spec import DagSpec

    spec = DagSpec.model_validate(
        {
            "dag_id": "t",
            "nodes": [
                {
                    "kind": "operator",
                    "id": "a",
                    "task_id": "a",
                    "operator": "airflow.providers.standard.operators.bash.BashOperator",
                    "params": {"bash_command": "echo"},
                }
            ],
        }
    )
    try:
        activate_ai(
            Settings(ai=AiSettings(assist=AssistSettings(default_task_timeout_seconds=600)))
        )
        assert active_ai().assist.default_task_timeout_seconds == 600
        finding = next(f for f in resource_findings(spec) if f.rule == "no-execution-timeout")
        assert finding.patch[0].value == 600
    finally:
        activate_ai(None)
    assert active_ai().assist.default_task_timeout_seconds == 3600
