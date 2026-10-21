"""Agentic research over the installed environment (air-gapped) and, when allowed, an
allow-list of documentation sites (online).

* :mod:`.local` - installed distributions, Airflow provider metadata
  (``get_provider_info``), docstrings / signatures of provider modules and their
  ``example_dags``, read-only source access confined to site-packages and configured
  folders.
* :mod:`.online` - SSRF-checked, rate-limited, size-limited and cached fetches of
  airflow.apache.org, PyPI JSON and raw GitHub files (``ai.research.online``).
* :mod:`.tools` - the research tools of the agent and the MCP server.
"""

from __future__ import annotations
