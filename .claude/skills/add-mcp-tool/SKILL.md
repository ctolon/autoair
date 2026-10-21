---
name: add-mcp-tool
description: Add or change a tool of AutoAir's MCP server (src/autoair/mcp/server.py). Use whenever a function is decorated with @mcp.tool or an MCP tool's inputs, permissions or feature switches change.
---

# Add an MCP tool

The server is FastMCP 4 on the `mcp` SDK 2 (`autoair[mcp]`, not part of `all`;
it cannot install next to Airflow 3.1, so MCP tests use the markers in `tests/optional.py`). Tools are registered inside
`create_mcp_server(...)` in `src/autoair/mcp/server.py`; access control is
`IamMiddleware` in `src/autoair/mcp/iam.py`.

## Steps

1. Register the tool inside `create_mcp_server`:

   ```python
   @mcp.tool(title="Human title", annotations=READ_ONLY, tags={"catalog"})
   def my_tool(arg: Annotated[str, Field(description="...")]) -> MyResult: ...
   ```

   - Inputs: `Annotated[..., Field(description=...)]`; outputs: Pydantic models
     (`src/autoair/mcp/models.py`) so clients get an `outputSchema`.
   - The docstring tells the model *when* to use the tool, not how it is implemented.
   - Annotations (defined at the top of `server.py`): `READ_ONLY`, `READ_ONLY_REMOTE` (reads
     that call Airflow or another server), `LLM_TOOL`, `MEMORY_WRITE`, `DESTRUCTIVE`. Pick the
     one the closest existing tool uses.
2. Classify it in `src/autoair/mcp/iam.py`:
   - `TOOL_PERMISSIONS["my_tool"] = (P.<PERMISSION>, ...)`; unknown tools fall back to
     `_UNKNOWN` (`admin:config`), i.e. effectively denied;
   - `TOOL_FEATURES["my_tool"] = ("feature", ...)` when a feature switch disables it; tools that
     call Airflow include `"airflow"` (off in `autoair local`);
   - git writes join `GIT_WRITE_TOOLS` to get the request-token rules.
3. Airflow calls use the caller's credentials (`airflow_credentials(ctx)`); never a service
   token. Git tokens arrive only through the `X-AutoAir-Git-Token` header of the MCP request
   (`mcp/git_token.py`), never as a tool argument, so they never enter the model's context.
4. Tests:

   ```bash
   uv run pytest -q tests/integration/test_mcp_iam.py tests/integration/test_mcp_server.py
   ```

   `test_every_mcp_tool_is_classified` fails until step 2 is done.
5. Document the tool in `docs/mcp.md` (tool table) and add a CHANGELOG entry.

## Pitfalls

- Over stdio there is no principal: permission checks apply over HTTP only. Do not rely on
  IAM for safety of a stdio-only path; keep destructive tools behind feature switches.
- Results go to an LLM: never return connection passwords or tokens, run code that handles a
  secret inside `autoair.secret_mask.masking(secret)`, and cap result sizes.

## Done when

- [ ] Tool in `TOOL_PERMISSIONS` (and `TOOL_FEATURES` if gated)
- [ ] Typed inputs/outputs with descriptions; correct annotations
- [ ] MCP IAM and server tests pass; `docs/mcp.md` updated
