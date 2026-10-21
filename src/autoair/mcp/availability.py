"""Whether the MCP server (FastMCP 4, ``pip install 'autoair[mcp]'``) can run in this process.

FastMCP 4 needs Starlette >= 1.0.1. Airflow 3.1 pins FastAPI < 0.118 and so Starlette < 0.49:
the ``mcp`` extra cannot be installed into an Airflow 3.1 environment. There the plugin
reports MCP as unavailable (``/api/meta``, the admin *Features* tab) and the MCP server runs
in its own environment instead (``autoair mcp-serve --http``, or ``autoair ai-serve`` with
``ai.service.proxy_mcp``).
"""

from __future__ import annotations

import importlib.util

INSTALL_HINT = "install autoair[mcp] (pip install 'autoair[mcp]')"

AIRFLOW_31_HINT = (
    "Airflow 3.1 pins Starlette < 0.49 and FastMCP 4 needs Starlette >= 1.0.1, so autoair[mcp] "
    "cannot be installed next to it: run `autoair mcp-serve --http` from a separate "
    "environment, or the AI service with ai.service.proxy_mcp=true"
)


def mcp_runtime_available() -> bool:
    """FastMCP and the ``mcp`` SDK can be imported."""
    return all(importlib.util.find_spec(name) is not None for name in ("fastmcp", "mcp"))


def mcp_unavailable_reason() -> str | None:
    """Why the MCP server cannot run here (``None`` when it can)."""
    if mcp_runtime_available():
        return None
    from autoair._compat import airflow_version

    version = airflow_version()
    if version is not None and version[:2] == (3, 1):
        return f"The MCP server is not installed: {AIRFLOW_31_HINT}."
    return f"The MCP server is not installed: {INSTALL_HINT}."


__all__ = ["AIRFLOW_31_HINT", "INSTALL_HINT", "mcp_runtime_available", "mcp_unavailable_reason"]
