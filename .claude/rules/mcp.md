---
paths:
  - "src/autoair/mcp/**"
  - "src/autoair/kb/mcp.py"
  - "docs/mcp.md"
  - "tests/integration/test_mcp_*.py"
---

# MCP server

Procedure for a new or changed tool: skill `add-mcp-tool`.

- Tools are registered inside `create_mcp_server(...)` (`mcp/server.py`) with typed
  `Annotated[..., Field(description=...)]` inputs, Pydantic outputs (`mcp/models.py`) and one of
  the annotations defined at the top of `server.py` (`READ_ONLY`, `READ_ONLY_REMOTE`,
  `LLM_TOOL`, `MEMORY_WRITE`, `DESTRUCTIVE`).
- Every tool is in `TOOL_PERMISSIONS` and, when a feature switch applies, `TOOL_FEATURES`
  (`mcp/iam.py`); tools that call Airflow include `"airflow"`; git writes join
  `GIT_WRITE_TOOLS`. `test_every_mcp_tool_is_classified` fails otherwise.
- Over stdio there is no principal: permission checks apply over HTTP only. Keep destructive
  tools behind feature switches rather than relying on IAM for a stdio path.
- Airflow calls use the caller's credentials (`airflow_credentials(ctx)`). Git tokens arrive
  only in the `X-AutoAir-Git-Token` header of the MCP request (`mcp/git_token.py`), never as a
  tool argument, so they never enter the model's context. Results go to an LLM: never return
  passwords or tokens, and cap result sizes.
- Check: `uv run pytest -q tests/integration/test_mcp_iam.py tests/integration/test_mcp_server.py`;
  document the tool in `docs/mcp.md`.
