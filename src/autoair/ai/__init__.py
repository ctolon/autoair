"""AutoAir AI: DAG generation, review / leak scan / optimise / fix features, retrieval
(RAG), research, writers, guardrails, memory and tool-using agents on pydantic-ai.

Everything is imported lazily (PEP 562): ``autoair.settings`` imports
:mod:`autoair.ai.config` at start-up, and the plugin / server must start without the
optional ``autoair[ai]`` extra (pydantic-ai).
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from autoair.ai.findings import Finding, FindingsReport, PatchOp, apply_findings, apply_patch
    from autoair.ai.generator import (
        GenerationResult,
        describe_dag,
        explain_problems,
        generate_spec,
        rank_operators,
        select_operators,
    )
    from autoair.ai.llm import (
        DEFAULT_MODEL,
        LLM,
        ChatMessage,
        LLMError,
        ModelConfig,
        build_embedder,
        build_llm,
        provider_config,
    )
    from autoair.ai.problems import SpecCheck, SpecProblem, check_spec
    from autoair.ai.usage import Usage, track_usage

_EXPORTS: dict[str, str] = {
    "Finding": "autoair.ai.findings",
    "FindingsReport": "autoair.ai.findings",
    "PatchOp": "autoair.ai.findings",
    "apply_findings": "autoair.ai.findings",
    "apply_patch": "autoair.ai.findings",
    "GenerationResult": "autoair.ai.generator",
    "describe_dag": "autoair.ai.generator",
    "explain_problems": "autoair.ai.generator",
    "generate_spec": "autoair.ai.generator",
    "rank_operators": "autoair.ai.generator",
    "select_operators": "autoair.ai.generator",
    "DEFAULT_MODEL": "autoair.ai.llm",
    "LLM": "autoair.ai.llm",
    "ChatMessage": "autoair.ai.llm",
    "LLMError": "autoair.ai.llm",
    "ModelConfig": "autoair.ai.llm",
    "build_embedder": "autoair.ai.llm",
    "build_llm": "autoair.ai.llm",
    "provider_config": "autoair.ai.llm",
    "SpecCheck": "autoair.ai.problems",
    "SpecProblem": "autoair.ai.problems",
    "check_spec": "autoair.ai.problems",
    "Usage": "autoair.ai.usage",
    "track_usage": "autoair.ai.usage",
}


def __getattr__(name: str) -> Any:
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module 'autoair.ai' has no attribute {name!r}")
    value = getattr(importlib.import_module(module), name)
    globals()[name] = value
    return value


__all__ = [
    "DEFAULT_MODEL",
    "LLM",
    "ChatMessage",
    "Finding",
    "FindingsReport",
    "GenerationResult",
    "LLMError",
    "ModelConfig",
    "PatchOp",
    "SpecCheck",
    "SpecProblem",
    "Usage",
    "apply_findings",
    "apply_patch",
    "build_embedder",
    "build_llm",
    "check_spec",
    "describe_dag",
    "explain_problems",
    "generate_spec",
    "provider_config",
    "rank_operators",
    "select_operators",
    "track_usage",
]
