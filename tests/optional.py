"""Skip markers for optional extras that some supported Airflow versions cannot install.

The CI matrix installs every extra a given Airflow can resolve (see
website/docs/installation/compatibility.md); tests of an extra that is absent skip with the
reason instead of failing at import time.
"""

from __future__ import annotations

import pytest

from autoair.mcp.availability import mcp_runtime_available

MCP_SKIP_REASON = (
    "autoair[mcp] is not installed (FastMCP 4 needs Starlette >= 1.0.1, which Airflow 3.1's "
    "FastAPI pin excludes)"
)

requires_mcp = pytest.mark.skipif(not mcp_runtime_available(), reason=MCP_SKIP_REASON)


def importorskip_mcp() -> None:
    """Skip the calling test module when FastMCP is missing."""
    if not mcp_runtime_available():
        pytest.skip(MCP_SKIP_REASON, allow_module_level=True)
