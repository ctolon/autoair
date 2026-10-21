"""AutoAir's agentic assistants.

* :mod:`.chat` - the developer and admin chat assistants ("Ask AI"): persisted threads
  (:mod:`.threads`), context compaction (:mod:`.compaction`), citations
  (:mod:`.sources`), tool sets (:mod:`.dev_tools`, :mod:`.admin_tools`);
* :mod:`.troubleshoot` - the scoped log-analysis agent for DAG runs, with the sandboxed
  file reader (:mod:`.fs_sandbox`) and external MCP servers as tools (:mod:`.mcp_client`);
* :mod:`.project_rules` - admin-managed rules injected into authoring prompts;
* :mod:`.upgrade` - the Airflow 3 upgrade assistant;
* :mod:`.deploy_message` - commit / PR messages from the spec diff.
"""
