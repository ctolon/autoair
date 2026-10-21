"""Whether the AI runtime (pydantic-ai, ``pip install 'autoair[ai]'``) is installed.

The core package never imports pydantic-ai: without the extra ``features.ai`` is off
(``/api/meta`` says why, the in-process AI routes are not mounted, the AI MCP tools are
hidden); a proxy-mode server (``ai.service_url``) needs no extra.
"""

from __future__ import annotations

import importlib.util

INSTALL_HINT = (
    "install autoair[ai] plus a provider extra, e.g. pip install 'autoair[ai,ai-anthropic]' "
    "(ai-openai, ai-google, ai-ollama, ai-bedrock, ai-groq, ai-mistral, ai-cohere, ai-all)"
)


def ai_runtime_available() -> bool:
    """pydantic-ai can be imported."""
    return importlib.util.find_spec("pydantic_ai") is not None


def ai_unavailable_reason() -> str | None:
    """Why the AI runtime is missing (``None`` when it is installed)."""
    return None if ai_runtime_available() else f"The AI runtime is not installed: {INSTALL_HINT}."


__all__ = ["INSTALL_HINT", "ai_runtime_available", "ai_unavailable_reason"]
