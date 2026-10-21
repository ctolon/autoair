"""Model Context Protocol server for AutoAir (built on FastMCP; ``pip install autoair[mcp]``).

The server modules import FastMCP lazily so that the storage adapter can be used
without the optional dependency.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from autoair.mcp.store import RepositoryStore, SpecStore, StoredDag, store_for

if TYPE_CHECKING:
    from autoair.mcp.server import (
        AIRFLOW_TOKEN_ENV,
        airflow_credentials,
        create_mcp_server,
        main,
    )

_LAZY = {"AIRFLOW_TOKEN_ENV", "airflow_credentials", "create_mcp_server", "main"}


def __getattr__(name: str) -> Any:
    if name in _LAZY:
        from autoair.mcp import server

        return getattr(server, name)
    raise AttributeError(name)


__all__ = [
    "AIRFLOW_TOKEN_ENV",
    "RepositoryStore",
    "SpecStore",
    "StoredDag",
    "airflow_credentials",
    "create_mcp_server",
    "main",
    "store_for",
]
