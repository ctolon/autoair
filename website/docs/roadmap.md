---
title: Roadmap
description: What AutoAir 0.1.0 ships, what is planned or under consideration, and how to propose a feature.
sidebar_label: Roadmap
---

AutoAir is a visual and code-based DAG builder for Apache Airflow 3. It aims to cover what Airflow 3 can express, deploy the way production Airflow reads DAGs, and leave access control to Airflow. Released features are listed per version; planned items have no dates and may change. Detailed changes are in the [changelog](https://github.com/ctolon/autoair/blob/main/CHANGELOG.md).

## Released in 0.1.0

### Builder

- Visual editor inside the Airflow UI (Airflow 3.0 and later) and as a standalone server.
- Task groups, notes, insert-on-edge, copy and paste with XCom remapping, undo and redo.
- Command palette, context menus, keyboard shortcuts and automatic layout.
- Run-state and version-diff overlays on the canvas.
- Full-screen code mode and an editable Code tab that apply edited Python back to the DAG, with a read-only preview of the file each environment gets.
- Raw code DAGs: hand-written DAG files stored, reviewed, validated and deployed like visual DAGs.
- English and Turkish UI, light and dark themes, and configurable branding.

### Authoring

- Typed DAG spec with automatic migrations as the single source of truth.
- Operator catalog of every installed provider, plugin and package, read statically without importing modules.
- TaskFlow decorators, dynamic task mapping, Airflow 3.4 task loops and setup/teardown tasks.
- Callbacks and notifiers, retry policies, deadline alerts and human-in-the-loop operators.
- Schedule editor for cron, presets, multi-cron, assets, asset watchers, partitions and combined schedules, with a next-runs preview.
- DAG params with a trigger-form preview, and environment overlays for dev, staging and prod.
- Jinja-aware editors with completion, drag-to-map XComs and typed connection forms.
- Python and shell editors with completion, hover, signature help and live lint.
- Output as Python, a JSON spec with a loader, Airflow 3.4 YAML or dag-factory YAML.
- Import from Python DAG files, from Airflow, from YAML, and in bulk from a folder or a git repository.

### Validation and policy

- Live validation of the graph, operator parameters, Jinja and the target Airflow version.
- Import check of the generated DAG with a warm DagBag pool.
- Organisation policy switches, declarative `policy.rules` and an optional Open Policy Agent decision.
- Detection of hard-coded secrets in parameters and inline code.

### Deploy and lifecycle

- Deploy to a local DAG folder, to object storage, or to Git through the git CLI, GitHub, GitLab, Bitbucket Cloud, Azure DevOps or Gitea.
- Pull-request mode or direct push, with labels, reviewers, drafts and GitHub App authentication.
- Deploy target discovery from Airflow's DAG bundle configuration, switchable without a restart.
- Commit and pull-request templates with a required context block and git trailers.
- Optimistic locking with a three-way semantic merge, version history and a visual graph diff.
- Environment promotion, and per-environment connection and variable mapping.
- Test runs of a task or a DAG without the metadata database; trigger, backfill, clear and pause.
- Template gallery, reusable fragments, versioned operator templates with their own editor, and signed marketplaces.
- Per-deploy git tokens typed by the user, never stored.

### Collaboration, identity and access

- Real-time co-editing with presence, remote cursors and per-user undo.
- Roles and permissions on top of Airflow's auth manager (FAB, Keycloak, SimpleAuthManager).
- Four-eyes approvals for deploys and roles allowed to push directly.
- Single sign-on through the Airflow session, Airflow credentials or an API token.
- Audit trail with configurable sinks and CSV export, and an admin panel.

### AI and MCP

- Generate, edit, explain, fix, review and document DAGs on pydantic-ai with Anthropic, OpenAI and compatible servers, Azure OpenAI, Gemini, Vertex AI, Ollama, Bedrock, Groq, Mistral or Cohere.
- Troubleshooting, developer and admin chat assistants and retrieval; troubleshooting can run the Airflow actions it proposes after approval.
- A documentation knowledge base over the Airflow, provider and Astronomer docs, usable without the AI extra and air-gapped.
- Guardrails, an AI audit log, and an optional separate AI service.
- MCP server for coding agents (stdio or HTTP), and an MCP client for external servers.

### Operations

- Run modes: Airflow plugin, standalone server, and local mode on a workstation.
- State in files, SQLite or PostgreSQL, with stateless replicas on PostgreSQL.
- Configuration from environment variables, YAML, JSON, INI, Python files and `airflow.cfg`; feature flags switchable at runtime from the admin panel.
- Health, readiness and Prometheus endpoints, JSON logs and optional OpenTelemetry tracing.
- Container image, Helm values for the official Airflow chart, and a Docker Compose demo.
- Supported versions: Apache Airflow 3.0 to 3.3 (3.4 pre-release tested), Python 3.10 to 3.14.

## In development for 0.2.0

These are implemented on the main branch and ship with the next release.

- **Editing existing Python DAGs.** Source policies (`generate`, `preserve`, `strict-preserve`) patch a hand-written file with LibCST instead of regenerating it, and deploys detect files changed outside AutoAir. See [Editing existing Python DAGs](/docs/user-guide/editing-python-dags).
- **Astronomer Blueprint steps.** See [Blueprint steps](/docs/user-guide/blueprints).
- **Large graphs.** Faster validation and a low-detail overview for DAGs with 500 to 1,000 tasks. See [Large DAGs](/docs/user-guide/large-dags).
- **Approving agent tool calls.** See [Tool approval](/docs/ai/tool-approval).
- **Isolated import checks.** A separate validator service with no network access. See [Validator service](/docs/deployment/validator-service).
- **Airflow 3.4.** DAG-page tab scoping, reparse and retry changes; tested against the 3.4 pre-release until 3.4.0 is final.
- **Plugin-mode visual baselines.** Visual regression screenshots taken inside the Airflow UI.

## Planned or under consideration

### Builder and editor

- **Runtime frontend extensions.** Load custom node kinds into the UI at runtime instead of registering them at build time.

### Security and operations

- **Managed Airflow guides.** Deployment guides for managed Airflow offerings.

### Testing

- **PostgreSQL in the CI matrix.** Run the Python and Airflow version matrix against the PostgreSQL state store. One PostgreSQL job covers it today.
- **Live provider checks.** GitHub, Bitbucket Cloud, Azure DevOps and Gitea are covered by contract tests against fake servers, and the AI features by a scripted model client. GitLab, FAB and Keycloak are already checked against real servers; add real GitHub, Bitbucket Cloud, Azure DevOps, Gitea and LLM provider runs.

## Proposing a feature

- Open a [feature request](https://github.com/ctolon/autoair/issues/new?template=feature_request.yml) describing the problem, who has it and what you do today instead.
- Use [GitHub Discussions](https://github.com/ctolon/autoair/discussions) for open-ended ideas and questions before they become a concrete request.
- Check the [open issues](https://github.com/ctolon/autoair/issues) first, and add your use case to an existing request rather than opening a duplicate.
- To contribute the change yourself, read [Development](/docs/contributing/development) and say in the issue that you are working on it.
