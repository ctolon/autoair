"""Agent tool definitions (pydantic-ai free, so tool modules load without ``autoair[ai]``).

An :class:`AgentTool` is a typed tool of AutoAir's agents: :mod:`autoair.ai.agent` turns it
into a pydantic-ai tool. Tool sets are plain lists, so allow-lists, scoping and citation
wrapping work on them before a run.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field

MAX_TOOL_CHARS = 12_000


@dataclass(frozen=True)
class AgentTool:
    """A typed tool; ``handler`` may be sync (run in a worker thread) or async."""

    name: str
    description: str
    input_model: type[BaseModel]
    handler: Callable[[Any], Any]
    #: A JSON schema given to the model instead of ``input_model``'s (external MCP tools,
    #: whose ``input_model`` accepts any object).
    schema: dict[str, Any] | None = None
    #: ``write`` when a call changes something outside the run (``ai.approvals`` decides
    #: whether such calls wait for the user's approval); ``read`` otherwise.
    kind: Literal["read", "write"] = "read"

    def json_schema(self) -> dict[str, Any]:
        schema = dict(self.schema) if self.schema else self.input_model.model_json_schema()
        schema.pop("title", None)
        return schema


class AgentStep(BaseModel):
    index: int
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    ok: bool
    output_preview: str = ""
    duration_ms: int = 0


def render_output(value: Any, limit: int = MAX_TOOL_CHARS) -> str:
    if isinstance(value, BaseModel):
        text = value.model_dump_json(exclude_none=True, by_alias=True)
    elif isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, default=str, separators=(",", ":"))
    if len(text) > limit:
        text = text[:limit] + f"... [truncated {len(text) - limit} characters]"
    return text


__all__ = ["MAX_TOOL_CHARS", "AgentStep", "AgentTool", "render_output"]
